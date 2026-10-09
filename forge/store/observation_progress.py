"""Durable observation continuation and bounded sweep evidence."""
import hashlib
import json
import math
import uuid

from .runtime import writable

KEY = 'supervisor:observation_sweep'


class ObservationProgress:
    def __init__(self, store):
        self.store = store
        with store.read_snapshot() as db:
            row = db.execute('SELECT value FROM store_metadata WHERE key=?', (KEY,)).fetchone()
        self.data = json.loads(row[0]) if row else {'current': None, 'last': None, 'recent': []}

    def start(self, now):
        state = self.data['current']
        if state is None:
            state = dict(id=str(uuid.uuid4()), started_at=now, scope_hash=self.scope_hash(), cursor=None, pages=0, visits=0,
                         refreshed=0, deferred=0, errors=0, error_codes=[], retired=0,
                         phases=0, active_ms=0, scope_restarts=0, interrupted=False,
                         requests=dict(attempts=0, failures=0, elapsed_ms=0, quota={}))
            self.data['current'] = state
        elif state.get('in_progress'):
            state['interrupted'] = True
        state['in_progress'] = True
        state['phases'] += 1
        self.save()
        return state

    def save(self):
        with self.store.write() as db:
            writable(db)
            db.execute('INSERT INTO store_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (KEY, json.dumps(self.data, allow_nan=False)))
            # The supervisor's cursor reflects this same atomic checkpoint.
            cursor = self.data['current']['cursor'] if self.data['current'] else None
            db.execute('INSERT INTO store_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       ('supervisor:observations:cursor', json.dumps(cursor)))

    def page(self, cursor, limit):
        with self.store.read_snapshot() as db:
            if cursor is not None:
                unit = db.execute('SELECT membership_generation FROM units WHERE id=?', (cursor['unit_id'],)).fetchone()
                if unit and unit[0] != cursor['membership_generation']:
                    cursor = dict(unit_id=cursor['unit_id'], member_id='', membership_generation=unit[0])
                    self.data['current']['scope_restarts'] += 1
                    self.data['current']['cursor'] = cursor
            after = 'AND (u.id,m.id)>(?,?)' if cursor else ''
            params = (cursor['unit_id'], cursor['member_id'], limit) if cursor else (limit,)
            return [dict(row) for row in db.execute(
                'SELECT u.id AS unit_id,u.membership_generation,m.id AS member_id,m.issue_id '
                'FROM units u JOIN unit_issues m ON m.unit_id=u.id AND m.left_at IS NULL '
                "WHERE u.state NOT IN ('merged','closed','dissolved') " + after +
                ' ORDER BY u.id,m.id LIMIT ?', params)]

    def scope_hash(self):
        # A fixed-size structural receipt detects fresh members inserted behind
        # the cursor. Issue timestamps alone cannot prove their reconciliation.
        digest=hashlib.sha256()
        with self.store.read_snapshot() as db:
            rows=db.execute('SELECT u.id,u.membership_generation,m.id '
                'FROM units u JOIN unit_issues m ON m.unit_id=u.id AND m.left_at IS NULL '
                "WHERE u.state NOT IN ('merged','closed','dissolved') ORDER BY u.id,m.id")
            for row in rows:
                digest.update(json.dumps(tuple(row),separators=(',',':')).encode()+b'\n')
        return digest.hexdigest()

    def coverage(self, started_at, now):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT COUNT(*) AS members,COUNT(DISTINCT u.id) AS units,'
                'SUM(i.observed_at IS NULL) AS unknown_members,'
                'SUM(i.observed_at<?) AS stale_members,'
                'COUNT(DISTINCT CASE WHEN i.observed_at IS NULL OR i.observed_at<? THEN u.id END) AS stale_units,'
                'MIN(i.observed_at) AS oldest_observed_at '
                'FROM units u JOIN unit_issues m ON m.unit_id=u.id AND m.left_at IS NULL '
                'JOIN issues i ON i.id=m.issue_id '
                "WHERE u.state NOT IN ('merged','closed','dissolved')", (started_at, started_at)).fetchone()
        result = {key: row[key] or 0 for key in ('members','units','unknown_members','stale_members','stale_units')}
        result['oldest_observed_at'] = row['oldest_observed_at']
        result['oldest_age_seconds'] = max(0, now-row['oldest_observed_at']) if row['oldest_observed_at'] is not None else None
        return result

    def finish(self, now):
        state = self.data['current']
        state.update(finished_at=now, coverage=self.coverage(state['started_at'], now),
                     scope_changed=self.scope_hash()!=state['scope_hash'])
        state['complete'] = not (state['scope_changed'] or state['errors'] or state['deferred'] or state['interrupted']
                                 or state['coverage']['stale_units'])
        state['wall_seconds'] = now-state['started_at'] if now >= state['started_at'] else None
        self.data['last'] = state
        self.data['recent'] = (self.data['recent'] + [dict(finished_at=now, complete=state['complete'],
            active_ms=state['active_ms'], wall_seconds=state['wall_seconds'])])[-100:]
        healthy=[row for row in self.data['recent'] if row['complete'] and row['wall_seconds'] is not None]
        def percentile(field, fraction):
            values=sorted(row[field] for row in healthy)
            return values[math.ceil(len(values)*fraction)-1] if values else None
        self.data['statistics']=dict(window=len(self.data['recent']),healthy_samples=len(healthy),
            wall_seconds_p50=percentile('wall_seconds',0.5),wall_seconds_p95=percentile('wall_seconds',0.95),
            active_ms_p50=percentile('active_ms',0.5),active_ms_p95=percentile('active_ms',0.95))
        self.data['current'] = None
        self.save()
        return state
