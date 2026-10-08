"""Reconcile durable GitHub intents without holding a SQLite transaction.

The process lock serializes each remote read/request/read sequence. Stable review
markers and check external IDs recover lost publication responses. A merge is
complete only after an independently fetched exact-candidate PR says merged.
"""
from contextlib import contextmanager
import fcntl
import json
import os
import stat
import time
import uuid

from ..store.effects import Effects, EffectQueries
from ..store.db import StoreError
from ..store.executions import ExecutionQueries
from ..store.models import Command, IdempotencyConflict, TransitionError
from .github import GitHubUnavailable


class EffectController:
    def __init__(self, store, github, *, admission=None, clock=time.time):
        self.store, self.github, self.clock = store, github, clock
        self.admission = admission
        self.commands = Effects(store, clock=clock)
        self.queries = EffectQueries(store)
        self.jobs = ExecutionQueries(store)

    @contextmanager
    def _lock(self):
        path = self.store.root / '.effect-controller.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise TransitionError('effect lock must be an owned private regular file')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.store.read_snapshot() as db:
                if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                    raise TransitionError('staging imports cannot issue GitHub effects')
            yield
        finally:
            os.close(fd)

    def ensure(self, job, report):
        """Return verified check identity; a lost response leaves durable intent."""
        if not job.get('candidate') or report.get('candidate') != job['candidate']['key']:
            raise TransitionError('verdict report belongs to a different candidate')
        with self._lock():
            existing = self.queries.verdict(job['id'])
            if existing is not None:
                if existing['payload'] != {'job_id': job['id'], 'report': report}:
                    raise IdempotencyConflict('verdict identity already belongs to another report')
                effect_id = existing['id']
            else:
                current = self.jobs.job(job['id'])
                # A rejected admission is journaled but must not permanently
                # consume the job's publication identity. The unique job row
                # and remote job marker bind the one accepted effect.
                request_id = 'verdict:' + job['id'] + ':' + str(uuid.uuid4())
                command = Command(request_id, 'supervisor:effects', current['unit_id'], current['unit_version'])
                effect_id = self.commands.queue_verdict(command, job['id'], report=report)['effect_id']
            result = self._step(effect_id)
            if result['status'] != 'confirmed':
                raise TransitionError('verdict effect is no longer admitted')
            return result['receipt']

    def queue_merge(self, command, candidate_id, *, policy_hash, authorization):
        return self.commands.queue_merge(command, candidate_id, policy_hash=policy_hash, authorization=authorization)

    @staticmethod
    def _matches(effect, pr):
        candidate = effect['identity']
        return (isinstance(pr, dict) and pr.get('number') == candidate['pull_number']
                and (pr.get('head') or {}).get('sha') == candidate['head_oid']
                and (pr.get('base') or {}).get('sha') == candidate['base_oid']
                and ((pr.get('head') or {}).get('repo') or {}).get('id') == effect['repo_id']
                and ((pr.get('base') or {}).get('repo') or {}).get('id') == effect['repo_id']
                and (pr.get('base') or {}).get('ref') == effect['target_branch']
                and pr.get('draft') is False)

    def observe_merged(self, effect, pr):
        """Reconcile this effect's merge; external human merges use Lifecycle directly."""
        if not self._matches(effect, pr) or pr.get('merged') is not True or not pr.get('merge_commit_sha'):
            return False
        current = self.queries.get(effect['id'])
        receipt = dict(verified=True, merged=True, pull_number=pr['number'],
                       head_oid=effect['identity']['head_oid'], base_oid=effect['identity']['base_oid'],
                       merge_sha=pr['merge_commit_sha'], observed_at=self.clock())
        self.commands.confirm_merged(Command(str(uuid.uuid4()), 'supervisor:effects', current['unit_id'],
                                              current['unit_version']), current['candidate_id'],
                                     merge_sha=receipt['merge_sha'], evidence=receipt)
        return True

    def step(self, effect_id):
        with self._lock():
            return self._step(effect_id)

    def _withdraw(self, effect, reason):
        status = 'uncertain' if effect['merge_status'] == 'uncertain' else 'superseded'
        self.commands.record(effect['id'], status, receipt={'reason': reason})
        return self.queries.get(effect['id'])

    def _step(self, effect_id):
        effect = self.queries.get(effect_id)
        if effect['status'] in {'confirmed', 'failed', 'superseded'}:
            return effect
        try:
            if effect['kind'] == 'github_settle_checks':
                self.commands.claim(effect_id, expected_version=effect['unit_version'])
                self._settle_checks(effect)
                return self.queries.get(effect_id)
            pr = self.github.pr(effect, effect['candidate']['pr'])
            if effect['kind'] == 'github_merge' and self.observe_merged(effect, pr):
                return self.queries.get(effect_id)
            if not self._matches(effect, pr) or pr.get('state') != 'open' or pr.get('merged'):
                return self._withdraw(effect, 'candidate_changed')
            authorization = None
            if effect['kind'] == 'github_merge':
                authorization = self.admission(effect) if self.admission else None
                if pr.get('mergeable') is not True:
                    return effect  # Unknown mergeability is an observation wait.
            try:
                self.commands.claim(effect_id, expected_version=effect['unit_version'], authorization=authorization)
            except TransitionError:
                return self._withdraw(effect, 'admission_changed')
            if effect['kind'] == 'github_verdict':
                job = self.jobs.job(effect['job_id'])
                check_id = self.github.verdict(job, effect['payload']['report'])
                if type(check_id) is not int or check_id < 1:
                    raise GitHubUnavailable('Published verdict check has no verified identity')
                self.commands.record(effect_id, 'confirmed', receipt={'check_id': check_id})
            elif effect['kind'] == 'github_check':
                payload = effect['payload']
                check_id = self.github.check(effect, payload['role'], effect['candidate'], effect_id,
                                             payload['conclusion'], payload['summary'])
                if type(check_id) is not int or check_id < 1:
                    raise GitHubUnavailable('Published check has no verified identity')
                self.commands.record(effect_id, 'confirmed', receipt={'check_id': check_id})
            elif effect['kind'] == 'github_merge':
                self.github.check(effect, 'controller', effect['candidate'], effect_id,
                                  'success', 'Technical and human gates verified for this candidate')
                # Check publication and authorization may take time. Re-read
                # both remote identity and local authority just before PUT.
                fresh = self.github.pr(effect, effect['candidate']['pr'])
                if self.observe_merged(effect, fresh):
                    return self.queries.get(effect_id)
                if not self._matches(effect, fresh) or fresh.get('state') != 'open' or fresh.get('mergeable') is not True:
                    return self._withdraw(self.queries.get(effect_id), 'candidate_changed')
                current = self.queries.get(effect_id)
                authorization = self.admission(current) if self.admission else None
                try:
                    self.commands.claim(effect_id, expected_version=current['unit_version'],
                                        authorization=authorization, merge_request=True)
                except TransitionError:
                    return self._withdraw(self.queries.get(effect_id), 'admission_changed')
                try:
                    self.github.merge(effect)  # HTTP success never establishes completion.
                except GitHubUnavailable as error:
                    if error.status in {400, 401, 403, 404, 405, 409, 422}:
                        # These responses explicitly reject this SHA-bound PUT;
                        # a timeout or server failure gives no such assurance.
                        refused = self.github.pr(effect, effect['candidate']['pr'])
                        if self.observe_merged(effect, refused):
                            return self.queries.get(effect_id)
                        current = self.queries.get(effect_id)
                        if (current['merge_send_count'] == 1 and refused.get('number') == effect['candidate']['pr']
                                and refused.get('merged') is False and refused.get('state') == 'open'
                                and ((refused.get('head') or {}).get('repo') or {}).get('id') == effect['repo_id']
                                and ((refused.get('base') or {}).get('repo') or {}).get('id') == effect['repo_id']
                                and (refused.get('base') or {}).get('ref') == effect['target_branch']):
                            receipt = dict(verified=True, merged=False, candidate_id=effect['candidate_id'],
                                pull_number=refused['number'], head_oid=effect['identity']['head_oid'],
                                observed_head=(refused.get('head') or {}).get('sha'), repository_id=effect['repo_id'],
                                observed_at=self.clock())
                            self.commands.refuse_merge(effect_id, http_status=error.status, evidence=receipt)
                    raise
                verified = self.github.pr(effect, effect['candidate']['pr'])
                if not self.observe_merged(effect, verified):
                    self.commands.record(effect_id, 'uncertain', receipt={'reason': 'merge_not_observed'})
            else:
                raise TransitionError('unsupported candidate effect kind')
        except GitHubUnavailable:
            self.commands.record(effect_id, 'uncertain', receipt={'reason': 'github_unavailable'})
            raise
        return self.queries.get(effect_id)

    def _settle_checks(self, effect):
        """Recover interrupted cancellation by reading every original check back."""
        path = f"/commits/{effect['candidate']['head']}/check-runs?filter=all"
        roles = {'forge/review': 'review', 'forge/audit': 'audit', 'forge/merge-policy': 'controller'}
        identities = {role: self.github.connection(role)['app_id'] for role in roles.values()}
        runs = self.github.pages('controller', effect, path, 'check_runs')
        if len(runs) > 100:
            raise GitHubUnavailable('Check settlement exceeded its observation limit')
        external_ids = set(effect['payload']['external_ids'])
        check_ids = {(row['github_id'], row['app_id']) for row in effect['payload']['check_ids']}
        expected = set()
        for run in runs:
            role = roles.get(run.get('name'))
            if not role or (run.get('app') or {}).get('id') != identities[role]:
                continue
            if run.get('external_id') not in external_ids and (run.get('id'), identities[role]) not in check_ids:
                continue
            if run.get('head_sha') != effect['candidate']['head']:
                raise GitHubUnavailable('Check settlement observed another head')
            expected.add(run['id'])
            if run.get('status') != 'completed':
                self.github.call(role, effect, 'PATCH', '/check-runs/' + str(run['id']), {
                    'status': 'completed', 'conclusion': 'cancelled',
                    'output': {'title': run['name'] + ': stopped', 'summary': effect['payload']['summary']}})
        after = self.github.pages('controller', effect, path, 'check_runs')
        verified = {run['id'] for run in after if run.get('id') in expected
                    and run.get('head_sha') == effect['candidate']['head'] and run.get('status') == 'completed'
                    and (run.get('app') or {}).get('id') == identities.get(roles.get(run.get('name')))}
        if verified != expected:
            raise GitHubUnavailable('Check settlement has not been independently verified')
        self.commands.record(effect['id'], 'confirmed', receipt={'check_ids': sorted(verified)})

    def cycle(self, *, limit=20):
        results = []
        with self._lock():
            with self.store.read_snapshot() as db:
                row = db.execute("SELECT value FROM store_metadata WHERE key='effects:cursor'").fetchone()
                after = json.loads(row[0]) if row else None
            page = self.queries.pending(limit=limit, after=after)
            for effect_id in page:
                try:
                    results.append(self._step(effect_id))
                except (GitHubUnavailable, TransitionError, StoreError, OSError) as error:
                    results.append({'id': effect_id, 'effect_id': effect_id,
                                    'error': getattr(error, 'code', type(error).__name__)})
            tail = self.queries.get(page[-1]) if len(page) == limit else None
            cursor = [tail['created_at'], tail['id']] if tail else None
            with self.store.write() as db:
                db.execute("INSERT INTO store_metadata(key,value) VALUES('effects:cursor',?) "
                           'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (json.dumps(cursor),))
        return results
