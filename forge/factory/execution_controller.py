"""SQLite worker execution driver; selected only by an explicit store caller.

All engine and credential operations happen outside SQL transactions. The
process lock serializes engine effects across controller restarts/processes;
durable IDs let an interrupted create/start/reset be reconciled on the next pass.
Result validation remains a separate GitHub observation step.
"""
from contextlib import contextmanager
import fcntl
import os
import stat
import time
import uuid

from ..store.executions import Executions, ExecutionQueries
from ..store.db import StoreError
from ..store.models import Command, TransitionError
from .github import GitHubUnavailable
from .runner import ContainerRunner, RunnerError


class ExecutionController:
    def __init__(self, store, runner, github, *, admission, clock=time.time):
        self.store, self.runner, self.github = store, runner, github
        self.admission, self.clock = admission, clock
        self.commands = Executions(store, clock=clock)
        self.queries = ExecutionQueries(store)

    @contextmanager
    def _lock(self):
        path = self.store.root / '.execution-controller.lock'
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise TransitionError('execution lock must be an owned private regular file')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.store.read_snapshot() as db:
                if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                    raise TransitionError('staging imports cannot execute workers')
            yield
        finally:
            os.close(fd)

    def _command(self, job):
        return Command(str(uuid.uuid4()), 'supervisor:execution', job['unit_id'], job['unit_version'])

    def _progress(self, job, phase, container_id=None):
        self.commands.progress(self._command(job), job['id'], execution_fence=job['execution_fence'],
                               phase=phase, container_id=container_id)
        return self.queries.job(job['id'])

    def _fence(self, job, reason):
        self.commands.fence_execution(self._command(job), job['id'],
                                      execution_fence=job['execution_fence'], reason=reason)
        return self.queries.job(job['id'])

    def _can_launch(self, job):
        return (job['launch_allowed'] and not job['fenced'] and not job['cleanup'] and job['outcome'] is None
                and job['assignment_generation'] == job['fencing_generation']
                and job['assigned_membership'] == job['membership_generation']
                and job['assigned_budget'] == job['budget_generation'])

    def step(self, job_id):
        with self._lock():
            return self._step(job_id)

    def _step(self, job_id):
        job = self.queries.job(job_id)
        if job['cleanup']:
            return {'job_id': job_id, 'cleanup': True}
        runner = (ContainerRunner(job['runner_config'], environ=self.runner.environ)
                  if isinstance(self.runner, ContainerRunner) else self.runner)
        if not job['fenced'] and job['phase'] in {'reserved', 'starting'}:
            if not self._can_launch(job) or self.clock() >= job['deadline'] or not self.admission(job):
                job = self._fence(job, 'launch_admission_withdrawn')
            else:
                current = runner.inspect(job)
                if current is None:
                    # Intent commits before create; create reconciles the same
                    # deterministic container name if its response was lost.
                    job = self._progress(job, 'starting')
                    container_id = runner.create(job)
                    job = self.queries.job(job_id)
                    if self._can_launch(job):
                        job = self._progress(job, 'starting', container_id)
                    current = runner.inspect(job)
                elif job['container_id'] is None:
                    job = self._progress(job, 'starting', current['id'])
                if current and current['status'] == 'created' and self._can_launch(job):
                    # Token exists only in memory and the runner's private IPC.
                    token = self.github.token(job['role'], job['repo_id'], worker=True)
                    job = self.queries.job(job_id)
                    if self._can_launch(job):
                        runner.prepare(job, token)
                        job = self.queries.job(job_id)
                        if self._can_launch(job):
                            runner.start(job)
                    del token
                job = self.queries.job(job_id)
                current = runner.inspect(job)
                if self._can_launch(job) and current and current['status'] != 'created':
                    job = self._progress(job, 'running', current['id'])

        current = runner.inspect(job)
        finished = current is None or current['status'] in {'exited', 'dead'}
        deadline = self.clock() >= job['deadline']
        if job['captured_at'] is None and job['outcome'] is None and (job['fenced'] or finished or deadline):
            capture = runner.capture(job)
            capture['timed_out'] = bool(deadline and not finished)
            # If an operator changed the version during capture, retry the
            # observation next cycle; no reset occurs until capture commits.
            self.commands.collect(self._command(job), job_id,
                                  execution_fence=job['execution_fence'], capture=capture)
            job = self.queries.job(job_id)
        if job['fenced']:
            receipt = runner.reset(job)
            if receipt.get('container_absent') is not True or receipt.get('capacity_ready') is not True:
                raise RunnerError('Teardown remains unverified')
            # Reset is idempotent. If a decision races with reset or commit
            # fails, the lease stays held until the next verified reset.
            job = self.queries.job(job_id)
            self.commands.verify_cleanup(self._command(job), job_id, execution_fence=job['execution_fence'],
                evidence={'process_stopped': True, 'resources_released': True, 'runner': receipt})
            return {'job_id': job_id, 'cleanup': True}
        return {'job_id': job_id, 'cleanup': False}

    def cycle(self, *, after='', limit=50):
        page = self.queries.pending(after=after, limit=limit)
        results = []
        for job_id in page['items']:
            try:
                results.append(self.step(job_id))
            except (RunnerError, GitHubUnavailable, TransitionError, StoreError, OSError) as error:
                # One unavailable provider must not prevent another execution's
                # cleanup. Keep output coded; runner errors may contain output.
                results.append({'job_id': job_id, 'error': getattr(error, 'code', type(error).__name__)})
        return {'items': results, 'next_cursor': page['next_cursor']}
