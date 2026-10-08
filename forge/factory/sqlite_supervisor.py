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

from ..store.artifacts import Artifacts
from ..store.db import StoreError
from ..store.models import TransitionError
from ..store.queries import _limit
from .github import GitHubUnavailable
from .runner import RunnerError


class SqliteSupervisor:
    def __init__(self, store, *, execution, observations, results, merges, effects,
                 dispatch, recovery=None, verification=None, waits=None, clock=time.time, retention_interval=3600):
        if type(retention_interval) not in (int, float) or not math.isfinite(retention_interval) or retention_interval <= 0:
            raise ValueError('retention interval must be finite and positive')
        self.store, self.clock, self.retention_interval = store, clock, retention_interval
        self.execution, self.observations, self.results = execution, observations, results
        self.merges, self.effects, self.dispatch = merges, effects, dispatch
        self.recovery, self.verification = recovery, verification
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
            if codes:
                errors[name] = {'count': len(codes), 'codes': sorted(set(codes))[:10]}
        return errors

    def cycle(self, *, limit=50):
        limit = _limit(limit)
        with self._lock():
            report = {'started_at': self.clock(), 'stages': {}}
            stages = [
                ('execution', lambda: self._page('execution', self.execution, limit, '')),
                ('observations', lambda: self._page('observations', self.observations, limit)),
                ('waits', lambda: self._page('waits', self.waits, limit, '')),
            ]
            if self.verification is not None:
                stages.append(('verification', lambda: self._page('verification', self.verification, limit, '')))
            stages.append(('results', lambda: self._page('results', self.results, limit, '')))
            if self.recovery is not None:
                stages.append(('recovery', lambda: self._page('recovery', self.recovery, limit)))
            stages.extend([
                ('merges', lambda: self._page('merges', self.merges, limit)),
                ('effects', lambda: self.effects.cycle(limit=limit)),
                ('dispatch', lambda: self.dispatch.cycle(limit=limit)),
                ('retention', self._retention),
            ])
            for name, run in stages:
                try:
                    report['stages'][name] = run()
                except (GitHubUnavailable, RunnerError, TransitionError, StoreError, OSError) as error:
                    report['stages'][name] = {'error': getattr(error, 'code', type(error).__name__)}
            report['finished_at'] = self.clock()
            # Store a bounded receipt, never raw worker reports or credentials.
            self._put('heartbeat', {'started_at': report['started_at'], 'finished_at': report['finished_at'],
                'errors': self._errors(report['stages'])})
            return report
