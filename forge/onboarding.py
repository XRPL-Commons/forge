"""Staged setup and durable Start/Reset intent. Only the supervisor applies intent."""
from copy import deepcopy
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from pathlib import Path

from .app_access import AppClient, ROLE_PERMISSIONS, missing_permissions
from .credentials import Credentials
from .settings import Settings, digest, read_json, write_json
from .state import AtomicStore
from .factory.config import validate as runtime_config

ROLES = tuple(ROLE_PERMISSIONS)


class Onboarding:
    def __init__(self, root, *, clock=time.time, client=None):
        self.root = Path(root)
        self.clock = clock
        self.credentials = Credentials(root)
        self.client = client
        self.store = AtomicStore(self.root / 'state' / 'onboarding', 'setup')

    def api(self):
        return self.client or AppClient(environ=self.credentials.environment())

    def settings(self):
        return Settings(self.root / 'setup', client=self.api(), clock=self.clock)

    def initialize(self, data):
        if not data:
            active = (self.root / 'state/factory-v2.enabled').exists()
            legacy = any((self.root / 'repos.d').glob('*.repo'))
            data.update(version=1, generation=str(uuid.uuid4()), phase='active' if active else 'legacy' if legacy else 'setup',
                        apps={}, checks={}, probe_jobs=[], operation=None, error=None)

    def read(self):
        with self.store.transaction() as data:
            self.initialize(data)
            return deepcopy(data)

    def runtime(self):
        path = self.root / 'setup/runtime.json'
        host = self.root / 'host-runtime.json'
        config = read_json(path) if path.exists() else read_json(host) if host.exists() else {}
        return runtime_config({'max_running': 1, 'slots': {'build': 2, 'review': 1, 'audit': 1, 'recovery': 1}, **config,
                               'enabled': False, 'merge_enabled': False})

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
        return {'phase': data['phase'], 'generation': data['generation'], 'revision': revision,
                'account_type': data.get('account_type'), 'reset_receipt': data.get('reset_receipt'),
                'roles': {role: {'status': settings['roles'][role]['status'],
                                 'registered': role in data['apps'],
                                 'app_id': data['apps'].get(role, {}).get('connection', {}).get('app_id'),
                                 'install_url': data['apps'].get(role, {}).get('install_url')} for role in ROLES},
                'repositories': [r for r in settings['repositories'] if r['access_ready']],
                'selected': data.get('selected'), 'owner': data.get('owner', ''),
                'provider_connected': bool(data.get('provider_revision')), 'checks': data['checks'],
                'can_start': data['phase'] == 'setup' and ready.get('revision') == revision
                             and 0 <= self.clock() - ready.get('at', 0) < 300 and not data.get('operation'),
                'operation': data.get('operation'), 'error': data.get('error')}

    @staticmethod
    def editable(data):
        if data['phase'] != 'setup' or data.get('operation'):
            raise ValueError('Finish the current operation, or reset the existing factory before setting up again')

    def begin(self, role, owner, account_type, origin, browser):
        if role not in ROLES or not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?', owner):
            raise ValueError('Choose a role and a GitHub account name')
        if account_type not in ('organization', 'personal') or not browser:
            raise ValueError('Choose a GitHub account type')
        with self.store.transaction() as data:
            self.initialize(data); self.editable(data)
            if data.get('owner') and (data['owner'].lower() != owner.lower() or data['account_type'] != account_type):
                raise ValueError('Use the same GitHub owner for all roles; reset to change owners')
            if role in data['apps']:
                return {'install_url': data['apps'][role]['install_url']}
            state = secrets.token_urlsafe(32)
            data.update(owner=owner, account_type=account_type, error=None)
            data['challenge'] = {'state': hashlib.sha256(state.encode()).hexdigest(),
                                 'browser': hashlib.sha256(browser.encode()).hexdigest(), 'role': role,
                                 'generation': data['generation'], 'expires': self.clock() + 3600}
            action = ('https://github.com/organizations/' + owner + '/settings/apps/new'
                      if account_type == 'organization' else 'https://github.com/settings/apps/new')
            manifest = {'name': 'Forge ' + role + ' ' + secrets.token_hex(4), 'url': origin,
                        'description': 'Forge software factory — ' + role,
                        # GitHub requires a webhook URL even when delivery is disabled.
                        'public': False, 'hook_attributes': {'url': origin + '/github/events', 'active': False},
                        'redirect_url': origin + '/onboarding/github/callback',
                        'setup_url': origin + '/onboarding',
                        'default_permissions': ROLE_PERMISSIONS[role], 'default_events': []}
            return {'action': action + '?state=' + state, 'manifest': manifest}

    def callback(self, state, code, browser):
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
                    or missing_permissions(role, verified.get('permissions', {}))):
                raise ValueError('App owner or role permissions differ from setup; reconnect using the chosen owner')
            if any(a['connection']['app_id'] == app['id'] for a in data['apps'].values()):
                raise ValueError('Each role must have its own App')
            url = 'https://github.com/apps/' + app['slug'] + '/installations/new'
            data['apps'][role] = {'connection': connection, 'install_url': url}
            data.update(checks={}, error=None)
            return url

    def refresh(self):
        with self.store.transaction() as data:
            self.initialize(data); self.editable(data)
            api, settings = self.api(), self.settings()
            config = settings.load()
            for role, app in data['apps'].items():
                connection = app['connection']
                installations = api.request('GET', '/app/installations?per_page=100', api.jwt(connection))
                if not isinstance(installations, list) or len(installations) >= 100:
                    raise ValueError('Installation inventory is not bounded; check the App on GitHub')
                matches = [i for i in installations if i.get('app_id') == connection['app_id']
                           and i.get('account', {}).get('login', '').lower() == data['owner'].lower()
                           and not i.get('suspended_at')]
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
                raise ValueError('Refresh GitHub access and choose a repository available to all four Apps')
            config = settings.load()
            config['repositories'] = [{'id': repo['id'], 'name': repo['name'], 'enabled': True}]
            settings.save(config, digest(settings.load()))
            data.update(selected={'id': repo['id'], 'name': repo['name']}, checks={}, error=None)
            data.pop('readiness', None)

    def provider(self, api_key=None):
        with self.store.transaction() as data:
            self.initialize(data); self.editable(data)
            config = self.runtime()
            env = self.credentials.environment()
            value = api_key or env.get('OPENAI_API_KEY')
            if value:
                if not isinstance(value, str) or any(c.isspace() for c in value) or len(value) < 16:
                    raise ValueError('Enter a valid provider API key')
                self.credentials.put('OPENAI_API_KEY', value)
                config.update(provider_environment=['OPENAI_API_KEY'], provider_auth_file_env=None)
            else:
                path = Path(env.get('FORGE_CODEX_AUTH_FILE') or Path.home() / '.codex/auth.json')
                if not path.is_file() or path.stat().st_size > 65536:
                    raise ValueError('No server connection found. Enter an OpenAI API key to connect')
                value = path.read_text()
                if not isinstance(json.loads(value), dict):
                    raise ValueError('Provider connection is invalid; sign in again on the server')
                self.credentials.put('FORGE_SETUP_PROVIDER_AUTH', value, file=True)
                config.update(provider_environment=[], provider_auth_file_env='FORGE_SETUP_PROVIDER_AUTH')
            write_json(self.root / 'setup/runtime.json', config)
            data.update(provider_revision=str(uuid.uuid4()), checks={}, error=None)
            data.pop('readiness', None)

    def queue(self, action, revision=None, generation=None):
        if action not in ('check', 'start', 'reset'):
            raise ValueError('Unknown setup action')
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
            if operation and operation['action'] == action:
                return operation['id']
            if action != 'reset':
                self.editable(data)
                if revision != self.revision(data):
                    raise ValueError('Setup changed; reload before continuing')
                if not data.get('selected') or not data.get('provider_revision'):
                    raise ValueError('Choose a repository and connect the coding provider first')
                ready = data.get('readiness') or {}
                if action == 'start' and (ready.get('revision') != revision or not 0 <= self.clock() - ready.get('at', 0) < 300):
                    raise ValueError('Run readiness checks again before starting')
            else:
                # Fence persists even if the process crashes before its intent is saved.
                write_json(self.root / 'state/factory-paused.json', {'generation': data['generation']})
                data['phase'] = 'resetting'
            op = {'id': str(uuid.uuid4()), 'action': action, 'generation': data['generation'],
                  'revision': revision, 'step': 'queued', 'at': self.clock()}
            data.update(operation=op, error=None)
            return op['id']
