"""A corrupt retained capture is a Forge fault, never a failed issue attempt."""
from .artifacts import ArtifactCorrupt
from .execution_problems import draft_in
from .executions import ExecutionQueries
from .models import TransitionError
from .transitions import Lifecycle, _json
from ..harnesses.execution import fault


class CaptureProblems(Lifecycle):
    def defer(self, command, job_id, *, execution_fence, input_manifest):
        queries = ExecutionQueries(self.store)
        job = queries.job(job_id)
        # Recheck the immutable retained bytes. A caller's exception or worker
        # report is not authority to discard a valid result without charging.
        try:
            queries.capture(job_id)
        except ArtifactCorrupt:
            pass
        else:
            raise TransitionError('Capture fault requires corrupt retained bytes')
        details = dict(source='execution', owner='forge', reason='capture_corrupt',
            job_id=job_id, capture_job_id=job_id, image_id=job['runner_config'].get('image'),
            **fault('ArtifactCorrupt', phase='contract', file='/opt/forge/store/artifacts.py'))

        def change(db, unit):
            current_job, current = self._job(db, unit, job_id, execution_fence, input_manifest)
            self._result(db, command, current_job, {'reason': 'forge_problem', 'execution': details},
                         'deferred' if current else 'stale_deferred')
            if not current:
                return {'accepted': False, 'charged': False}
            gate_id = self._open_gate(db, command, 'forge_problem', details, job_id=job_id)
            pins = list(db.execute('SELECT member_id FROM job_members WHERE job_id=?', (job_id,)))
            db.executemany('INSERT INTO forge_problem_jobs(job_id,gate_id,member_id,details_json) VALUES(?,?,?,?)',
                          [(job_id, gate_id, pin[0], _json(details)) for pin in pins])
            draft_in(db, job_id, details, self.clock())
            return {'accepted': True, 'charged': False, 'gate_id': gate_id, 'report_signature': details['signature']}

        return self._apply(command, 'capture_problem', dict(job_id=job_id,
            execution_fence=execution_fence, input_manifest=input_manifest), change)
