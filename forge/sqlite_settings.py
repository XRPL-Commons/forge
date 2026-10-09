"""Settings and access observations backed by normalized runtime rows."""
import json

from .settings import Settings, DEFAULT, validate
from .store.configuration import Configuration
from .store.runtime import writable


class SqliteSettings(Settings):
    def view(self, *, config=None):
        with self.database.read_session():
            return super().view(config=config)

    def repository_views(self, *, runtime):
        with self.database.read_session():
            return super().repository_views(runtime=runtime)

    @property
    def domain(self):
        return self.authority.domain('factory.json')

    def load(self):
        row = Configuration(self.database).get(self.domain)
        return validate(row['settings']) if row else json.loads(json.dumps(DEFAULT))

    def configured(self):
        return Configuration(self.database).get(self.domain) is not None

    def paused(self):
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT enabled FROM runtime_controls WHERE name='factory_paused'").fetchone()
            return bool(row and row[0])

    def generation(self):
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT generation FROM setup_instances WHERE scope='live'").fetchone()
            return row[0] if row else ''

    @staticmethod
    def _generation(db, expected):
        if expected is not None:
            row = db.execute("SELECT generation FROM setup_instances WHERE scope='live'").fetchone()
            if (row[0] if row else '') != expected:
                raise ValueError('Factory setup changed while the operation was running')

    def save_config(self, config, *, expected_generation=None, expected_revision=None):
        validate(config)
        if expected_revision is None:
            _, expected_revision = self.config_snapshot()
        with self.database.write() as db:
            writable(db)
            self._generation(db, expected_generation)
            Configuration(self.database, clock=self.clock).replace_in(
                db, self.domain, config, expected_revision=expected_revision)
            if self.scope == 'live':
                db.execute('UPDATE repositories SET enabled=0')
                for repo in config['repositories']:
                    db.execute('INSERT INTO repositories(id,full_name,enabled) VALUES(?,?,?) '
                               'ON CONFLICT(id) DO UPDATE SET full_name=excluded.full_name,enabled=excluded.enabled',
                               (repo['id'], repo['name'], int(repo['enabled'])))

    def observation(self):
        with self.database.read_snapshot() as db:
            result = {'roles': {}}
            header = db.execute('SELECT details_json FROM runtime_controls WHERE name=?', (self.scope + ':access',)).fetchone()
            if header:
                result.update(json.loads(header[0]))
            for row in db.execute('SELECT * FROM access_role_observations WHERE scope=?', (self.scope,)):
                value = {**json.loads(row['details_json']), 'status': row['status'], 'observed_at': row['observed_at']}
                rows = db.execute('SELECT * FROM access_repository_observations WHERE scope=? AND role=? ORDER BY position',
                                  (self.scope, row['role'])).fetchall()
                if rows or row['status'] == 'ok':
                    value['repositories'] = [dict(id=r['repository_id'], name=r['full_name'], archived=bool(r['archived']),
                                                  disabled=bool(r['disabled'])) for r in rows]
                result['roles'][row['role']] = value
            return result

    def save_observation(self, value, *, expected_generation=None):
        with self.database.write() as db:
            writable(db)
            self._generation(db, expected_generation)
            for role, row in value['roles'].items():
                details = {k: v for k, v in row.items() if k not in {'status', 'observed_at', 'repositories'}}
                db.execute('INSERT INTO access_role_observations VALUES(?,?,?,?,?) '
                           'ON CONFLICT(scope,role) DO UPDATE SET status=excluded.status,observed_at=excluded.observed_at,details_json=excluded.details_json',
                           (self.scope, role, row['status'], row['observed_at'], json.dumps(details, allow_nan=False)))
                db.execute('DELETE FROM access_repository_observations WHERE scope=? AND role=?', (self.scope, role))
                for index, repo in enumerate(row.get('repositories', [])):
                    db.execute('INSERT INTO access_repository_observations VALUES(?,?,?,?,?,?,?)',
                               (self.scope, role, repo['id'], repo['name'], int(repo.get('archived', False)),
                                int(repo.get('disabled', False)), index))
            self._control(db, self.scope + ':access', {k: v for k, v in value.items() if k != 'roles'})

    def _control(self, db, name, value):
        db.execute('INSERT INTO runtime_controls(name,heartbeat_at,updated_at,details_json) VALUES(?,?,?,?) '
                   'ON CONFLICT(name) DO UPDATE SET generation=generation+1,heartbeat_at=excluded.heartbeat_at,'
                   'updated_at=excluded.updated_at,details_json=excluded.details_json',
                   (name, value.get('observed_at', value.get('finished_at')), self.clock(), json.dumps(value, allow_nan=False)))

    def sync(self):
        with self.database.read_snapshot() as db:
            row = db.execute('SELECT details_json FROM runtime_controls WHERE name=?', (self.scope + ':access_sync',)).fetchone()
            return json.loads(row[0]) if row else {'status': 'never'}

    def save_sync(self, value):
        with self.database.write() as db:
            writable(db)
            self._control(db, self.scope + ':access_sync', value)

    def admission(self, repo):
        if self.scope == 'live' and self.paused():
            return {'allowed': False, 'reason': 'factory_paused'}
        with self.database.read_snapshot() as db:
            flags = dict(db.execute("SELECT name,enabled FROM runtime_controls WHERE name IN ('onboarding_active','factory_active')"))
        if self.scope == 'live' and flags.get('onboarding_active') and not flags.get('factory_active'):
            return {'allowed': False, 'reason': 'setup_not_started'}
        if not self.configured():
            return {'allowed': False, 'reason': 'settings_unavailable'}
        row = next((r for r in self.view()['repositories'] if r['name'].lower() == repo.lower()), None)
        if not row or not row['enabled']:
            return {'allowed': False, 'reason': 'repository_not_enabled'}
        return {'allowed': row['access_ready'], 'reason': 'access_verified' if row['access_ready'] else 'github_access_unverified',
                'roles': row['access']}
