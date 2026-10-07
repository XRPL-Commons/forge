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
from .transitions import Lifecycle, _json, applicable_gates


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
        if set(self.context) - {'review_policy', 'ci_evidence', 'verification_checks', 'resume_branch', 'resume_pr'}:
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
        observed = db.execute('SELECT readiness FROM provider_observations WHERE account_ref=? AND provider=? '
                              'ORDER BY observed_at DESC,id DESC LIMIT 1', (account, provider)).fetchone()
        if observed and observed[0] != 'ready':
            return False
    if not reference:
        return True
    # Discussion runtime migration has no typed account assignment yet. An
    # unknown live lease cannot be assumed to own a different OAuth file.
    if db.execute('SELECT 1 FROM discussion_leases LIMIT 1').fetchone():
        return False
    parallel = provider == 'codex' and config.get('codex_parallel_auth') is True
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
    members = db.execute('SELECT m.requirements_revision_id FROM unit_issues m JOIN jobs j ON j.unit_id=m.unit_id '
                         'WHERE j.id=? AND m.left_at IS NULL LIMIT 2', (job_id,)).fetchall()
    if len(members) != 1:
        raise TransitionError('worker launch currently requires a unit of one')
    candidate = db.execute('SELECT c.branch FROM jobs j JOIN candidates c ON c.id=j.candidate_id WHERE j.id=?', (job_id,)).fetchone()
    if candidate and candidate[0] != payload['branch']:
        raise TransitionError('launch branch must match the assigned candidate')
    db.execute(
        'INSERT INTO job_launches(job_id,slot,execution_fence,branch,publisher,provider,provider_id,model,'
        'runner_config_json,policy_json,context_json,deadline,requirements_revision_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        (job_id, slot, execution_fence, payload['branch'], payload['publisher'], payload['provider'],
         payload['provider_id'], payload['model'], _json(payload['runner_config']), _json(payload['policy']),
         _json(payload['context']), payload['deadline'], members[0][0]),
    )
    db.execute('INSERT INTO job_execution_state(job_id) VALUES(?)', (job_id,))
    pool = payload['runner_config'].get('provider_pool') or []
    position = next((index for index, entry in enumerate(pool) if entry.get('id') == payload['provider_id']), None)
    if position is not None:
        db.execute("INSERT INTO runtime_controls(name,cursor,updated_at) VALUES('provider_cursor',?,?) "
                   'ON CONFLICT(name) DO UPDATE SET cursor=excluded.cursor,updated_at=excluded.updated_at, '
                   'generation=runtime_controls.generation+1', (str((position + 1) % len(pool)), now))


def _artifact(db, job_id, kind, content, now):
    if len(content) > MAX_ARTIFACT_SIZE:
        raise TransitionError('worker capture exceeds the supported artifact size')
    packed = zlib.compress(content, 6)
    codec, stored = ('zlib', packed) if len(packed) < len(content) else ('identity', content)
    db.execute(
        'INSERT INTO job_artifacts(job_id,kind,sequence,media_type,digest,content,size,codec,raw_size,created_at,expires_at) '
        'VALUES(?,?,0,?,?,?,?,?,?,?,?)',
        (job_id, kind, 'text/plain' if kind == 'logs' else 'application/json',
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
            if applicable_gates(db, unit, job['member_id'], job['candidate_id']):
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

    def collect(self, command, job_id, *, execution_fence, capture):
        """Commit capture and fence launch before reset; never accept worker claims."""
        if not isinstance(capture, dict) or not isinstance(capture.get('logs', ''), str):
            raise TransitionError('capture requires structured output and text logs')
        structured = {key: value for key, value in capture.items() if key != 'logs'}
        content, logs = _json(structured).encode(), capture.get('logs', '').encode()
        if max(len(content), len(logs)) > MAX_ARTIFACT_SIZE:
            raise TransitionError('worker capture exceeds the supported artifact size')
        payload = dict(job_id=job_id, execution_fence=execution_fence,
                       capture_digest=hashlib.sha256(content).hexdigest(), logs_digest=hashlib.sha256(logs).hexdigest())
        def change(db, unit):
            job = self._owned(db, unit, job_id, execution_fence)
            if job['captured_at'] is not None or job['outcome'] is not None:
                raise TransitionError('execution already has a capture or result')
            now = self.clock()
            _artifact(db, job_id, 'worker_capture', content, now)
            _artifact(db, job_id, 'logs', logs, now)
            db.execute('UPDATE job_execution_state SET captured_at=?,capture_authorized=? WHERE job_id=?',
                       (now, int(not job['fenced']), job_id))
            db.execute("UPDATE jobs SET fenced=1,phase='collecting' WHERE id=?", (job_id,))
            if unit['state'] not in {'merged', 'closed', 'dissolved'}:
                self._ensure_cleanup_gate(db, command, job_id, job['member_id'])
                self._open_gate(db, command, 'result_pending', {'job_id': job_id}, job['member_id'], job['candidate_id'])
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
                'l.deadline,l.requirements_revision_id,l.runner_config_json,l.policy_json,l.context_json,e.container_id,e.captured_at,'
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
            job['launch_allowed'] = not applicable_gates(db, unit, job['member_id'], job['candidate_id'])
            for source, target in [('runner_config_json', 'runner_config'), ('policy_json', 'policy'),
                                   ('input_manifest_json', 'input_manifest')]:
                job[target] = json.loads(job.pop(source))
            job.update(json.loads(job.pop('context_json')))
            job['candidate'] = None
            if job['candidate_id']:
                identity = dict(db.execute('SELECT * FROM candidates WHERE id=?', (job['candidate_id'],)).fetchone())
                job['candidate'] = dict(key=identity['id'], pr=identity['pull_number'], branch=identity['branch'],
                                        head=identity['head_oid'], base=identity['base_oid'], requirements=identity['requirements_hash'])
            # The singleton adapter pins the original membership, even after its
            # unit is closed or the issue is admitted elsewhere.
            members = db.execute('SELECT m.id,i.github_number,m.issue_id '
                                 'FROM unit_issues m JOIN issues i ON i.id=m.issue_id '
                                 'WHERE m.unit_id=? ORDER BY m.joined_at,m.id LIMIT 2', (job['unit_id'],)).fetchall()
            if len(members) != 1:
                raise TransitionError('worker assignment currently requires a unit of one')
            member = members[0]
            requirements = db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?', (job['requirements_revision_id'],)).fetchone()[0]
            job.update(issue_number=member['github_number'], issue_key=member['issue_id'],
                       requirements=json.loads(requirements),
                       history={'failures': [], 'retry_plan': None, 'verified_checks': []})
            context = db.execute('SELECT retry_plan_json FROM unit_context WHERE unit_id=?', (job['unit_id'],)).fetchone()
            if context and context[0]:
                job['history']['retry_plan'] = json.loads(context[0])
            for failure in db.execute('SELECT attempt_id,role,cause,findings_json FROM attempt_failures '
                                      'WHERE unit_id=? ORDER BY position DESC LIMIT 3', (job['unit_id'],)):
                job['history']['failures'].append(dict(attempt=failure['attempt_id'], role=failure['role'],
                    cause=(failure['cause'] or '')[:2000], findings=json.loads(failure['findings_json']) if failure['findings_json'] else None))
            job['history']['failures'].reverse()
            return job

    def capture(self, job_id):
        """Result evaluators explicitly request evidence after selecting one job."""
        return json.loads(Artifacts(self.store).read(job_id, 'worker_capture').content)
