"""Staged setup and durable Start/Reset intent. Only the supervisor applies intent."""
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import hmac
import json
import re
import secrets
import shlex
import time
import uuid
from pathlib import Path

from .app_access import AppClient, APP_PERMISSIONS, ROLE_PERMISSIONS, missing_app_permissions, missing_permissions
from .credentials import Credentials
from .settings import Settings, digest
from .sqlite_identity import identity_effect_lock, audit_in
from .store.auxiliary import save_in, job_in
from .store.files import StateError
from .store.runtime import required, writable
from .factory.config import validate as runtime_config
from .harnesses.registry import manifests, manifest

ROLES = tuple(ROLE_PERMISSIONS)
# The single Forge App: one manifest, one installation, every role's credential.
APP = 'app'


def status_label(cooldown):
    """The Coding accounts page's per-model wording for an active cooldown."""
    reason = (cooldown or {}).get('reason')
    if reason == 'blocked_auth':
        return 'sign in again'
    if reason == 'blocked_quota':
        return 'usage limit; retry pending'
    return 'unavailable; retry pending'


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


class Onboarding:
    def __init__(self, root, *, clock=time.time, client=None):
        self.root, self.clock, self.client = Path(root), clock, client
        self.database = required(root)
        self.credentials = Credentials(root)
        self.store = SetupState(self.root, self.database, clock)

    def api(self):
        return self.client or AppClient(environ=self.credentials.environment())

    def settings(self):
        return Settings(self.root, scope='setup', client=self.api(), clock=self.clock)

    def initialize(self, data):
        if not data:
            from .configuration_authority import ConfigurationAuthority
            active = ConfigurationAuthority(self.root, database=self.database).active()
            data.update(version=1, generation=str(uuid.uuid4()), phase='active' if active else 'setup',
                        apps={}, checks={}, probe_jobs=[], operation=None, error=None)

    def read(self):
        with self.store.transaction() as data:
            self.initialize(data)
            return deepcopy(data)

    def _runtime(self, scope, *, host_default=False):
        from .configuration_authority import ConfigurationAuthority
        authority = ConfigurationAuthority(self.root, scope=scope)
        row = authority.runtime()
        revisions = getattr(self, '_runtime_revisions', {})
        revisions[authority.scope] = row['revision'] if row else 0
        self._runtime_revisions = revisions
        if row is None and host_default:
            row = authority.snapshot('host-runtime.json')
        return runtime_config(row['settings'] if row else {})

    def runtime(self):
        config = self._runtime('active', host_default=True)
        return runtime_config({**config, 'enabled': False, 'merge_enabled': False})

    def active_runtime(self):
        return self._runtime('live')

    def save_runtime(self, config, *, active):
        from .configuration_authority import ConfigurationAuthority
        scope = 'live' if active else 'setup'
        authority = ConfigurationAuthority(self.root, scope=scope)
        domain = authority.domain('runtime.json')
        revisions = getattr(self, '_runtime_revisions', {})
        if scope not in revisions:
            row = authority.snapshot(domain)
            revisions[scope] = row['revision'] if row else 0
        revisions[scope] = authority.replace(domain, runtime_config(config), expected_revision=revisions[scope])
        self._runtime_revisions = revisions

    def provider_health(self):
        with self.database.read_snapshot() as db:
            rows = db.execute('SELECT * FROM provider_cooldowns').fetchall()
            return {r['provider']: {'until': r['retry_at'], 'reason': r['reason_code']} for r in rows}

    def reset_fence(self, data):
        with self.database.write() as db:
            writable(db)
            control_in(db, 'factory_paused', True, self.clock(), {'generation': data['generation']})

    def revision(self, data):
        return digest({'generation': data['generation'], 'config': self.settings().load(), 'runtime': self.runtime(),
                       'provider': data.get('provider_revision'), 'apps': data['apps']})

    def view(self):
        data = self.read()
        if data['phase'] == 'resetting':
            return {'phase': 'resetting', 'generation': data['generation'], 'revision': '',
                    'roles': {r: {'status': 'not_configured', 'registered': False} for r in ROLES},
                    'repositories': [], 'selected': None, 'owner': '', 'provider_connected': False,
                    'checks': {}, 'can_start': False, 'operation': data['operation'], 'error': data.get('error')}
        settings = self.settings().view()
        revision = self.revision(data)
        ready = data.get('readiness') or {}
        def registration(role):
            return data['apps'].get(APP) or data['apps'].get(role, {})
        return {'phase': data['phase'], 'generation': data['generation'], 'revision': revision,
                'account_type': data.get('account_type'), 'reset_receipt': data.get('reset_receipt'),
                'single_app': APP in data['apps'],
                'roles': {role: {'status': settings['roles'][role]['status'],
                                 'registered': bool(registration(role)),
                                 'app_id': registration(role).get('connection', {}).get('app_id'),
                                 'install_url': registration(role).get('install_url')} for role in ROLES},
                'repositories': [r for r in settings['repositories'] if r['access_ready']],
                'selected': data.get('selected'), 'owner': data.get('owner', ''),
                'provider_connected': bool(data.get('provider_revision')), 'checks': data['checks'],
                'harnesses': self.harnesses(data),
                'activity': self.activity(data),
                'repository_check': data.get('repository_check'),
                'repository_setup': next((r for r in settings['repositories'] if r['id'] == (data.get('selected') or {}).get('id')), None),
                'qualification_fresh': self.qualified(data),
                'image_verified': self.image_verified(data),
                'service': self.service_status(),
                'can_start': data['phase'] == 'setup' and ready.get('revision') == revision
                             and 0 <= self.clock() - ready.get('at', 0) < 300 and self.qualified(data)
                             and data['checks'].get('github') == 'passed' and not data.get('operation'),
                'operation': data.get('operation'), 'error': data.get('error')}

    def service_status(self):
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT heartbeat_at FROM runtime_controls WHERE name='setup_service'").fetchone()
        last = row[0] if row else 0
        return {'last_seen': last, 'available': bool(last and 0 <= self.clock() - last < 120)}

    def qualification_key(self, data):
        return digest({'runtime': self.runtime(), 'provider': data.get('provider_revision')})

    def image_verified(self, data):
        """The running image is the one the last passing check covered.

        Unlike qualification freshness, this does not expire and ignores
        capacity or pacing edits, so an active factory can report it."""
        value = data.get('qualification') or {}
        return 'image' in value and value['image'] == self.runtime().get('image')

    def qualified(self, data):
        value = data.get('qualification') or {}
        return (value.get('key') == self.qualification_key(data)
                and 0 <= self.clock() - value.get('at', 0) < 900)

    @staticmethod
    def activity(data):
        operation = data.get('operation')
        if not operation:
            return None
        action, step = operation['action'], operation.get('step')
        if action in ('check', 'harness_check'):
            if step in ('probe', 'probe_wait') and data.get('probe_jobs'):
                job = data['probe_jobs'][-1]
                if job.get('capacity_wait_since') is not None:
                    return {'target': 'setup', 'label': 'Waiting for available memory; verification will resume automatically'}
                if job['kind'] == 'provider':
                    provider = job['runner_config']['provider']
                    return {'target': 'provider', 'provider': provider, 'model_id': job.get('provider_id'),
                            'label': ('Finishing ' if job['fenced'] else 'Checking ') + manifest(provider)['name']}
                return {'target': 'runner', 'label': 'Checking worker ' + ('reuse' if job['kind'] == 'reuse' else 'cleanup')}
            if step == 'prepare' and action == 'check':
                return {'target': 'github', 'label': 'Checking repository access and protection'}
            if step == 'prepare' and action == 'harness_check' and data.get('phase') == 'active':
                if operation.get('harness'):
                    # A scoped check only waits for the workers holding that login.
                    return {'target': 'runner', 'label': 'Waiting for workers using the ' + manifest(operation['harness'])['name']
                            + ' login to finish; other work continues'}
                return {'target': 'runner', 'label': 'Waiting for active workers to finish before checking shared coding accounts; new work is paused'}
            return {'target': 'setup', 'label': 'Preparing verification'}
        return {'target': action, 'label': 'Starting Forge' if action == 'start' else 'Resetting Forge'}

    @staticmethod
    def editable(data):
        if data['phase'] != 'setup' or data.get('operation'):
            raise ValueError('Finish the current operation, or reset the existing factory before setting up again')

    def begin(self, role, owner, account_type, origin, browser):
        """Register the one Forge App (role ``app``), or a legacy per-role App."""
        if role not in (APP, *ROLES) or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', owner):
            raise ValueError('Choose a role and a GitHub account name')
        if account_type not in ('organization', 'personal') or not browser:
            raise ValueError('Choose a GitHub account type')
        with self.store.transaction() as data:
            self.initialize(data); self.editable(data)
            if data.get('owner') and (data['owner'].lower() != owner.lower() or data['account_type'] != account_type):
                raise ValueError('Use the same GitHub owner for all roles; reset to change owners')
            if role in data['apps']:
                return {'install_url': data['apps'][role]['install_url']}
            if APP in data['apps']:
                return {'install_url': data['apps'][APP]['install_url']}
            if role == APP and data['apps']:
                raise ValueError('This setup already registered per-role Apps; reset setup to use the one Forge App')
            state = secrets.token_urlsafe(32)
            data.update(owner=owner, account_type=account_type, error=None)
            data['challenge'] = {'state': hashlib.sha256(state.encode()).hexdigest(),
                                 'browser': hashlib.sha256(browser.encode()).hexdigest(), 'role': role,
                                 'generation': data['generation'], 'expires': self.clock() + 3600}
            action = ('https://github.com/organizations/' + owner + '/settings/apps/new'
                      if account_type == 'organization' else 'https://github.com/settings/apps/new')
            single = role == APP
            manifest = {'name': ('Forge ' if single else 'Forge ' + role + ' ') + secrets.token_hex(4), 'url': origin,
                        'description': 'Forge software factory' if single else 'Forge software factory — ' + role,
                        # The same App signs operators in when Tailscale identity is unavailable.
                        **({'callback_urls': [origin + '/auth/github/callback'], 'request_oauth_on_install': False}
                           if single else {}),
                        # GitHub requires a webhook URL even when delivery is disabled.
                        'public': False, 'hook_attributes': {'url': origin + '/github/events', 'active': False},
                        'redirect_url': origin + '/onboarding/github/callback',
                        'setup_url': origin + '/onboarding',
                        'default_permissions': APP_PERMISSIONS if single else ROLE_PERMISSIONS[role], 'default_events': []}
            return {'action': action + '?state=' + state, 'manifest': manifest}

    def callback(self, state, code, browser, *, owner_id=None):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', code) or not browser:
            raise ValueError('Invalid GitHub callback; return to setup and reconnect')
        # Consume the one-use challenge durably BEFORE exchanging the code.
        with self.store.transaction() as data:
            self.initialize(data); self.editable(data)
            challenge = data.get('challenge') or {}
            if (not hmac.compare_digest(challenge.get('state', ''), hashlib.sha256(state.encode()).hexdigest())
                    or not hmac.compare_digest(challenge.get('browser', ''), hashlib.sha256(browser.encode()).hexdigest())
                    or challenge.get('generation') != data['generation'] or challenge.get('expires', 0) <= self.clock()):
                raise ValueError('GitHub setup expired or belongs to another browser; reconnect from setup')
            del data['challenge']
            intent = {'role': challenge['role'], 'generation': data['generation'], 'owner': data['owner']}
        app = self.api().request('POST', '/app-manifests/' + code + '/conversions', None)
        if (not isinstance(app, dict) or type(app.get('id')) is not int or app['id'] <= 0
                or not re.fullmatch(r'[A-Za-z0-9-]+', app.get('slug', ''))
                or not isinstance(app.get('pem'), str) or 'PRIVATE KEY-----' not in app['pem']):
            raise ValueError('GitHub did not return a valid App; reconnect from setup')
        with self.store.transaction() as data:
            self.editable(data)
            if data['generation'] != intent['generation']:
                raise ValueError('This setup was reset; reconnect from the new setup')
            role = intent['role']
            ref = 'FORGE_SETUP_' + role.upper() + '_KEY'
            self.credentials.put(ref, app['pem'], file=True)
            connection = {'app_id': app['id'], 'private_key_env': ref}
            api = self.api()
            verified = api.request('GET', '/app', api.jwt(connection))
            if (verified.get('id') != app['id'] or verified.get('slug') != app['slug']
                    or verified.get('owner', {}).get('login', '').lower() != intent['owner'].lower()
                    or owner_id is not None and verified.get('owner', {}).get('id') != owner_id
                    or (missing_app_permissions(verified.get('permissions', {})) if role == APP
                        else missing_permissions(role, verified.get('permissions', {})))):
                raise ValueError('App owner or role permissions differ from setup; reconnect using the chosen owner')
            if any(a['connection']['app_id'] == app['id'] for a in data['apps'].values()):
                raise ValueError('This App is already registered; register the one Forge App only once')
            url = 'https://github.com/apps/' + app['slug'] + '/installations/new'
            data['apps'][role] = {'connection': connection, 'install_url': url}
            client_id, client_secret = app.get('client_id'), app.get('client_secret')
            if (role == APP and isinstance(client_id, str) and re.fullmatch(r'[A-Za-z0-9_.-]{5,100}', client_id)
                    and isinstance(client_secret, str) and re.fullmatch(r'[A-Za-z0-9_-]{20,200}', client_secret)):
                # "Login with GitHub" through the same App: the operator sign-in
                # fallback. Only a reference is kept in setup state.
                self.credentials.put('FORGE_SETUP_APP_SIGN_IN_SECRET', client_secret)
                data['apps'][role].update(client_id=client_id, sign_in_secret_env='FORGE_SETUP_APP_SIGN_IN_SECRET')
            data.update(checks={}, error=None)
            return url

    def refresh(self):
        with self.store.transaction() as data:
            self.initialize(data); self.editable(data)
            api, settings = self.api(), self.settings()
            config = settings.load()
            for name, app in data['apps'].items():
                connection = app['connection']
                installations = api.request('GET', '/app/installations?per_page=100', api.jwt(connection))
                if not isinstance(installations, list) or len(installations) >= 100:
                    raise ValueError('Installation inventory is not bounded; check the App on GitHub')
                matches = [i for i in installations if i.get('app_id') == connection['app_id']
                           and i.get('account', {}).get('login', '').lower() == data['owner'].lower()
                           and not i.get('suspended_at')]
                # The one Forge App's installation is every role's connection.
                for role in (ROLES if name == APP else (name,)):
                    if len(matches) == 1:
                        config['connections'][role] = {**connection, 'installation_id': matches[0]['id']}
                    else:
                        config['connections'].pop(role, None)
            settings.save(config, digest(settings.load()))
            data['error'] = None
        settings.refresh()
        return self.view()

    def select(self, repo_id):
        with self.store.transaction() as data:
            self.initialize(data); self.editable(data)
            settings = self.settings()
            repo = next((r for r in settings.view()['repositories'] if r['id'] == repo_id and r['access_ready']), None)
            if not repo:
                raise ValueError('Refresh GitHub access and choose a repository the Forge App can reach')
            config = settings.load()
            selected = next((r for r in config['repositories'] if r['id'] == repo_id), {})
            # Selecting the first repository changes activation, not the saved
            # policies of the other repositories prepared by the installer.
            config['repositories'] = [
                {**row, 'enabled': False} for row in config['repositories'] if row['id'] != repo_id
            ] + [{**selected, 'id': repo['id'], 'name': repo['name'], 'enabled': True}]
            settings.save(config, digest(settings.load()))
            data.update(selected={'id': repo['id'], 'name': repo['name']}, checks={}, repository_check=None, error=None)
            data.pop('readiness', None)

    def harnesses(self, data=None):
        data = data or self.read()
        config = self.active_runtime() if data['phase'] == 'active' else self.runtime()
        pool = config['provider_pool']
        # Legacy single-provider setup remains readable during migration.
        if not pool and data.get('provider_revision'):
            pool = [{'id': config['provider'], **{k: config[k] for k in ('provider', 'model')}}]
        health = {}
        if data['phase'] == 'active':
            health = self.provider_health()
        def auth(entry):
            # How the account is held decides whether its jobs may run in parallel.
            names = manifest(entry['provider'])
            if entry.get('provider_auth_file_env'):
                return 'login'
            if names.get('token_env') in (entry.get('provider_environment') or []):
                return 'token'
            return 'api_key'

        def status(entry):
            cooldown = health.get(entry['provider'], {})
            if cooldown.get('until', 0) > self.clock():
                return status_label(cooldown)
            if data['phase'] == 'active' and config.get('provider_ready') is not None and entry['id'] not in config['provider_ready']:
                return 'verification required'
            return data.get('provider_checks', {}).get(entry['id'], 'connected')
        return [{'id': key, 'name': item['name'], 'version': item['version'],
                 'connected': any(e['provider'] == key for e in pool),
                 'models': [{'id': e['id'], 'model': e['model'],
                             'status': status(e), 'auth': auth(e)}
                            for e in pool if e['provider'] == key],
                 'token_command': item.get('token_command'),
                 'token_login_command': shlex.join([str(Path(__file__).resolve().parents[1] / 'bin/forge'),
                                                    'setup', 'login', key, '--token', '--root', str(self.root)]) if item.get('token_env') else None,
                 'login_command': shlex.join([str(Path(__file__).resolve().parents[1] / 'bin/forge'),
                                              'setup', 'login', key, '--root', str(self.root)]),
                 'check_command': shlex.join([str(Path(__file__).resolve().parents[1] / 'bin/forge'),
                                              'setup', 'check', '--root', str(self.root)])}
                for key, item in manifests().items()]

    def connect(self, provider, *, api_key=None, token=None, auth_value=None, models=None):
        """Called by terminal setup. The caller serializes active runtime edits.

        A browser login is a rotating credential file with one writer, so its
        jobs run one at a time. An API key or a long-lived token travels as an
        environment variable that never rotates, so its jobs run in parallel.
        """
        item = manifest(provider)
        if token and not item.get('token_env'):
            raise ValueError(item['name'] + ' has no long-lived token login; use the browser login or an API key')
        models = models or [None]
        with self.store.transaction() as data:
            self.initialize(data)
            if data['phase'] not in ('setup', 'active') or data.get('operation'):
                raise ValueError('Finish the current setup operation before connecting a harness')
            active = data['phase'] == 'active'
            config = self.active_runtime() if active else self.runtime()
            pool = config['provider_pool']
            if not pool and data.get('provider_revision'):
                pool = [{'id': config['provider'], **{k: config[k] for k in
                         ('provider', 'model', 'provider_environment', 'provider_auth_file_env')}}]
            environment, reference, secret = [], None, api_key or token
            if secret:
                if not isinstance(secret, str) or any(c.isspace() for c in secret) or len(secret) < 16:
                    raise ValueError('Enter a valid provider API key' if api_key else 'Enter a valid long-lived token')
                environment = [item['api_key_env'] if api_key else item['token_env']]
            else:
                if not isinstance(auth_value, str) or not isinstance(json.loads(auth_value), dict):
                    raise ValueError('No valid harness login found; sign in again')
                reference = item['auth_reference']
            entries = [{'id': provider + ('-' + str(i + 1) if len(models) > 1 else ''), 'provider': provider,
                        'model': model, 'provider_environment': environment, 'provider_auth_file_env': reference}
                       for i, model in enumerate(models)]
            config['provider_pool'] = [e for e in pool if e['provider'] != provider] + entries
            if active or config.get('provider_ready') is not None:
                ready = config.get('provider_ready')
                config['provider_ready'] = [e['id'] for e in pool if e['provider'] != provider and (ready is None or e['id'] in ready)]
            runtime_config(config)  # Validate BEFORE persisting credentials or configuration.
            self.credentials.put(reference or environment[0], auth_value if reference else secret, file=bool(reference))
            self.save_runtime(config, active=active)
            data.update(provider_revision=str(uuid.uuid4()), checks={}, provider_checks={}, provider_errors={}, error=None)
            data.pop('readiness', None)

    def scope(self, harness):
        """Every pool entry of one harness; the rest keeps its recorded verdict."""
        item = manifest(harness)
        identifiers = [entry['id'] for entry in self.runtime()['provider_pool'] if entry['provider'] == harness]
        if not identifiers:
            raise ValueError('Connect ' + item['name'] + ' before checking it')
        return identifiers

    def queue(self, action, revision=None, generation=None, harness=None):
        if action not in ('check', 'harness_check', 'start', 'reset'):
            raise ValueError('Unknown setup action')
        if harness is not None and action != 'harness_check':
            raise ValueError('Only coding account verification can be limited to one harness')
        with self.store.transaction() as data:
            self.initialize(data)
            if action == 'reset' and generation != data['generation']:
                receipt = data.get('reset_receipt') or {}
                if generation and receipt.get('generation') == generation:
                    return receipt['id']
                raise ValueError('This reset belongs to an earlier setup; reload before resetting the current factory')
            if action == 'start' and data['phase'] == 'active' and revision == data.get('activation_revision'):
                return data['activation_request']
            operation = data.get('operation')
            scope = self.scope(harness) if harness else None
            if operation and operation['action'] == action and (operation.get('providers') or None) == scope:
                return operation['id']
            if action != 'reset':
                if not (action == 'harness_check' and data['phase'] == 'active' and not data.get('operation')):
                    self.editable(data)
                if revision != self.revision(data):
                    raise ValueError('Setup changed; reload before continuing')
                if (action != 'harness_check' and not data.get('selected')) or not data.get('provider_revision'):
                    raise ValueError('Choose a repository and connect the coding provider first')
                ready = data.get('readiness') or {}
                if action == 'start' and (ready.get('revision') != revision or not 0 <= self.clock() - ready.get('at', 0) < 300):
                    raise ValueError('Run readiness checks again before starting')
            else:
                # Fence persists even if the process crashes before its intent is saved.
                self.reset_fence(data)
                data['phase'] = 'resetting'
            image = None
            if action == 'harness_check':
                # The runner probes are repeated only for an unverified image.
                image = (data.get('qualification') or {}).get('image')
                data.pop('qualification', None)
            if action in ('check', 'harness_check'):
                data.pop('readiness', None)
            op = {'id': str(uuid.uuid4()), 'action': action, 'generation': data['generation'],
                  'revision': revision, 'step': 'queued', 'at': self.clock()}
            if scope:
                op.update(harness=harness, providers=scope, image=image)
            data.update(operation=op, error=None)
            return op['id']
