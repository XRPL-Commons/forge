"""Row commands and reads at the controller/worker boundary.

Launch configuration is saved with the lease, before any container operation.
Capture is immutable evidence, not permission to advance a delivery. Cleanup can
release capacity before GitHub result validation; the original assignment fence
remains available and operator decisions still supersede that assignment.
"""
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import zlib

from .artifacts import Artifacts, MAX_ARTIFACT_SIZE
from .models import TransitionError
from .runtime import writable
from .transitions import Lifecycle, _json, applicable_gates, manifest_hash


@dataclass(frozen=True)
class Launch:
    branch: str
    publisher: str
    provider: str
    deadline: float
    runner_config: dict
    policy: dict
    provider_id: str | None = None
    model: str | None = None
    context: dict = field(default_factory=dict)

    def payload(self):
        if not all(isinstance(v, str) and v for v in (self.branch, self.publisher, self.provider)):
            raise TransitionError('launch branch, publisher and provider are required')
        if type(self.deadline) not in (int, float) or not math.isfinite(self.deadline):
            raise TransitionError('launch deadline must be finite')
        for value in (self.provider_id, self.model):
            if value is not None and (not isinstance(value, str) or not value):
                raise TransitionError('provider and model identities must be nonempty strings')
        for value in (self.runner_config, self.policy, self.context):
            if not isinstance(value, dict) or len(_json(value).encode()) > 65536:
                raise TransitionError('launch configuration must be a bounded object')
        if set(self.context) - {'review_policy', 'ci_evidence', 'verification_checks', 'resume_branch', 'resume_pr', 'resume_head', 'publication_author'}:
            raise TransitionError('unsupported launch context field')
        return asdict(self)


def provider_available(db, provider, provider_id, config, now):
    """Account admission from current rows; usable inside reservation writes.

    Legacy provider-wide cooldowns remain effective. OAuth refresh files have a
    single writer until verified teardown. Qualified Codex access-only snapshots
    can share a file after every old refresh writer has drained.
    """
    reference = config.get('provider_auth_file_env')
    accounts = {provider, provider_id, reference} - {None}
    for account in accounts:
        cooldown = db.execute('SELECT retry_at FROM provider_cooldowns WHERE account_ref=? AND provider=?',
                              (account, provider)).fetchone()
        if cooldown and cooldown[0] > now:
            return False
        auxiliary = db.execute("SELECT 1 FROM auxiliary_execution_waits WHERE kind='provider_error' "
            "AND json_extract(details_json,'$.account_ref')=? AND json_extract(runner_config_json,'$.provider')=? "
            "AND json_extract(details_json,'$.retry_at')>? LIMIT 1",(account,provider,now)).fetchone()
        if auxiliary:return False
        observed = db.execute('SELECT readiness FROM provider_observations WHERE account_ref=? AND provider=? '
                              'ORDER BY observed_at DESC,id DESC LIMIT 1', (account, provider)).fetchone()
        if observed and observed[0] != 'ready':
            return False
    if not reference:
        return True
    parallel = provider == 'codex' and config.get('codex_parallel_auth') is True
    # Every execution domain pins the credential reference before launching.
    # A legacy unknown lease remains fail closed until verified teardown.
    for lease_table, runtime_table in (('discussion_leases','discussion_runtime'), ('auxiliary_leases','auxiliary_runtime')):
        rows = db.execute('SELECT r.manifest_json FROM '+lease_table+' l LEFT JOIN '+runtime_table+
                          ' r ON r.execution_id=l.execution_id LIMIT 101').fetchall()
        if len(rows)>100:
            return False
        for row in rows:
            if row[0] is None:
                return False
            other = json.loads(row[0]).get('runner_config') or {}
            if other.get('provider_auth_file_env') == reference and (
                    not parallel or other.get('provider') != 'codex' or other.get('codex_parallel_auth') is not True):
                return False
    rows = db.execute('SELECT l.provider,l.runner_config_json FROM job_launches l '
                      'JOIN jobs j ON j.id=l.job_id WHERE j.cleanup=0 '
                      "AND json_extract(l.runner_config_json,'$.provider_auth_file_env')=?", (reference,))
    for row in rows:
        other = json.loads(row['runner_config_json'])
        if not parallel or row['provider'] != 'codex' or other.get('codex_parallel_auth') is not True:
            return False
    return True


