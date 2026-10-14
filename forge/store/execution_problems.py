"""Atomic noncharged execution faults and deduplicated operator report drafts."""
import json

from .executions import ExecutionQueries
from .forge_problems import ForgeProblems, repair_in
from .models import TransitionError, VersionConflict
from .transitions import Lifecycle, _json


def draft_in(db, job_id, details, now):
    # An allowlist removes log prose, exception messages, credential values,
    # repository paths and URLs. The diagnostic excerpt is reconstructed from
    # the trusted image frame instead of publishing arbitrary worker logs.
    safe = {key: details[key] for key in ('signature', 'phase', 'exception', 'file', 'line')}
    db.execute('INSERT OR IGNORE INTO forge_problem_reports(signature,details_json,created_at) VALUES(?,?,?)',
               (safe['signature'], _json(safe), now))
    db.execute('INSERT OR IGNORE INTO forge_problem_report_jobs(signature,job_id) VALUES(?,?)',
               (safe['signature'], job_id))


class ExecutionProblems(Lifecycle):
    def defer(self, command, job_id, *, execution_fence, input_manifest, details):
        from ..factory.execution_problems import classify, contract_problem
        query = ExecutionQueries(self.store)
        job = query.job(job_id)
        capture = query.capture(job_id)
        decision = classify(job, capture, self.clock())
        trusted = decision['details'] if decision and decision['kind'] == 'forge_problem' else None
        if trusted is None and details.get('reason') == 'result_contract':
            from ..factory.contracts import result
            try:
                report = result(capture.get('result'), job)
            except (ValueError, TypeError, KeyError):
                report = None
            if report is None:
                trusted = contract_problem(job, capture)
        if trusted is None or trusted != details:
            raise TransitionError('Execution fault requires the retained exact capture')
        evidence = dict(reason='forge_problem', capture_job_id=job_id, execution=details)
        def change(db, unit):
            job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            self._result(db, command, job, evidence, 'deferred' if current else 'stale_deferred')
            if not current:
                return {'accepted': False, 'charged': False}
            # Scope the stop to this physical attempt's assigned members.
            gate_id = self._open_gate(db, command, 'forge_problem', details, job_id=job_id)
            pins = list(db.execute('SELECT member_id FROM job_members WHERE job_id=?', (job_id,)))
            if not pins:
                raise TransitionError('Execution failure has no immutable assigned scope')
            db.executemany('INSERT INTO forge_problem_jobs(job_id,gate_id,member_id,details_json) VALUES(?,?,?,?)',
                           [(job_id, gate_id, row[0], _json(details)) for row in pins])
            draft_in(db, job_id, details, self.clock())
            return {'accepted': True, 'charged': False, 'gate_id': gate_id, 'report_signature': details['signature']}
        return self._apply(command, 'execution_problem', dict(job_id=job_id, execution_fence=execution_fence,
            input_manifest=input_manifest, details=details), change)

    def recheck(self, command, gate_id):
        # Same qualified immutable replacement evidence as the startup path.
        # Generic resolves are forbidden; this rechecks the configuration inside
        # the write transaction and naturally allows one retry per image identity.
        with self.store.read_snapshot() as db:
            gate = ForgeProblems._gate(db, command, gate_id)
            if gate['resolved_at'] is not None:
                return {'gate_id': gate_id, 'resolved': True}
            if json.loads(gate['details_json']).get('source') != 'execution':
                raise TransitionError('Execution repair requires an execution fault')
            if repair_in(db, gate, self.clock()) is None:
                return {'gate_id': gate_id, 'pending': 'qualified_runner_update'}
        def change(db, unit):
            self._live(unit)
            gate = ForgeProblems._gate(db, command, gate_id)
            evidence = repair_in(db, gate, self.clock())
            if gate['resolved_at'] is not None or evidence is None:
                raise VersionConflict('Qualified execution replacement changed; recheck')
            self._resolve(db, command, gate_id, evidence)
            return {'gate_id': gate_id, 'resolved': True}
        return self._apply(command, 'recheck_execution_problem', {'gate_id': gate_id}, change)

    def report(self, signature):
        with self.store.read_snapshot() as db:
            return report_in(db, signature)


def report_in(db, signature):
    row = db.execute('SELECT * FROM forge_problem_reports WHERE signature=?', (signature,)).fetchone()
    if row is None:
        raise TransitionError('Unknown Forge report draft')
    details = json.loads(row['details_json'])
    affected = [dict(item) for item in db.execute('SELECT DISTINCT j.unit_id,p.job_id FROM forge_problem_report_jobs p '
        'JOIN jobs j ON j.id=p.job_id WHERE p.signature=? ORDER BY j.unit_id,p.job_id', (signature,))]
    lines = ['Forge execution failure', '', 'Signature: ' + signature, '', 'Diagnostic excerpt (safe image fields only):',
             f"{details['exception']} at {details['file']}:{details['line']} ({details['phase']})", '', 'Affected units and attempts:']
    lines += [f"- {item['unit_id']} / {item['job_id']}" for item in affected]
    return dict(status='draft', signature=signature, title='Forge execution failure: ' + details['exception'],
                body='\n'.join(lines), affected=affected)
