"""Release elapsed automatic waits without changing budget or operator intent."""
import json
import math
import time
import uuid

from ..store.db import StoreError
from ..store.models import Command, TransitionError
from ..store.queries import _limit
from ..store.transitions import Lifecycle


class WaitController:
    def __init__(self, store, *, clock=time.time):
        self.store, self.clock = store, clock
        self.commands = Lifecycle(store, clock=clock)

    def step(self, gate_id):
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot release runtime waits')
            row = db.execute('SELECT g.*,u.version,u.state FROM unit_gates g JOIN units u ON u.id=g.unit_id '
                             'WHERE g.id=?', (gate_id,)).fetchone()
            if row is None:
                raise KeyError(gate_id)
            gate = dict(row)
        if gate['resolved_at'] is not None:
            return {'gate_id': gate_id, 'resolved': True}
        if gate['code'] not in {'provider_unavailable', 'retry_delay'}:
            raise TransitionError('this wait needs an observation or operator decision')
        if gate['state'] in {'merged', 'closed', 'dissolved'}:
            return {'gate_id': gate_id, 'pending': 'terminal_history'}
        retry_at = json.loads(gate['details_json']).get('retry_at')
        if type(retry_at) not in (int, float) or not math.isfinite(retry_at):
            return {'gate_id': gate_id, 'pending': 'missing_retry_time'}
        now = self.clock()
        if now < retry_at:
            return {'gate_id': gate_id, 'pending': 'retry_time'}
        command = Command(str(uuid.uuid4()), 'supervisor:waits', gate['unit_id'], gate['version'])
        self.commands.resolve_gate(command, gate_id, release='time', evidence={'observed_at': now})
        return {'gate_id': gate_id, 'resolved': True}

    def cycle(self, *, after='', limit=50):
        limit = _limit(limit)
        if not isinstance(after, str):
            raise ValueError('wait cursor must be a gate ID')
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot process runtime waits')
            rows = db.execute("SELECT g.id FROM unit_gates g JOIN units u ON u.id=g.unit_id "
                "WHERE g.id>? AND g.resolved_at IS NULL AND g.code IN ('provider_unavailable','retry_delay') "
                "AND u.state NOT IN ('merged','closed','dissolved') ORDER BY g.id LIMIT ?", (after, limit + 1)).fetchall()
        page, results = rows[:limit], []
        for row in page:
            try:
                results.append(self.step(row['id']))
            except (TransitionError, StoreError, OSError) as error:
                results.append({'gate_id': row['id'], 'error': getattr(error, 'code', type(error).__name__)})
        return {'items': results, 'next_cursor': page[-1]['id'] if len(rows) > limit else None}