def insert_launch(db, job_id, slot, execution_fence, payload, now):
    """Called inside reserve_job's transaction, never as a second write."""
    if not provider_available(db, payload['provider'], payload['provider_id'], payload['runner_config'], now):
        raise TransitionError('provider account is unavailable or still leased')
    if payload['deadline'] <= now:
        raise TransitionError('launch deadline must be in the future')
    candidate = db.execute('SELECT c.branch FROM jobs j JOIN candidates c ON c.id=j.candidate_id WHERE j.id=?', (job_id,)).fetchone()
    if candidate and candidate[0] != payload['branch']:
        raise TransitionError('launch branch must match the assigned candidate')
    db.execute(
        'INSERT INTO job_launches(job_id,slot,execution_fence,branch,publisher,provider,provider_id,model,'
        'runner_config_json,policy_json,context_json,deadline) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (job_id, slot, execution_fence, payload['branch'], payload['publisher'], payload['provider'],
         payload['provider_id'], payload['model'], _json(payload['runner_config']), _json(payload['policy']),
         _json(payload['context']), payload['deadline']),
    )
    db.execute('INSERT INTO job_execution_state(job_id) VALUES(?)', (job_id,))
    pool = payload['runner_config'].get('provider_pool') or []
    position = next((index for index, entry in enumerate(pool) if entry.get('id') == payload['provider_id']), None)
    if position is not None:
        db.execute("INSERT INTO runtime_controls(name,cursor,updated_at) VALUES('provider_cursor',?,?) "
                   'ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at, '
                   'generation=runtime_controls.generation+1', (str((position + 1) % len(pool)), now))


def _artifact(db, job_id, kind, content, now, *, sequence=0, media_type=None):
    if len(content) > MAX_ARTIFACT_SIZE:
        raise TransitionError('worker capture exceeds the supported artifact size')
    packed = zlib.compress(content, 6)
    codec, stored = ('zlib', packed) if len(packed) < len(content) else ('identity', content)
    db.execute(
        'INSERT INTO job_artifacts(job_id,kind,sequence,media_type,digest,content,size,codec,raw_size,created_at,expires_at) '
        'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
        (job_id, kind, sequence, media_type or ('text/plain' if kind == 'logs' else 'application/json'),
         hashlib.sha256(content).hexdigest(), stored, len(stored), codec, len(content), now,
         now + 30 * 86400 if kind == 'logs' else None),
    )


