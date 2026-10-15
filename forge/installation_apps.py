"""Operator-authenticated browser handoff initiated by administrative setup CLI.

GitHub registration stays in the operator's browser. Only the role App's own
credential reaches Forge, through the existing manifest conversion path.
"""
from copy import deepcopy
import hashlib
import hmac
import html
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlsplit

from .onboarding import APP, Onboarding, ROLES
from .operator_auth import OperatorAuth
from .store.runtime import required


class FreshAppConsent:
    """SSH-only bootstrap. Local manifest conversion supplies App credentials.

    This surface is never routed through HTTP. The installation RPC authenticates
    the administrator and pins the installation before reaching it.
    """
    def __init__(self, root, *, setup=None, auth=None):
        self.setup = setup or Onboarding(root)
        self.auth = auth or OperatorAuth(root)
        self.store = required(root)

    def call(self, action, binding, **parameters):
        from forge_client.contracts import fields, require
        from .operator_auth import configuration
        from .app_access import missing_app_permissions
        from .sqlite_identity import identity_effect_lock
        fields(binding, ('setup_id', 'owner', 'operators', 'origin', 'codex_parallel_auth', 'workflow_edits', 'repositories'))
        require(isinstance(binding['setup_id'], str) and re.fullmatch('[a-f0-9]{32}', binding['setup_id']))
        fields(binding['owner'], ('id', 'login', 'type'))
        owner = binding['owner']
        require(type(owner['id']) is int and owner['id'] > 0 and owner['type'] in ('User', 'Organization')
                and isinstance(owner['login'], str) and re.fullmatch('[A-Za-z0-9-]{1,39}', owner['login']))
        configuration(binding['origin'], 'validation', 'a' * 40, binding['operators'])
        require(type(binding['codex_parallel_auth']) is bool and type(binding['workflow_edits']) is bool)
        require(isinstance(binding['repositories'], list) and 1 <= len(binding['repositories']) <= 25)
        from forge_client.contracts import repository
        for target in binding['repositories']: repository(target)
        require(len({target['id'] for target in binding['repositories']}) == len(binding['repositories']))
        require(action in ('status', 'begin', 'import', 'refresh'))
        with identity_effect_lock(self.store):
            data = self.setup.read()
            saved = data.get('installation_consent')
            require(saved is None or saved['binding'] == binding, 'consent_changed', 'Resume the original installation consent.')
            if action == 'status':
                return self.view(binding)
            if action == 'refresh' and data['phase'] == 'active':
                fields(parameters, ())
                require(saved is not None and saved['generation'] == data['generation'], 'consent_changed')
                self.identity(binding)
                from .settings import Settings
                Settings(self.setup.root, scope='active').refresh()
                return self.view(binding)
            self.setup.editable(data)
            if action == 'begin':
                fields(parameters, ('callback',))
                callback = parameters['callback']
                require(isinstance(callback, str) and re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}/callback', callback))
                require(1 <= urlsplit(callback).port <= 65535)
                if saved:
                    require(saved['callback'] == callback, 'consent_changed')
                    return self.view(binding, registration=True)
                require(not data['apps'] and not self.auth.config(), 'existing_identity',
                        'This instance already has Apps or operator identity; migrate it or reset setup explicitly.')
                verified = self.setup.api().request('GET', '/users/' + owner['login'], None)
                require(all(verified.get(k) == owner[k] for k in ('id', 'type'))
                        and verified.get('login', '').lower() == owner['login'].lower(), 'owner_changed')
                registration = self.setup.begin(APP, owner['login'],
                    'organization' if owner['type'] == 'Organization' else 'personal', binding['origin'], secrets.token_urlsafe(32))
                registration['manifest']['redirect_url'] = callback
                if binding['workflow_edits']:
                    registration['manifest']['default_permissions'] = {
                        **registration['manifest']['default_permissions'], 'workflows': 'write'}
                with self.setup.store.transaction() as current:
                    current['installation_consent'] = {'binding': deepcopy(binding), 'callback': callback,
                        'generation': current['generation'], 'registration': registration}
                return self.view(binding, registration=True)
            require(saved is not None and saved['generation'] == data['generation'], 'consent_changed')
            if action == 'import':
                fields(parameters, ('app',))
                app = parameters['app']
                require(isinstance(app, dict) and type(app.get('id')) is int and app['id'] > 0
                        and isinstance(app.get('slug'), str) and re.fullmatch('[A-Za-z0-9-]+', app['slug'])
                        and isinstance(app.get('pem'), str) and 'PRIVATE KEY-----' in app['pem'], 'invalid_app')
                configuration(binding['origin'], app.get('client_id'), app.get('client_secret'), binding['operators'])
                existing = data['apps'].get(APP)
                require(existing is None or existing['connection']['app_id'] == app['id'], 'app_changed')
                identity = self.auth.config()
                require(identity is None or identity == configuration(binding['origin'], app['client_id'],
                        app['client_secret'], binding['operators']), 'identity_changed',
                        'Existing operator identity differs from this installation.')
                key = 'FORGE_SETUP_APP_KEY'
                # Use an isolated temporary reference for verification so an invalid
                # resume can never overwrite the configured App's key.
                probe = 'FORGE_SETUP_BOOTSTRAP_VERIFY_KEY'
                self.setup.credentials.put(probe, app['pem'], file=True)
                api = self.setup.api()
                verified = api.request('GET', '/app', api.jwt({'app_id': app['id'], 'private_key_env': probe}))
                require(verified.get('id') == app['id'] and verified.get('slug') == app['slug']
                        and verified.get('client_id') == app['client_id']
                        and verified.get('owner', {}).get('id') == owner['id']
                        and verified.get('owner', {}).get('login', '').lower() == owner['login'].lower()
                        and not missing_app_permissions(verified.get('permissions', {}))
                        and (not binding['workflow_edits'] or verified.get('permissions', {}).get('workflows') == 'write'),
                        'app_identity_changed')
                self.setup.credentials.put(key, app['pem'], file=True)
                self.setup.credentials.put('FORGE_SETUP_APP_SIGN_IN_SECRET', app['client_secret'])
                with self.setup.store.transaction() as current:
                    current['apps'][APP] = {'connection': {'app_id': app['id'], 'private_key_env': key},
                        'install_url': 'https://github.com/apps/' + app['slug'] + '/installations/new',
                        'client_id': app['client_id'], 'sign_in_secret_env': 'FORGE_SETUP_APP_SIGN_IN_SECRET'}
                    current.pop('challenge', None)
                    current.update(checks={}, error=None)
                self.identity(binding)
                runtime = self.setup.runtime()
                runtime['codex_parallel_auth'] = binding['codex_parallel_auth']
                self.setup.save_runtime(runtime, active=False)
            if action == 'refresh':
                fields(parameters, ())
                self.identity(binding)
                self.setup.refresh()
            return self.view(binding)

    def identity(self, binding):
        from forge_client.contracts import require
        app = self.setup.read()['apps'].get(APP)
        require(app is not None, 'app_required')
        secret = self.setup.credentials.environment(only=[app['sign_in_secret_env']])[app['sign_in_secret_env']]
        expected = {'origin': binding['origin'], 'client_id': app['client_id'],
                    'client_secret': secret, 'operators': sorted(set(binding['operators']))}
        current = self.auth.config()
        if current is None:
            self.auth.configure(**expected, if_unconfigured=True)
        else:
            require(current == expected, 'identity_changed', 'Existing operator identity differs from this installation.')

    def view(self, binding, *, registration=False):
        from forge_client.contracts import require
        data = self.setup.read()
        saved = data.get('installation_consent')
        require(saved is None or saved['generation'] == data['generation'], 'consent_changed')
        app = data['apps'].get(APP)
        result = {'status': 'consent_required', 'instance': self.store.instance_id, 'generation': data['generation']}
        if app and saved:
            config = self.auth.config()
            ready = config is not None and config['origin'] == binding['origin'] and config['client_id'] == app['client_id']
            ready = ready and config['operators'] == sorted(set(binding['operators']))
            from .settings import Settings
            settings = (Settings(self.setup.root, scope='active') if data['phase'] == 'active' else self.setup.settings())
            connections = settings.load()['connections']
            installed = all(connections.get(role, {}).get('app_id') == app['connection']['app_id']
                            and connections[role].get('installation_id') for role in ROLES)
            accessible = {row['id']: row for row in settings.view()['repositories'] if row['access_ready']}
            missing = [target for target in binding['repositories'] if target['id'] not in accessible
                       or accessible[target['id']]['name'].lower() != target['name'].lower()]
            installed = installed and not missing
            result.update(status='consent_ready' if ready and installed else 'app_installation_required',
                          app_id=app['connection']['app_id'], install_url=app['install_url'], identity_ready=ready,
                          missing_repositories=missing)
        elif saved and registration:
            result['registration'] = deepcopy(saved['registration'])
        return result


