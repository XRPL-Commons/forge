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


@contextmanager
def effect_lock(store):
    path = store.root / '.effect-controller.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise TransitionError('effect lock must be an owned private regular file')
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot issue GitHub effects')
        yield
    finally:
        os.close(fd)


class EffectController:
    def __init__(self, store, github, *, admission=None, clock=time.time, reliability=None):
        self.store, self.github, self.clock = store, github, clock
        self.admission = admission
        reliability = reliability or {}
        self.commands = Effects(store, clock=clock, retry_seconds=reliability.get('retry_seconds',30),
            retry_cap=reliability.get('effect_retry_cap_seconds',900),max_attempts=reliability.get('effect_max_attempts',8),
            uncertainty_seconds=reliability.get('effect_uncertainty_seconds',900))
        self.queries = EffectQueries(store, clock=clock)
        self.jobs = ExecutionQueries(store)

    def _lock(self):
        return effect_lock(self.store)

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
        if effect.get('review_source'):
            from ..store.review_pulls import exact_source_matches
            return exact_source_matches(pr,effect['review_source'])
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

    def _observe_unmerged(self, effect, pr):
        if effect['merge_status']!='uncertain':
            return False
        if (pr.get('number')!=effect['identity']['pull_number'] or pr.get('merged') is not False
            or ((pr.get('head') or {}).get('repo') or {}).get('id')!=effect['repo_id']
            or ((pr.get('base') or {}).get('repo') or {}).get('id')!=effect['repo_id']
            or (pr.get('base') or {}).get('ref')!=effect['target_branch']
            or not (pr.get('state')=='closed' or (pr.get('head') or {}).get('sha')!=effect['identity']['head_oid'])):
            return False
        evidence=dict(verified=True,merged=False,candidate_id=effect['candidate_id'],
            pull_number=effect['identity']['pull_number'],head_oid=effect['identity']['head_oid'],
            observed_head=(pr.get('head') or {}).get('sha'),repository_id=effect['repo_id'],
            base_branch=effect['target_branch'],state=pr.get('state'),observed_at=self.clock())
        self.commands.observe_unmerged(effect['id'],evidence=evidence)
        self._resolve_fault(effect)
        return True

    def _resolve_fault(self,effect):
        from ..store.scheduling import Scheduling
        with self.store.read_snapshot() as db:
            ids=[row[0] for row in db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='forge_problem' "
                "AND resolved_at IS NULL AND json_extract(details_json,'$.source')='scheduling' "
                "AND json_extract(details_json,'$.effect_id')=?",(effect['unit_id'],effect['id']))]
        for gate_id in ids:
            current=self.queries.get(effect['id'])
            Scheduling(self.store,clock=self.clock).recheck(Command(str(uuid.uuid4()),'supervisor:effects',current['unit_id'],current['unit_version']),gate_id)

    def step(self, effect_id):
        with self._lock():
            return self._step(effect_id)

    def _withdraw(self, effect, reason):
        status = 'uncertain' if effect['merge_status'] == 'uncertain' else 'superseded'
        self.commands.record(effect['id'], status, receipt={'reason': reason})
        if status=='uncertain':
            self.commands.bounded(effect['id'])
        return self.queries.get(effect['id'])

    def _step(self, effect_id):
        effect = self.queries.get(effect_id)
        if effect['status'] in {'confirmed', 'failed', 'superseded'}:
            return effect
        if effect['retry_at'] is not None and effect['retry_at'] > self.clock():
            return effect
        try:
            if effect['kind'] == 'github_settle_checks':
                if self.commands.bounded(effect_id):
                    self._settle_checks(effect, observe_only=True)
                    return self.queries.get(effect_id)
                self.commands.claim(effect_id, expected_version=effect['unit_version'])
                self._settle_checks(effect)
                return self.queries.get(effect_id)
            pr = self.github.pr(effect, effect['candidate']['pr'])
            if effect['kind'] == 'github_merge' and self.observe_merged(effect, pr):
                return self.queries.get(effect_id)
            if effect['kind']=='github_merge' and self._observe_unmerged(effect,pr):
                return self.queries.get(effect_id)
            if not self._matches(effect, pr) or pr.get('state') != 'open' or pr.get('merged'):
                return self._withdraw(effect, 'candidate_changed')
            if (effect['kind']=='github_merge' and effect['escalation_at'] is not None
                    and effect['merge_send_count']==0 and pr.get('mergeable') is True):
                self.commands.resume_unsent_merge(effect_id,evidence=dict(verified=True,merged=False,mergeable=True,state='open',
                    candidate_id=effect['candidate_id'],pull_number=effect['identity']['pull_number'],head_oid=effect['identity']['head_oid'],
                    base_oid=effect['identity']['base_oid'],repository_id=effect['repo_id'],base_branch=effect['target_branch'],observed_at=self.clock()))
                effect=self.queries.get(effect_id)
            authorization = None
            if effect['kind']=='github_check' and effect['payload'].get('review_only') is True:
                from .merge_controller import MergeAdmission
                authorization=self.admission.evaluate_review_policy(effect) if isinstance(self.admission,MergeAdmission) else None
                if authorization is None:return self._withdraw(effect,'review_policy_changed')
            if effect['kind'] == 'github_merge':
                authorization = self.admission(effect) if self.admission else None
                if pr.get('mergeable') is not True:
                    self.commands.observe_wait(effect_id, conflict=pr.get('mergeable') is False)
                    current=self.queries.get(effect_id)
                    # Elapsed unknown state is never evidence of a conflict.
                    if (pr.get('mergeable') is False and current['conflict_since'] is not None
                            and self.clock()-current['conflict_since']>=self.commands.uncertainty_seconds
                            and current['merge_send_count']==0):
                        self.commands.rebuild_conflict(effect_id)
                    else:
                        self.commands.bounded(effect_id)
                    return self.queries.get(effect_id)
            if self.commands.bounded(effect_id):
                self._observe_publication(effect)
                return self.queries.get(effect_id)
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
                check_id = self.github.check(effect, payload['role'], effect['candidate'], payload.get('check_external_id', effect_id),
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
                if self._observe_unmerged(self.queries.get(effect_id),fresh):
                    return self.queries.get(effect_id)
                if not self._matches(effect, fresh) or fresh.get('state') != 'open':
                    return self._withdraw(self.queries.get(effect_id), 'candidate_changed')
                if fresh.get('mergeable') is not True:
                    self.commands.observe_wait(effect_id,conflict=fresh.get('mergeable') is False)
                    return self.queries.get(effect_id)
                current = self.queries.get(effect_id)
                authorization = self.admission({**current, 'merge_request': True}) if self.admission else None
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
            self.commands.bounded(effect_id)
            raise
        return self.queries.get(effect_id)

    def _observe_publication(self, effect):
        """After escalation only independent read-back can finish a publication."""
        check_id=None
        if effect['kind']=='github_verdict':
            check_id=self.github.verdict(self.jobs.job(effect['job_id']),effect['payload']['report'],observe_only=True)
        elif effect['kind']=='github_check':
            payload=effect['payload']
            check_id=self.github.check(effect,payload['role'],effect['candidate'],payload.get('check_external_id',effect['id']),payload['conclusion'],payload['summary'],observe_only=True)
        if type(check_id) is int and check_id>0:
            self.commands.record(effect['id'],'confirmed',receipt={'check_id':check_id})
            self._resolve_fault(effect)

    def _settle_checks(self, effect, *, observe_only=False):
        """Recover interrupted cancellation by reading every original check back."""
        path = f"/commits/{effect['candidate']['head']}/check-runs?filter=all"
        roles = {'forge/review': 'review', 'forge/audit': 'audit', 'forge/merge-policy': 'controller'}
        identities = {role: self.github.connection(role)['app_id'] for role in roles.values()}
        # Runs a retired role App published before the move to one App are
        # history: settled when complete, and never patched with another App.
        history = {role: set(self.github.app_ids(role)) - {identities[role]} for role in roles.values()}
        runs = self.github.pages('controller', effect, path, 'check_runs')
        if len(runs) > 100:
            raise GitHubUnavailable('Check settlement exceeded its observation limit')
        external_ids = set(effect['payload']['external_ids'])
        check_ids = {(row['github_id'], row['app_id']) for row in effect['payload']['check_ids']}
        expected = set()
        for run in runs:
            role = roles.get(run.get('name'))
            app_id = (run.get('app') or {}).get('id')
            if not role or app_id not in {identities[role], *history[role]}:
                continue
            if run.get('external_id') not in external_ids and (run.get('id'), app_id) not in check_ids:
                continue
            if run.get('head_sha') != effect['candidate']['head']:
                raise GitHubUnavailable('Check settlement observed another head')
            if app_id != identities[role]:
                if run.get('status') == 'completed':
                    expected.add(run['id'])
                continue
            expected.add(run['id'])
            if run.get('status') != 'completed':
                if observe_only:
                    return
                payload = {'status': 'completed', 'conclusion': 'cancelled',
                    'output': {'title': run['name'] + ': stopped', 'summary': effect['payload']['summary']}}
                url = self.github.work_details_url(effect, effect['candidate']) if hasattr(self.github, 'work_details_url') else None
                if isinstance(url, str):
                    payload['details_url'] = url
                self.github.call(role, effect, 'PATCH', '/check-runs/' + str(run['id']), payload)
        after = self.github.pages('controller', effect, path, 'check_runs')
        verified = {run['id'] for run in after if run.get('id') in expected
                    and run.get('head_sha') == effect['candidate']['head'] and run.get('status') == 'completed'
                    and (run.get('app') or {}).get('id') in {identities.get(roles.get(run.get('name'))),
                                                             *history.get(roles.get(run.get('name')), ())}}
        if verified != expected:
            raise GitHubUnavailable('Check settlement has not been independently verified')
        self.commands.record(effect['id'], 'confirmed', receipt={'check_ids': sorted(verified)})
        self._resolve_fault(effect)

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