class Executions(Lifecycle):
    """Typed execution commands share lifecycle versions and decision IDs."""

    @staticmethod
    def _owned(db, unit, job_id, execution_fence):
        job = db.execute(
            'SELECT j.*, l.execution_fence, e.container_id, e.captured_at FROM jobs j '
            'JOIN job_leases l ON l.job_id=j.id JOIN job_execution_state e ON e.job_id=j.id '
            'WHERE j.id=? AND j.unit_id=?', (job_id, unit['id']),
        ).fetchone()
        if job is None or job['execution_fence'] != execution_fence or job['cleanup']:
            raise TransitionError('execution does not own this live lease')
        return dict(job)

    def progress(self, command, job_id, *, execution_fence, phase, container_id=None):
        if phase not in {'starting', 'running'}:
            raise TransitionError('unsupported execution phase')
        if container_id is not None and (not isinstance(container_id, str) or not container_id):
            raise TransitionError('container identity must be nonempty')
        def change(db, unit):
            job = self._owned(db, unit, job_id, execution_fence)
            if job['fenced'] or job['outcome'] or job['captured_at'] is not None:
                raise TransitionError('execution authority was revoked or completed')
            manifest = json.loads(db.execute('SELECT input_manifest_json FROM attempts WHERE id=?', (job['attempt_id'],)).fetchone()[0])
            _, current = self._job(db, unit, job_id, execution_fence, manifest)
            if not current:
                raise TransitionError('execution assignment is no longer current')
            from .recovery import execution_gates
            if execution_gates(db, unit, job):
                raise TransitionError('a gate prevents launching this assignment')
            if phase == 'running' and not (container_id or job['container_id']):
                raise TransitionError('running requires the exact container identity')
            if container_id:
                db.execute('UPDATE job_execution_state SET container_id=? WHERE job_id=?', (container_id, job_id))
            db.execute('UPDATE jobs SET phase=? WHERE id=?', (phase, job_id))
            self._event(db, command, 'execution_progress', {'phase': phase}, job_id=job_id)
            return {'job_id': job_id, 'phase': phase}
        return self._apply(command, 'execution_progress', dict(job_id=job_id, execution_fence=execution_fence,
                           phase=phase, container_id=container_id), change)

    def observe(self, job_id, *, execution_fence, observation):
        """Record a successful engine inspection under the exact capacity lease.

        This refreshes liveness evidence, not lifecycle authority or unit version.
        An unavailable engine leaves the previous observation to become stale.
        """
        status = 'missing' if observation is None else observation.get('status')
        if status not in {'missing', 'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'}:
            raise TransitionError('unsupported container observation')
        container = observation.get('id') if observation is not None else None
        started = observation.get('started_at') if observation is not None else None
        if observation is not None and (not isinstance(container, str) or not container):
            raise TransitionError('container observation requires its identity')
        if started is not None and (type(started) not in (int, float) or not math.isfinite(started)):
            raise TransitionError('container start time must be finite')
        with self.store.write() as db:
            writable(db)
            row = db.execute('SELECT e.container_id FROM jobs j JOIN job_leases l ON l.job_id=j.id '
                'JOIN job_execution_state e ON e.job_id=j.id WHERE j.id=? AND l.execution_fence=? AND j.cleanup=0',
                (job_id, execution_fence)).fetchone()
            if row is None or (observation is not None and row['container_id'] is not None and row['container_id'] != container):
                raise TransitionError('observation does not own the exact leased container')
            db.execute('UPDATE job_execution_state SET container_id=COALESCE(container_id,?),observed_at=?,observed_status=?,started_at=COALESCE(?,started_at) WHERE job_id=?',
                       (container, self.clock(), status, started, job_id))
            if observation is not None:
                from .resource_usage import observe_in
                observe_in(db, job_id, container, observation.get('resources'))

    def collect(self, command, job_id, *, execution_fence, capture, preparation_refusal=None):
        """Commit capture and fence launch before reset; never accept worker claims."""
        if not isinstance(capture, dict) or not isinstance(capture.get('logs', ''), str):
            raise TransitionError('capture requires structured output and text logs')
        if preparation_refusal is not None:
            from ..factory.preparation_problems import validate
            validate(preparation_refusal)
            if command.actor_id != 'supervisor:execution' or capture.get('result') is not None:
                raise TransitionError('preparation refusals require a host execution observation')
        bundle = capture.get('worktree_bundle')
        from ..factory.delegation_artifacts import MAX_BUNDLE_SIZE
        if bundle is not None and (not isinstance(bundle, bytes) or not 0 < len(bundle) <= MAX_BUNDLE_SIZE):
            raise TransitionError('delegated output exceeds the supported bundle size')
        structured = {key: value for key, value in capture.items() if key not in {'logs', 'worktree_bundle'}}
        if bundle is not None:
            structured['bundle'] = {'digest': hashlib.sha256(bundle).hexdigest(), 'size': len(bundle),
                                    'chunks': (len(bundle) + MAX_ARTIFACT_SIZE - 1) // MAX_ARTIFACT_SIZE}
        content, logs = _json(structured).encode(), capture.get('logs', '').encode()
        if max(len(content), len(logs)) > MAX_ARTIFACT_SIZE:
            raise TransitionError('worker capture exceeds the supported artifact size')
        payload = dict(job_id=job_id, execution_fence=execution_fence,
                       capture_digest=hashlib.sha256(content).hexdigest(), logs_digest=hashlib.sha256(logs).hexdigest())
        if preparation_refusal is not None:
            payload['preparation_refusal'] = preparation_refusal
        def change(db, unit):
            job = self._owned(db, unit, job_id, execution_fence)
            if preparation_refusal is not None:
                state = db.execute('SELECT started_at,observed_status FROM job_execution_state WHERE job_id=?', (job_id,)).fetchone()
                if (job['phase'] not in {'reserved', 'starting'} or job['fenced']
                        or state['started_at'] is not None or state['observed_status'] not in ('missing', 'created')
                        or preparation_refusal['manifest_digest'] != manifest_hash(json.loads(db.execute(
                            'SELECT input_manifest_json FROM attempts WHERE id=?', (job['attempt_id'],)).fetchone()[0]))):
                    raise TransitionError('preparation refusal requires an unstarted current assignment')
            if job['captured_at'] is not None or job['outcome'] is not None:
                raise TransitionError('execution already has a capture or result')
            now = self.clock()
            _artifact(db, job_id, 'worker_capture', content, now)
            from .resource_usage import capture_in
            capture_in(db, job_id, structured, now)
            _artifact(db, job_id, 'logs', logs, now)
            if bundle is not None:
                if job['role'] != 'build':
                    raise TransitionError('only a build assignment can return a worktree bundle')
                for sequence, offset in enumerate(range(0, len(bundle), MAX_ARTIFACT_SIZE)):
                    _artifact(db, job_id, 'worktree_bundle', bundle[offset:offset + MAX_ARTIFACT_SIZE], now,
                              sequence=sequence, media_type='application/x-git-bundle')
            db.execute('UPDATE job_execution_state SET captured_at=?,capture_authorized=? WHERE job_id=?',
                       (now, int(not job['fenced']), job_id))
            db.execute("UPDATE jobs SET fenced=1,phase='collecting' WHERE id=?", (job_id,))
            if unit['state'] not in {'merged', 'closed', 'dissolved'}:
                self._ensure_cleanup_gate(db, command, job_id, job['member_id'])
                self._open_gate(db, command, 'result_pending', {'job_id': job_id}, job['member_id'],
                                None if job['member_id'] else job['candidate_id'])
            self._event(db, command, 'execution_progress', {'phase': 'collecting', **payload}, job_id=job_id)
            return {'job_id': job_id, 'captured': True}
        return self._apply(command, 'collect_execution', payload, change)

    def fence_execution(self, command, job_id, *, execution_fence, reason):
        """Stop just this launch. Other batch members keep their authority."""
        if not isinstance(reason, str) or not reason or len(reason) > 200:
            raise TransitionError('fencing requires a bounded reason code')
        def change(db, unit):
            job = self._owned(db, unit, job_id, execution_fence)
            db.execute('UPDATE jobs SET fenced=1 WHERE id=?', (job_id,))
            db.execute('UPDATE job_execution_state SET result_revoked=1 WHERE job_id=?', (job_id,))
            if unit['state'] not in {'merged', 'closed', 'dissolved'}:
                self._ensure_cleanup_gate(db, command, job_id, job['member_id'])
            self._event(db, command, 'attempt_fenced', {'reason': reason}, job_id=job_id)
            return {'job_id': job_id}
        return self._apply(command, 'fence_execution', dict(job_id=job_id, execution_fence=execution_fence, reason=reason), change)


    def revoke_captured(self, command, job_id, *, execution_fence, input_manifest, policy_hash):
        """Revoke a captured assignment after a verified policy change.

        Cleanup may already have released its lease. The immutable launch fence
        still names the assignment; next-cycle stale-result consumption releases
        only this job's result gate, without charging or clearing other gates.
        """
        if (not isinstance(policy_hash, str) or not policy_hash
                or not isinstance(input_manifest, dict)
                or policy_hash == input_manifest.get('policy_hash')):
            raise TransitionError('captured policy revocation requires a different policy identity')
        payload = dict(job_id=job_id, execution_fence=execution_fence,
                       input_manifest=input_manifest, policy_hash=policy_hash)
        def change(db, unit):
            job, _ = self._job(db, unit, job_id, execution_fence, input_manifest)
            capture = db.execute('SELECT captured_at FROM job_execution_state WHERE job_id=?', (job_id,)).fetchone()
            if capture is None or capture['captured_at'] is None:
                raise TransitionError('only a captured assignment can be revoked by this command')
            db.execute('UPDATE jobs SET fenced=1 WHERE id=?', (job_id,))
            db.execute('UPDATE job_execution_state SET result_revoked=1 WHERE job_id=?', (job_id,))
            self._event(db, command, 'attempt_fenced',
                        {'reason': 'policy_changed', 'policy_hash': policy_hash}, job_id=job_id)
            return {'job_id': job_id, 'result_revoked': True}
        return self._apply(command, 'revoke_captured_policy', payload, change)


    def revoke_captured_base(self, command, job_id, *, execution_fence, input_manifest, publication):
        """Retain a verified publication while retiring its obsolete build base.

        This receipt owns only continuation of the exact PR, never acceptance of
        a candidate. The next result pass consumes the revoked capture uncharged.
        """
        from ..factory.contracts import SHA
        fields = {'pull_number', 'branch', 'head_oid', 'base_oid', 'repository_id', 'base_branch', 'author'}
        if (not isinstance(publication, dict) or set(publication) != fields
                or type(publication['pull_number']) is not int or publication['pull_number'] <= 0
                or type(publication['repository_id']) is not int
                or any(not isinstance(publication[key], str) or not publication[key] for key in ('branch', 'base_branch'))
                or any(not isinstance(publication[key], str) or not SHA.fullmatch(publication[key]) for key in ('head_oid', 'base_oid'))
                or not isinstance(publication['author'], dict) or set(publication['author']) != {'id', 'login', 'type'}
                or type(publication['author']['id']) is not int or publication['author']['id'] <= 0
                or any(not isinstance(publication['author'][key], str) or not publication['author'][key] for key in ('login', 'type'))
                or not isinstance(input_manifest, dict) or publication['base_oid'] == input_manifest.get('base_oid')):
            raise TransitionError('captured base revocation requires an exact changed publication identity')
        payload = dict(job_id=job_id, execution_fence=execution_fence, input_manifest=input_manifest, publication=publication)
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            capture = db.execute('SELECT captured_at,capture_authorized,result_revoked FROM job_execution_state WHERE job_id=?', (job_id,)).fetchone()
            if (not current or job['role'] != 'build' or not job['cleanup'] or capture is None
                    or capture['captured_at'] is None or not capture['capture_authorized'] or capture['result_revoked']):
                raise TransitionError('base revocation requires a current cleaned build capture')
            launch = db.execute('SELECT branch,publisher FROM job_launches WHERE job_id=?', (job_id,)).fetchone()
            if (publication['branch'] != launch['branch'] or publication['repository_id'] != unit['repository_id']
                    or publication['base_branch'] != unit['base_branch']):
                raise TransitionError('publication differs from the assigned build target')
            grouped = db.execute('SELECT stage FROM builder_assignments WHERE job_id=?', (job_id,)).fetchone()
            if grouped:
                if grouped['stage'] != 'integration':
                    raise TransitionError('only integration can retain a grouped publication')
                from .delivery import Delivery
                from .models import Candidate
                identity = Candidate(publication['pull_number'], publication['branch'], publication['head_oid'], publication['base_oid'],
                                     input_manifest['policy_hash'], input_manifest['requirements_hash'], manifest_hash(input_manifest))
                Delivery._publication_target_in(input_manifest, identity)
            elif db.execute('SELECT 1 FROM job_delegations WHERE job_id=?', (job_id,)).fetchone():
                raise TransitionError('children cannot retain a publication')
            target = input_manifest.get('publication_target') or {}
            expected_author = target['author'] if grouped and target.get('kind') == 'existing' else None
            author_matches = (publication['author'] == expected_author if expected_author is not None else
                              publication['author']['type'] == 'Bot' and publication['author']['login'] == launch['publisher'])
            if db.execute('SELECT 1 FROM mirror_job_inputs WHERE job_id=?', (job_id,)).fetchone():
                receipt = db.execute("SELECT r.receipt_json FROM publication_receipts r JOIN publication_intents p "
                    "ON p.id=r.intent_id WHERE p.job_id=? AND p.status='confirmed'", (job_id,)).fetchone()
                proven = json.loads(receipt[0]) if receipt else {}
                author_matches = (proven.get('pr_author') == publication['author']
                    and proven.get('branch') == publication['branch'] and proven.get('head_oid') == publication['head_oid']
                    and proven.get('pull_number') == publication['pull_number']
                    and proven.get('repository_id') == publication['repository_id'])
            if not author_matches:
                raise TransitionError('publication author differs from the assigned publisher')
            db.execute('UPDATE jobs SET fenced=1 WHERE id=?', (job_id,))
            db.execute('UPDATE job_execution_state SET result_revoked=1 WHERE job_id=?', (job_id,))
            self._event(db, command, 'attempt_fenced', {'reason': 'base_changed', 'publication': publication}, job_id=job_id)
            return {'job_id': job_id, 'result_revoked': True}
        return self._apply(command, 'revoke_captured_base', payload, change)


def observed_facts_in(db, job, candidate):
    """Project only facts attached to this candidate's confirmed publication."""
    from ..factory.source_inputs import digest
    rows = db.execute("SELECT p.id,p.job_id,p.output_digest,p.output_json,r.receipt_json "
        "FROM candidate_evidence e JOIN publication_intents p ON p.id=json_extract(e.details_json,'$.publication_receipt_id') "
        "JOIN publication_receipts r ON r.intent_id=p.id JOIN jobs j ON j.id=p.job_id "
        "WHERE e.candidate_id=? AND e.unit_id=? AND e.kind='publication' AND e.verdict='pass' "
        "AND p.unit_id=e.unit_id AND p.status='confirmed' AND j.attempt_id=e.attempt_id LIMIT 2",
        (candidate['id'], job['unit_id'])).fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise TransitionError('Candidate publication evidence is ambiguous')
    row = rows[0]
    receipt, output = json.loads(row['receipt_json']), json.loads(row['output_json'])
    facts = output.get('facts')
    if (not isinstance(facts, dict) or receipt.get('intent_id') != row['id']
            or receipt.get('job_id') != row['job_id'] or receipt.get('repository_id') != job['repo_id']
            or any(receipt.get(key) != candidate[key] for key in ('pull_number', 'branch', 'head_oid', 'base_oid'))
            or any(receipt.get(key) is not True for key in ('verified', 'exact_ref', 'exact_pr'))
            or receipt.get('output_digest') != row['output_digest'] or digest(output) != row['output_digest']
            or receipt.get('tree_oid') != output.get('tree_oid')
            or receipt.get('facts_digest') != digest(facts) or output.get('facts_digest') != digest(facts)):
        raise TransitionError('Candidate publication facts do not match its receipt')
    return dict(candidate_id=candidate['id'], publication_receipt_id=row['id'],
                repository_id=job['repo_id'], head_oid=receipt['head_oid'], base_oid=receipt['base_oid'],
                tree_oid=receipt['tree_oid'], facts_digest=receipt['facts_digest'], facts=facts)


class ExecutionQueries:
    def __init__(self, store):
        self.store = store

    def pending(self, *, after='', limit=50, results=False):
        """Indexed pages of IDs, excluding historical cleaned jobs and artifacts."""
        from .queries import _limit
        limit = _limit(limit)
        if not isinstance(after, str) or type(results) is not bool:
            raise ValueError('invalid execution cursor or result filter')
        where = 'j.outcome IS NULL AND e.captured_at IS NOT NULL' if results else 'j.cleanup=0'
        with self.store.read_snapshot() as db:
            rows = db.execute('SELECT j.id FROM jobs j JOIN job_execution_state e ON e.job_id=j.id '
                              'WHERE ' + where + ' AND j.id>? ORDER BY j.id LIMIT ?', (after, limit + 1)).fetchall()
            return {'items': [row[0] for row in rows[:limit]],
                    'next_cursor': rows[limit - 1][0] if len(rows) > limit else None}

    def job(self, job_id):
        """One worker assignment. No transcript, import archive or other jobs read."""
        with self.store.read_snapshot() as db:
            row = db.execute(
                'SELECT j.*, l.slot,l.execution_fence,l.branch,l.publisher,l.provider,l.provider_id,l.model,'
                'l.deadline,l.runner_config_json,l.policy_json,l.context_json,e.container_id,e.captured_at,'
                'e.capture_authorized,e.result_revoked,u.state AS unit_state,u.current_candidate_id,'
                'u.version AS unit_version,u.base_branch AS target_branch,'
                'u.fencing_generation,u.membership_generation,u.budget_generation,r.id AS repo_id,r.full_name AS repo,'
                'a.input_manifest_json,a.assignment_generation,a.membership_generation AS assigned_membership,'
                'a.budget_generation AS assigned_budget '
                'FROM jobs j JOIN job_launches l ON l.job_id=j.id JOIN job_execution_state e ON e.job_id=j.id '
                'JOIN units u ON u.id=j.unit_id JOIN repositories r ON r.id=u.repository_id '
                'JOIN attempts a ON a.id=j.attempt_id WHERE j.id=?', (job_id,),
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            job = dict(row)
            unit = dict(db.execute('SELECT * FROM units WHERE id=?', (job['unit_id'],)).fetchone())
            from .recovery import execution_gates
            job['launch_allowed'] = not execution_gates(db, unit, job)
            for source, target in [('runner_config_json', 'runner_config'), ('policy_json', 'policy'),
                                   ('input_manifest_json', 'input_manifest')]:
                job[target] = json.loads(job.pop(source))
            job.update(json.loads(job.pop('context_json')))
            job['candidate'] = None
            if job['candidate_id']:
                identity = dict(db.execute('SELECT * FROM candidates WHERE id=?', (job['candidate_id'],)).fetchone())
                job['candidate'] = dict(key=identity['id'], pr=identity['pull_number'], branch=identity['branch'],
                                        head=identity['head_oid'], base=identity['base_oid'], requirements=identity['requirements_hash'])
            if job['role'] in ('review', 'audit') and job['candidate_id']:
                job['observed_by_forge'] = observed_facts_in(db, job, identity)
            from .operator_views import reviewer_request_in
            job['reviewers_requested']=reviewer_request_in(db,job['candidate_id']) if job['candidate_id']==job['current_candidate_id'] else None
            # Every assignment reads its immutable scope; later membership edits
            # cannot change the requirements that were handed to this execution.
            members = db.execute('SELECT m.id AS member_id,i.github_number AS issue_number,m.issue_id AS issue_key,'
                                 'p.requirements_revision_id,r.requirements_json,r.title,r.body '
                                 'FROM job_members p JOIN unit_issues m ON m.id=p.member_id '
                                 'JOIN issues i ON i.id=m.issue_id JOIN issue_revisions r ON r.id=p.requirements_revision_id '
                                 'WHERE p.job_id=? ORDER BY m.id', (job_id,)).fetchall()
            job['members'] = [{**dict(member), 'requirements': json.loads(member['requirements_json'])} for member in members]
            for member in job['members']:
                del member['requirements_json']
            if not members:
                raise TransitionError('worker assignment has no pinned issue scope')
            # Singular fields remain only for singular assignments. Grouped
            # consumers use members and cannot accidentally authorize the first.
            if len(members) == 1:
                job.update({key: job['members'][0][key] for key in
                            ('issue_number', 'issue_key', 'requirements_revision_id', 'requirements')})
            else:
                job.update(issue_number=None, issue_key=None, requirements_revision_id=None,
                           requirements=[{key: member[key] for key in ('member_id', 'requirements_revision_id', 'requirements')}
                                         for member in job['members']])
            assignment = db.execute('SELECT builder_id,stage FROM builder_assignments WHERE job_id=?', (job_id,)).fetchone()
            if assignment:
                job.update(builder_id=assignment['builder_id'], builder_stage=assignment['stage'])
            task=db.execute('SELECT t.id,t.task_key,t.executor,t.scope,t.builder_id,x.plan_id,x.worktree_key,p.parent_job_id '
                'FROM job_tasks x JOIN delivery_tasks t ON t.id=x.task_id JOIN delegation_plans p ON p.id=x.plan_id WHERE x.job_id=?',(job_id,)).fetchone()
            if task:
                job['task']={**dict(task),'key':task['task_key']}
                job.update(builder_id=task['builder_id'],builder_stage='task',task_id=task['id'],task_key=task['task_key'],task_executor=task['executor'])
                if job['input_manifest'].get('manifest_version')!=2:
                    job['delegation']={key:task[key] for key in ('plan_id','builder_id','parent_job_id','worktree_key','scope')}|{'delegation_key':task['task_key']}
            if job['input_manifest'].get('verification_requests'):
                from .verification_requests import expand_in
                job['verification_requests'] = expand_in(db, job['input_manifest'], unit_id=job['unit_id'])
            job['history'] = {'failures': [], 'retry_plan': None, 'verified_checks': []}
            context = db.execute('SELECT retry_plan_json FROM unit_context WHERE unit_id=?', (job['unit_id'],)).fetchone()
            if context and context[0]:
                job['history']['retry_plan'] = json.loads(context[0])
            for failure in db.execute('SELECT attempt_id,role,cause,findings_json FROM attempt_failures '
                                      'WHERE unit_id=? ORDER BY position DESC LIMIT 3', (job['unit_id'],)):
                job['history']['failures'].append(dict(attempt=failure['attempt_id'], role=failure['role'],
                    cause=(failure['cause'] or '')[:2000], findings=json.loads(failure['findings_json']) if failure['findings_json'] else None))
            job['history']['failures'].reverse()
            if job['candidate_id']:
                from .verification import worker_receipts_in
                job['history']['verified_checks'] = worker_receipts_in(db, job['candidate_id'])
            if job['role'] == 'recovery':
                from .recovery import failure_context
                job['failure'] = failure_context(self.store, db, job['id'])
            prepared = db.execute('SELECT p.source_json,i.publication_authorization_json FROM mirror_job_inputs i '
                'JOIN mirror_sources p ON p.digest=i.source_digest AND p.policy_digest=i.policy_digest WHERE i.job_id=?', (job_id,)).fetchone()
            if prepared:
                job['source_input'] = json.loads(prepared['source_json'])
                job['publication_authorization'] = json.loads(prepared['publication_authorization_json'])
            return job

    def capture(self, job_id):
        """Result evaluators explicitly request evidence after selecting one job."""
        return json.loads(Artifacts(self.store).read(job_id, 'worker_capture').content)

    def bundle(self, job_id):
        from ..factory.delegation_artifacts import MAX_BUNDLE_SIZE
        capture = self.capture(job_id)
        metadata = capture.get('bundle') or {}
        count = metadata.get('chunks')
        if type(count) is not int or not 1 <= count <= MAX_BUNDLE_SIZE // MAX_ARTIFACT_SIZE:
            raise TransitionError('child output has no complete bounded bundle')
        content = b''.join(Artifacts(self.store).read(job_id, 'worktree_bundle', sequence).content for sequence in range(count))
        if len(content) != metadata.get('size') or hashlib.sha256(content).hexdigest() != metadata.get('digest'):
            raise TransitionError('child output bundle identity differs from capture')
        return content

    def input_refs(self, job):
        """Topological closure of immutable output refs, never current outputs."""
        manifest = job['failure']['input_manifest'] if job['role'] == 'recovery' else job['input_manifest']
        wanted = manifest.get('prerequisite_outputs', manifest.get('part_outputs', []))
        ordered, seen, visiting = [], set(), set()
        pending = [(output, manifest.get('manifest_version', 1), False) for output in reversed(wanted)]
        with self.store.read_snapshot() as db:
            while pending:
                output, version, expanded = pending.pop()
                identity = output['output_id']
                if expanded:
                    ordered.append(output)
                    visiting.remove(identity); seen.add(identity)
                    continue
                row = db.execute('SELECT o.*,j.id AS job_id,a.input_manifest_json FROM part_outputs o '
                                 'JOIN jobs j ON j.attempt_id=o.attempt_id JOIN attempts a ON a.id=o.attempt_id '
                                 'WHERE o.id=?', (identity,)).fetchone()
                keys = ('task_id', 'attempt_id', 'commit_oid', 'input_manifest_hash') if version == 2 else ('commit_oid', 'input_manifest_hash')
                if version not in (1, 2) or row is None or any(row[key] != output[key] for key in keys):
                    raise TransitionError('assigned task output is unavailable')
                if version == 1:
                    pins = db.execute('SELECT member_id FROM job_members WHERE job_id=?', (row['job_id'],)).fetchall()
                    if len(pins) != 1 or pins[0][0] != output['member_id']:
                        raise TransitionError('legacy output does not match its original member scope')
                # Check every occurrence before deduplicating: one output ID
                # cannot hide conflicting task, attempt or commit identities.
                if identity in seen:
                    continue
                if identity in visiting:
                    raise TransitionError('assigned output dependency cycle')
                visiting.add(identity)
                original = json.loads(row['input_manifest_json'])
                pending.append(({**output, 'job_id': row['job_id']}, version, True))
                pending.extend((prerequisite, original.get('manifest_version', 1), False)
                               for prerequisite in reversed(original['prerequisite_outputs']))
        return ordered

    def input_bundles(self, references):
        # Keep at most one bounded bundle in controller memory during IPC.
        for reference in references:
            yield {**reference, 'content': self.bundle(reference['job_id'])}
