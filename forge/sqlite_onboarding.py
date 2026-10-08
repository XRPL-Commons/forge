"""One bounded setup identity, with separate durable probe executions."""
from contextlib import contextmanager
from copy import deepcopy
import json
import time
import uuid

from .onboarding import Onboarding
from .credentials import Credentials
from .state import StateError
from .sqlite_identity import identity_effect_lock, audit_in
from .store.runtime import selected, writable
from .store.configuration import Configuration
from .store.auxiliary import save_in, job_in
from .factory.config import load, save, validate


def control_in(db, name, enabled, now, details=None):
    db.execute('INSERT INTO runtime_controls(name,enabled,updated_at,details_json) VALUES(?,?,?,?) '
               'ON CONFLICT(name) DO UPDATE SET enabled=excluded.enabled,generation=generation+1,'
               'updated_at=excluded.updated_at,details_json=excluded.details_json',
               (name, int(enabled), now, json.dumps(details or {})))


class SetupState:
    """CAS for a single setup instance; no database transaction spans caller IO."""
    def __init__(self, root, database, clock):
        self.root, self.database, self.clock = root, database, clock

    def _read(self, db):
        row = db.execute("SELECT * FROM setup_instances WHERE scope='live'").fetchone()
        if row is None:
            return {}, 0
        data = json.loads(row['settings_json'])
        ids = data.pop('_probe_execution_ids', None)
        if ids is None:
            ids = [r[0] for r in db.execute("SELECT id FROM auxiliary_executions WHERE domain='setup' AND owner_id='live' ORDER BY position LIMIT 101")]
        if len(ids) > 100:
            raise StateError('Setup probe history exceeds its bound')
        data.update(generation=row['generation'], phase=row['phase'], owner=row['owner'],
                    account_type=row['account_type'] or None, probe_jobs=[job_in(db, key) for key in ids])
        return data, row['revision']

    def read(self):
        with self.database.read_snapshot() as db:
            return self._read(db)[0]

    @contextmanager
    def transaction(self):
        with identity_effect_lock(self.database):
            with self.database.read_snapshot() as db:
                writable(db)
                data, revision = self._read(db)
            original = deepcopy(data)
            yield data
            if original == data:
                return
            self.replace(data, revision, original)

    def replace(self, data, revision, original):
        jobs = data.get('probe_jobs', [])
        if len(jobs) > 100:
            raise StateError('Setup probe history exceeds its bound')
        settings = {k: v for k, v in data.items() if k not in {'generation', 'phase', 'owner', 'account_type', 'probe_jobs'}}
        settings['_probe_execution_ids'] = [j['id'] for j in jobs]
        payload = json.dumps(settings, sort_keys=True, separators=(',', ':'), allow_nan=False)
        if len(payload.encode()) > 65536:
            raise StateError('Setup settings exceed their size limit')
        with self.database.write() as db:
            writable(db)
            current = db.execute("SELECT revision FROM setup_instances WHERE scope='live'").fetchone()
            if (current[0] if current else 0) != revision:
                raise StateError('Setup changed during the operation; retry')
            for job in jobs:
                existing = db.execute('SELECT 1 FROM auxiliary_runtime WHERE execution_id=?', (job['id'],)).fetchone()
                if existing is None and job.get('cleanup'):
                    # Imported completed probes remain immutable diagnostic evidence.
                    historical = job_in(db, job['id'])
                    if historical != job:
                        raise StateError('Completed imported probe cannot be changed')
                else:
                    save_in(db, 'setup', 'live', job, self.clock())
            db.execute("INSERT INTO setup_instances VALUES('live',?,?,?,?,?,?) ON CONFLICT(scope) DO UPDATE SET "
                       'generation=excluded.generation,phase=excluded.phase,owner=excluded.owner,account_type=excluded.account_type,'
                       'settings_json=excluded.settings_json,revision=excluded.revision',
                       (data['generation'], data['phase'], data.get('owner') or '', data.get('account_type') or '', payload, revision + 1))
            if data['generation'] != original.get('generation') or data['phase'] == 'resetting':
                db.execute("UPDATE client_connections SET revoked_at=?,revocation_reason='setup_changed' WHERE revoked_at IS NULL", (self.clock(),))
                db.execute("UPDATE client_pairings SET status='denied' WHERE status IN ('pending','approved')")
            control_in(db, 'onboarding_active', True, self.clock(), {'generation': data['generation']})
            control_in(db, 'factory_active', data['phase'] == 'active', self.clock(), {'generation': data['generation']})
            if data['phase'] == 'resetting':
                control_in(db, 'factory_paused', True, self.clock(), {'generation': data['generation']})
            audit_in(db, self.clock, 'setup_updated', details={'generation': data['generation'], 'phase': data['phase'], 'revision': revision + 1})


class SqliteOnboarding(Onboarding):
    def __init__(self, root, *, clock=time.time, client=None):
        from pathlib import Path
        self.root, self.clock, self.client = Path(root), clock, client
        self.database = selected(root)
        self.credentials = Credentials(root)
        self.store = SetupState(self.root, self.database, clock)

    def initialize(self, data):
        if not data:
            with self.database.read_snapshot() as db:
                row = db.execute("SELECT enabled FROM runtime_controls WHERE name='factory_active'").fetchone()
            data.update(version=1, generation=str(uuid.uuid4()), phase='active' if row and row[0] else 'setup',
                        apps={}, checks={}, probe_jobs=[], operation=None, error=None)
        elif data.get('phase') == 'legacy':
            data['phase'] = 'setup'

    def runtime(self):
        data = self.store.read()
        domain = 'runtime.json' if data.get('phase') == 'active' else 'setup/runtime.json'
        value = Configuration(self.database).get(domain) or Configuration(self.database).get('host-runtime.json')
        return validate({**(value['settings'] if value else {}), 'enabled': False, 'merge_enabled': False})

    def active_runtime(self):
        return load(self.root)

    def save_runtime(self, config, *, active):
        return save(self.root if active else self.root / 'setup', config)

    def provider_health(self):
        with self.database.read_snapshot() as db:
            rows = db.execute('SELECT * FROM provider_cooldowns').fetchall()
            return {r['provider']: {'until': r['retry_at'], 'reason': r['reason_code']} for r in rows}

    def reset_fence(self, data):
        with self.database.write() as db:
            writable(db)
            control_in(db, 'factory_paused', True, self.clock(), {'generation': data['generation']})

    def service_status(self):
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT heartbeat_at FROM runtime_controls WHERE name='setup_service'").fetchone()
        last = row[0] if row else 0
        return {'last_seen': last, 'available': bool(last and 0 <= self.clock() - last < 120)}
