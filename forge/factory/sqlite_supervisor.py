"""Explicit SQLite controller composition; no production backend selection.

A cycle advances bounded pages. Its durable cursors avoid starving later work
when an earlier item waits on GitHub. Cleanup runs first, and a failed observation
stage does not prevent already-owned executions from being cleaned next cycle.
"""
from contextlib import contextmanager
import fcntl
import json
import math
import os
import stat
import time

from ..request_metrics import RequestBudgetExhausted, RequestMetrics, request_scope
from ..store.artifacts import Artifacts
from ..store.db import StoreError
from ..store.models import TransitionError
from ..store.queries import _limit
from .github import GitHubUnavailable
from .runner import RunnerError


class SqliteSupervisor:
    def __init__(self, store, *, execution, observations, results, merges, effects,
                 dispatch, recovery=None, verification=None, waits=None, requirements=None, operators=None,
                 discovery=None, deployments=None, planning=None, triage_effects=None, steward_effects=None, scheduling=None, clock=time.time, retention_interval=3600, cycle_budget_seconds=60, monotonic=time.monotonic):
        if type(retention_interval) not in (int, float) or not math.isfinite(retention_interval) or retention_interval <= 0:
            raise ValueError('retention interval must be finite and positive')
        if type(cycle_budget_seconds) not in (int, float) or not math.isfinite(cycle_budget_seconds) or cycle_budget_seconds <= 0:
            raise ValueError('cycle budget must be finite and positive')
        self.cycle_budget_seconds, self.monotonic = cycle_budget_seconds, monotonic
        self.store, self.clock, self.retention_interval = store, clock, retention_interval
        from ..store.scheduling import Scheduling
        self.scheduling = scheduling if scheduling is not None else Scheduling(store, clock=clock)
        self.execution, self.observations, self.results = execution, observations, results
        self.merges, self.effects, self.dispatch = merges, effects, dispatch
        self.recovery, self.verification = recovery, verification
        self.requirements, self.operators, self.discovery = requirements, operators, discovery
        self.deployments = deployments
        self.planning, self.triage_effects = planning, triage_effects
        self.steward_effects = steward_effects
        from .wait_controller import WaitController
        self.waits = waits if waits is not None else WaitController(store, clock=clock)

    @contextmanager
    def _lock(self):
        fd = os.open(self.store.root / '.sqlite-supervisor.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise TransitionError('supervisor lock must be an owned private regular file')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.store.read_snapshot() as db:
                if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                    raise TransitionError('staging imports cannot run a controller cycle')
            yield
        finally:
            os.close(fd)

    def _get(self, name, default=None):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT value FROM store_metadata WHERE key=?', ('supervisor:' + name,)).fetchone()
        return json.loads(row[0]) if row else default

    def _put(self, name, value):
        with self.store.write() as db:
            db.execute('INSERT INTO store_metadata(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('supervisor:' + name, json.dumps(value, allow_nan=False)))

    def _page(self, name, controller, limit, empty=None):
        cursor = self._get(name + ':cursor', empty)
        result = controller.cycle(after=cursor, limit=limit)
        if not result.get('checkpointed'):
            self._put(name + ':cursor', result['next_cursor'] or empty)
        return result

    def _retention(self):
        now = self.clock()
        last = self._get('retention_at')
        if last is not None and now < last + self.retention_interval:
            return {'due': False}
        count = Artifacts(self.store, clock=self.clock).prune_raw_logs(now=now, batch=500)
        # Repeating after interruption only prunes another eligible bounded page;
        # structured results, events and pinned recovery evidence are preserved.
        self._put('retention_at', now)
        return {'due': True, 'expired': count}

    @staticmethod
    def _errors(stages):
        errors = {}
        for name, result in stages.items():
            rows = result if isinstance(result, list) else result.get('items', [])
            codes = [row['error'] for row in rows if isinstance(row, dict) and row.get('error')]
            if isinstance(result, dict) and result.get('error'):
                codes.append(result['error'])
            if isinstance(result, dict) and result.get('error_count') is not None:
                if result['error_count']:
                    errors[name] = {'count': result['error_count'], 'codes': result['error_codes']}
                continue
            if codes:
                errors[name] = {'count': len(codes), 'codes': sorted(set(codes))[:10]}
        return errors

    def cycle(self, *, limit=50):
        limit = _limit(limit)
        with self._lock():
            report = {'started_at': self.clock(), 'stages': {}}
            stages = [
                ('execution', lambda: self._page('execution', self.execution, limit, '')),
            ]
            if self.operators is not None:
                stages.append(('operators', lambda: self._page('operators', self.operators, limit, '')))
            stages.append(('observations', lambda: self._page('observations', self.observations, limit)))
            if self.discovery is not None:
                stages.append(('discovery', lambda: self._page('discovery', self.discovery, limit)))
            if self.requirements is not None:
                stages.append(('requirements', lambda: self._page('requirements', self.requirements, limit)))
            stages.append(('waits', lambda: self._page('waits', self.waits, limit, '')))
            if self.verification is not None:
                stages.append(('verification', lambda: self._page('verification', self.verification, limit, '')))
            stages.append(('results', lambda: self._page('results', self.results, limit, '')))
            if self.recovery is not None:
                stages.append(('recovery', lambda: self._page('recovery', self.recovery, limit)))
            stages.extend([
                ('merges', lambda: self._page('merges', self.merges, limit)),
                ('effects', lambda: self.effects.cycle(limit=limit)),
            ])
            if self.triage_effects is not None:
                stages.append(('triage_effects', lambda: self._page('triage_effects', self.triage_effects, limit, '')))
            if self.steward_effects is not None:
                stages.append(('steward_effects', lambda: self._page('steward_effects', self.steward_effects, limit, '')))
            if self.deployments is not None:
                stages.append(('deployments', lambda: self._page('deployments', self.deployments, limit)))
            stages.extend([
                ('dispatch', lambda: self.dispatch.cycle(limit=limit)),
            ])
            if self.planning is not None:
                stages.append(('planning', lambda: self._page('planning', self.planning, limit)))
            # Execution always gets the first chance to verify cleanup. Remote
            # phases resume after the phase that spent the previous allowance;
            # no workload-dependent budget fraction can starve later admission.
            execution, phases = stages[0], stages[1:]
            resume = self._get('next_phase')
            names = [name for name, _ in phases]
            if resume in names:
                index = names.index(resume)
                phases = phases[index:] + phases[:index]
            next_phase = None
            timings = {}
            metrics = RequestMetrics()
            until = self.monotonic() + self.cycle_budget_seconds
            with request_scope(until=until, metrics=metrics):
                for name, run in [execution] + phases:
                    started = self.monotonic()
                    try:
                        result = run()
                    except RequestBudgetExhausted:
                        # No page checkpoint follows an unattempted request.
                        result = {'deferred': 'github_budget'}
                    except (GitHubUnavailable, RunnerError, TransitionError, StoreError, OSError) as error:
                        result = {'error': getattr(error, 'code', type(error).__name__)}
                    report['stages'][name] = result
                    timings[name] = round((self.monotonic() - started) * 1000, 3)
                    exhausted = self.monotonic() >= until or isinstance(result, dict) and result.get('deferred') == 'github_budget'
                    if name != 'execution' and exhausted and next_phase is None:
                        index = names.index(name)
                        next_phase = names[(index + 1) % len(names)]
                        self._put('next_phase', next_phase)
                    # Local work in later phases still runs. Their first remote
                    # request defers at the same transport boundary.
            # This local ownership pass still runs after remote budget exhaustion.
            # Disabled maintenance has an explicit owner and is never an orphan.
            maintenance = getattr(self.dispatch, 'config', {}).get('enabled') is False
            try:
                report['stages']['scheduling'] = self.scheduling.cycle(
                    after=self._get('scheduling:cursor'),limit=limit,maintenance=maintenance)
                self._put('scheduling:cursor',report['stages']['scheduling']['next_cursor'])
            except (TransitionError, StoreError, OSError) as error:
                report['stages']['scheduling'] = {'error':getattr(error,'code',type(error).__name__)}
            started = self.monotonic()
            try:
                report['stages']['retention'] = self._retention()
            except (TransitionError, StoreError, OSError) as error:
                report['stages']['retention'] = {'error': getattr(error, 'code', type(error).__name__)}
            timings['retention'] = round((self.monotonic() - started) * 1000, 3)
            if next_phase is None:
                self._put('next_phase', None)
            report['finished_at'] = self.clock()
            report['requests'] = metrics.view()
            report['stage_ms'] = timings
            report['next_phase'] = next_phase
            # Store bounded numeric evidence, never raw reports or credentials.
            self._put('heartbeat', {'started_at': report['started_at'], 'finished_at': report['finished_at'],
                'errors': self._errors(report['stages']), 'requests': report['requests'],
                'stage_ms': timings, 'next_phase': next_phase})
            return report