class RoleAppHandoff:
    def __init__(self, root, *, setup=None, auth=None, clock=time.time):
        self.setup = setup or Onboarding(root)
        self.auth = auth or OperatorAuth(root)
        self.store = required(root)
        self.clock = clock

    def operator(self, actor_id):
        config = self.auth.config()
        if type(actor_id) is not int or not config or actor_id not in config['operators']:
            raise ValueError('Use a configured Forge operator account')
        return config

    def begin(self, *, setup_id, actor_id, owner, role, generation):
        config = self.operator(actor_id)
        if (not isinstance(setup_id, str) or not re.fullmatch(r'[a-f0-9]{32}', setup_id)
                or role not in (APP, *ROLES) or not isinstance(owner, dict)
                or set(owner) != {'id', 'login', 'type'} or type(owner['id']) is not int or owner['id'] <= 0
                or owner['type'] not in ('User', 'Organization')
                or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', owner.get('login', ''))):
            raise ValueError('Choose the setup, role and verified GitHub owner')
        verified = self.setup.api().request('GET', '/users/' + owner['login'], None)
        if (verified.get('id') != owner['id'] or verified.get('type') != owner['type']
                or verified.get('login', '').lower() != owner['login'].lower()):
            raise ValueError('GitHub owner identity changed; inspect before continuing')
        current = self.setup.read()
        if current['generation'] != generation:
            raise ValueError('Setup changed; inspect before continuing')
        if role in current['apps']:
            return {'status': 'app_registered', 'role': role, 'install_url': current['apps'][role]['install_url']}
        existing = current.get('cli_app_handoff')
        binding = dict(setup_id=setup_id, actor_id=actor_id, owner=owner, role=role,
                       generation=generation, instance=self.store.instance_id, origin=config['origin'])
        if (existing and existing['binding'] == binding and existing['expires'] > self.clock()
                and (current.get('challenge') or {}).get('state') == existing['state_hash']):
            return self._public(existing)
        browser = secrets.token_urlsafe(32)
        registration = self.setup.begin(role, owner['login'], 'organization' if owner['type'] == 'Organization' else 'personal',
                                        config['origin'], browser)
        state = parse_qs(urlsplit(registration['action']).query)['state'][0]
        registration['manifest']['redirect_url'] = config['origin'] + '/onboarding/cli/github/callback'
        handoff = {'binding': binding, 'state': state, 'state_hash': hashlib.sha256(state.encode()).hexdigest(),
                   'browser': browser, 'expires': self.clock() + 600, 'registration': registration}
        with self.setup.store.transaction() as data:
            if data['generation'] != generation or (data.get('challenge') or {}).get('state') != handoff['state_hash']:
                raise ValueError('Setup changed while registration was prepared')
            data['cli_app_handoff'] = handoff
        return self._public(handoff)

    @staticmethod
    def _public(handoff):
        return {'status': 'app_registration_required', 'role': handoff['binding']['role'],
                'expires_at': handoff['expires'],
                'url': handoff['binding']['origin'] + '/onboarding/cli/github/start?state=' + handoff['state']}

    def _bound(self, state, actor_id):
        config = self.operator(actor_id)
        data = self.setup.read()
        handoff = data.get('cli_app_handoff') or {}
        binding = handoff.get('binding') or {}
        state_hash = hashlib.sha256(state.encode()).hexdigest() if isinstance(state, str) else ''
        if (not hmac.compare_digest(handoff.get('state_hash', ''), state_hash)
                or handoff.get('expires', 0) <= self.clock()
                or binding.get('actor_id') != actor_id or binding.get('generation') != data['generation']
                or binding.get('instance') != self.store.instance_id or binding.get('origin') != config['origin']
                or (data.get('challenge') or {}).get('state') != state_hash):
            raise ValueError('Registration expired or belongs to another setup or operator; resume the CLI')
        self.setup.editable(data)
        return deepcopy(handoff)

    def page(self, state, actor_id):
        handoff = self._bound(state, actor_id)
        registration = handoff['registration']
        escape = html.escape
        # No script or credential appears in this page. Submission goes directly to GitHub.
        return ('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
                '<title>Register a Forge GitHub App</title><body><main><h1>Register the '
                + escape(handoff['binding']['role'].title()) + ' App</h1><p>GitHub owner: '
                + escape(handoff['binding']['owner']['login']) + '. Approve registration on GitHub, then install this App '
                'on the repository you will use. Return to the CLI afterward.</p><form method="post" action="'
                + escape(registration['action'], quote=True) + '"><input type="hidden" name="manifest" value="'
                + escape(json.dumps(registration['manifest']), quote=True)
                + '"><button type="submit">Continue on GitHub</button></form></main></body></html>')

    def callback(self, state, code, actor_id):
        handoff = self._bound(state, actor_id)
        target = self.setup.callback(state, code, handoff['browser'], owner_id=handoff['binding']['owner']['id'])
        with self.setup.store.transaction() as data:
            current = data.get('cli_app_handoff') or {}
            if current.get('state_hash') == handoff['state_hash']:
                data.pop('cli_app_handoff', None)
        return target
