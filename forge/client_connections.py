"""Browser-approved client grants. No GitHub administrative credentials enter here."""
from copy import deepcopy
import hmac
import re
import secrets
from urllib.parse import urlencode

from .operator_auth import OperatorAuth, digest
from .state import StateError
from forge_client.contracts import positive, repository

CAPABILITIES = ('repositories:read', 'repositories:prepare', 'repositories:activate')
CLIENT_ID = 'forge-cli'
ACCESS_SECONDS = 900
IDLE_SECONDS = 30 * 86400
ABSOLUTE_SECONDS = 90 * 86400
DEVICE_SECONDS = 600
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')
CODE = re.compile(r'[A-Z2-9]{4}-[A-Z2-9]{4}')


class ConnectionError(ValueError):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.status = code, status


def need(condition, code='invalid_request', message='Invalid client connection request.', status=400):
    if not condition:
        raise ConnectionError(code, message, status)


def fields(value, keys):
    need(isinstance(value, dict) and set(value) == set(keys))


def capabilities(value):
    need(isinstance(value, list) and value and all(isinstance(x, str) and x in CAPABILITIES for x in value))
    need(len(value) == len(set(value)) and 'repositories:read' in value)
    return sorted(value)


def scope(value):
    fields(value, ('repositories', 'owners'))
    repos, owners = value['repositories'], value['owners']
    need(isinstance(repos, list) and isinstance(owners, list) and 0 < len(repos) + len(owners) <= 100)
    for repo in repos:
        repository(repo)
    for owner in owners:
        fields(owner, ('id', 'login'))
        need(positive(owner['id']) and isinstance(owner['login'], str) and re.fullmatch(r'[A-Za-z0-9-]{1,39}', owner['login']))
    need(len({r['id'] for r in repos}) == len(repos) and len({o['id'] for o in owners}) == len(owners))
    return deepcopy(value)


def active_generation(root):
    from pathlib import Path
    from .onboarding import Onboarding
    root = Path(root)
    need((root / 'state/factory-v2.enabled').exists() and not (root / 'state/factory-paused.json').exists(),
         'setup_required', 'Finish initial setup or reset before connecting an agent.', 409)
    state = Onboarding(root).store.read()
    need(state.get('phase') == 'active' and isinstance(state.get('generation'), str),
         'setup_required', 'Finish initial setup before connecting an agent.', 409)
    return state['generation']


def verify_owner(owner):
    from .app_access import AppClient
    value = AppClient().request('GET', '/users/' + owner['login'], None)
    need(value.get('id') == owner['id'] and value.get('login', '').lower() == owner['login'].lower(),
         'scope_changed', 'GitHub owner identity changed. Start a new connection.', 409)


def target_identity(root, target):
    """Fresh metadata, not cached inventory or a client-asserted owner ID."""
    from .settings import Settings
    from .factory.github import GitHub, GitHubUnavailable
    settings = Settings(root)
    try:
        value = GitHub(settings, {}).call('controller', {'repo': target['name'], 'repo_id': target['id']}, 'GET', '')
    except (GitHubUnavailable, KeyError):
        value = settings.client.request('GET', '/repos/' + target['name'], None)
    need(value.get('id') == target['id'] and value.get('full_name', '').lower() == target['name'].lower(),
         'scope_changed', 'Repository identity changed.', 403)
    owner = value.get('owner') or {}
    need(positive(owner.get('id')), 'scope_unverified', 'Repository owner could not be verified.', 403)
    return owner['id']


class ClientConnections:
    def __init__(self, root, *, auth=None, generation=None, owner_check=verify_owner, identity=None):
        self.root = root
        self.auth = auth or OperatorAuth(root)
        self.store, self.clock = self.auth.store, self.auth.clock
        self.generation = generation or (lambda: active_generation(root))
        self.owner_check = owner_check
        self.identity = identity or (lambda target: target_identity(root, target))

    def _context(self, data, generation):
        config = self.auth._config(data)
        need(config is not None, 'sign_in_required', 'Configure HTTPS operator sign-in before connecting an agent.', 403)
        for key in ('devices', 'clients'):
            data.setdefault(key, {})
            if not isinstance(data[key], dict):
                raise StateError('Invalid client authorization state')
        data.setdefault('instance_id', secrets.token_urlsafe(32))
        return {'instance': data['instance_id'], 'generation': generation, 'epoch': data['epoch'], 'origin': config['origin']}

    def _operator(self, data, token):
        self.auth._config(data)
        session = data['sessions'].get(digest(token))
        need(session and session['expires'] > self.clock() and session['operator']['id'] in data['config']['operators'],
             'sign_in_required', 'Sign in as an authorized operator.', 401)
        return session

    def _valid(self, data, grant, context):
        return (not grant.get('revoked') and all(grant.get(k) == context[k] for k in ('instance', 'generation', 'epoch'))
                and grant['operator']['id'] in data['config']['operators']
                and grant['expires'] > self.clock() and grant['idle_expires'] > self.clock())

    def _prune(self, data):
        now = self.clock()
        data['devices'] = {key: value for key, value in data['devices'].items() if value['expires'] > now}
        data['clients'] = {key: value for key, value in data['clients'].items() if value['expires'] > now}

    def begin(self, value):
        fields(value, ('client_id', 'name', 'capabilities', 'scope'))
        need(value['client_id'] == CLIENT_ID)
        need(isinstance(value['name'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', value['name']))
        requested, targets = capabilities(value['capabilities']), scope(value['scope'])
        generation = self.generation()
        device = secrets.token_urlsafe(32)
        with self.store.transaction() as data:
            context = self._context(data, generation)
            self._prune(data)
            window = data.get('device_window', {'start': 0, 'count': 0})
            if self.clock() - window['start'] >= 60:
                window = {'start': self.clock(), 'count': 0}
            need(window['count'] < 20 and len(data['devices']) < 100, 'slow_down', 'Too many connection requests. Try again later.', 429)
            window['count'] += 1
            data['device_window'] = window
            alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
            while True:
                text = ''.join(secrets.choice(alphabet) for _ in range(8))
                code = text[:4] + '-' + text[4:]
                if all(d['user_code'] != code for d in data['devices'].values()):
                    break
            data['devices'][digest(device)] = {**context, 'user_code': code, 'name': value['name'],
                'capabilities': requested, 'scope': targets, 'expires': self.clock() + DEVICE_SECONDS,
                'interval': 5, 'next_poll': self.clock() + 5, 'status': 'pending'}
        uri = context['origin'] + '/settings/agents'
        return {'device_code': device, 'user_code': code, 'verification_uri': uri,
                'verification_uri_complete': uri + '?' + urlencode({'code': code}),
                'expires_in': DEVICE_SECONDS, 'interval': 5, 'instance': context['instance'], 'generation': generation}

    def _find_device(self, data, code, context):
        need(isinstance(code, str) and CODE.fullmatch(code))
        device = next((d for d in data['devices'].values() if hmac.compare_digest(d['user_code'], code)), None)
        need(device and device['expires'] > self.clock() and device['status'] == 'pending'
             and all(device.get(k) == context[k] for k in ('instance', 'generation', 'epoch')),
             'expired_token', 'This code expired or has already been used.', 409)
        return device

    def preview(self, session_token, code):
        generation = self.generation()
        with self.store.transaction() as data:
            context = self._context(data, generation)
            operator = self._operator(data, session_token)
            # Only signed-in operators can look up a 40-bit display code.
            key = str(operator['operator']['id'])
            windows = data.setdefault('connection_lookups', {})
            window = windows.get(key, {'start': 0, 'count': 0})
            if self.clock() - window['start'] >= 60:
                window = {'start': self.clock(), 'count': 0}
            window['count'] += 1
            windows[key] = window
            error = None
            try:
                need(window['count'] <= 20, 'slow_down', 'Too many code lookups. Try again later.', 429)
                device = self._find_device(data, code, context)
                result = {k: deepcopy(device[k]) for k in ('name', 'user_code', 'capabilities', 'scope', 'expires', 'origin')}
            except ConnectionError as exc:
                error = exc
        if error:
            raise error
        return result

    def decide(self, session_token, value):
        fields(value, ('code', 'decision', 'capabilities', 'scope', 'confirmed'))
        need(value['decision'] in ('approve', 'deny') and value['confirmed'] is True)
        selected, targets = capabilities(value['capabilities']), scope(value['scope'])
        # Resolve stable owner identities using the server's fixed GitHub API.
        if value['decision'] == 'approve':
            for owner in targets['owners']:
                self.owner_check(owner)
        generation = self.generation()
        with self.store.transaction() as data:
            context = self._context(data, generation)
            session = self._operator(data, session_token)
            device = self._find_device(data, value['code'], context)
            need(set(selected) <= set(device['capabilities']) and all(r in device['scope']['repositories'] for r in targets['repositories'])
                 and all(o in device['scope']['owners'] for o in targets['owners']), 'invalid_scope', 'Approval cannot expand the requested access.')
            self.auth.audit(session, '/api/connections/' + value['decision'])
            if value['decision'] == 'deny':
                device['status'] = 'denied'
                return {'status': 'denied'}
            self._prune(data)
            need(len(data['clients']) < 256, 'capacity_reached', 'Revoke expired or unused connections first.', 409)
            identifier = secrets.token_urlsafe(18)
            data['clients'][identifier] = {**context, 'id': identifier, 'name': device['name'],
                'operator': deepcopy(session['operator']), 'capabilities': selected, 'scope': targets,
                'created': self.clock(), 'expires': self.clock() + ABSOLUTE_SECONDS,
                'idle_expires': self.clock() + IDLE_SECONDS, 'last_used': None, 'revoked': None,
                'access_hash': '', 'access_expires': 0, 'refresh_hash': '', 'used_refresh': []}
            device.update(status='approved', grant_id=identifier)
        return {'status': 'approved', 'id': identifier}

    def _issue(self, grant):
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        grant.update(access_hash=digest(access), access_expires=self.clock() + ACCESS_SECONDS,
                     refresh_hash=digest(refresh), idle_expires=min(grant['expires'], self.clock() + IDLE_SECONDS),
                     last_used=self.clock())
        return {'token_type': 'Bearer', 'access_token': access, 'refresh_token': refresh,
                'expires_in': ACCESS_SECONDS, 'connection': self._public(grant, 'active')}

    def poll(self, value):
        fields(value, ('client_id', 'device_code'))
        need(value['client_id'] == CLIENT_ID and isinstance(value['device_code'], str) and TOKEN.fullmatch(value['device_code']))
        generation = self.generation()
        result = None
        with self.store.transaction() as data:
            context = self._context(data, generation)
            device = data['devices'].get(digest(value['device_code']))
            error = None
            if not device or device['expires'] <= self.clock() or any(device.get(k) != context[k] for k in ('instance', 'generation', 'epoch')):
                error = 'expired_token'
            elif self.clock() < device['next_poll']:
                device['interval'] += 5
                device['next_poll'] = self.clock() + device['interval']
                error = 'slow_down'
            else:
                device['next_poll'] = self.clock() + device['interval']
                if device['status'] == 'pending':
                    error = 'authorization_pending'
                elif device['status'] == 'denied':
                    error = 'access_denied'
                elif device['status'] == 'consumed':
                    # A replay cannot mint another token family.
                    error = 'expired_token'
                else:
                    grant = data['clients'].get(device['grant_id'])
                    if not grant or not self._valid(data, grant, context):
                        error = 'access_denied'
                    else:
                        result = self._issue(grant)
                        device['status'] = 'consumed'
        if error:
            raise ConnectionError(error, 'Client authorization has not completed.' if error == 'authorization_pending' else 'Client authorization cannot complete.')
        return result

    def refresh(self, token):
        need(isinstance(token, str) and TOKEN.fullmatch(token))
        generation = self.generation()
        result = None
        with self.store.transaction() as data:
            context = self._context(data, generation)
            hashed = digest(token)
            grant = next((g for g in data['clients'].values() if hashed == g['refresh_hash'] or hashed in g['used_refresh']), None)
            if grant and hashed in grant['used_refresh']:
                grant['revoked'] = 'refresh_replay'
            elif grant and self._valid(data, grant, context):
                need(self.clock() >= grant.get('next_refresh', 0), 'slow_down', 'Wait before rotating this credential again.', 429)
                grant['next_refresh'] = self.clock() + 5
                if len(grant['used_refresh']) >= 10000:
                    grant['revoked'] = 'rotation_limit'
                else:
                    grant['used_refresh'].append(hashed)
                    result = self._issue(grant)
        need(result is not None, 'invalid_grant', 'Connection expired or was revoked. Connect again.', 401)
        return result

    def _bearer(self, data, token, context):
        need(isinstance(token, str) and TOKEN.fullmatch(token), 'invalid_token', 'Invalid client credential.', 401)
        grant = next((g for g in data['clients'].values() if hmac.compare_digest(g['access_hash'], digest(token))), None)
        need(grant and self._valid(data, grant, context) and grant['access_expires'] > self.clock(),
             'invalid_token', 'Connection expired or was revoked. Connect again.', 401)
        return grant

    def _public(self, grant, status):
        return {**{k: deepcopy(grant[k]) for k in ('id', 'name', 'operator', 'capabilities', 'scope', 'created', 'expires', 'idle_expires', 'last_used', 'instance', 'generation', 'origin')}, 'status': status}

    def status(self, token):
        generation = self.generation()
        with self.store.transaction() as data:
            context = self._context(data, generation)
            grant = self._bearer(data, token, context)
            grant['last_used'] = self.clock()
            return self._public(grant, 'active')

    def authorize(self, token, target, capability, instance, generation):
        repository(target)
        need(capability in CAPABILITIES)
        actual_generation = self.generation()
        data = self.store.read()
        context = self._context(data, actual_generation)
        grant = self._bearer(data, token, context)
        need(instance == context['instance'] and generation == actual_generation,
             'connection_changed', 'Forge instance or generation changed.', 409)
        need(capability in grant['capabilities'], 'insufficient_scope', 'This connection does not allow that action.', 403)
        selected = any(r['id'] == target['id'] and r['name'].lower() == target['name'].lower() for r in grant['scope']['repositories'])
        if not selected:
            owners = grant['scope']['owners']
            need(bool(owners), 'insufficient_scope', 'Repository is outside this connection.', 403)
            owner_id = self.identity(target)
            need(any(owner['id'] == owner_id for owner in owners), 'insufficient_scope', 'Repository owner is outside this connection.', 403)
        # Recheck revocation after any GitHub call. This is also required at every actual effect.
        self.status(token)
        return self._public(grant, 'active')

    def view(self, session_token):
        generation = self.generation()
        data = self.store.read()
        context = self._context(data, generation)
        self._operator(data, session_token)
        return {'enabled': True, 'connections': [self._public(g, 'active' if self._valid(data, g, context) else 'revoked' if g['revoked'] else 'expired') for g in data['clients'].values()]}

    def revoke(self, session_token, identifier):
        need(isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]{24}', identifier))
        with self.store.transaction() as data:
            session = self._operator(data, session_token)
            grant = data.get('clients', {}).get(identifier)
            need(grant is not None, 'not_found', 'Connection not found.', 404)
            self.auth.audit(session, '/api/connections/revoke')
            grant['revoked'] = 'operator_revoked'

    def disconnect(self, token):
        generation = self.generation()
        with self.store.transaction() as data:
            context = self._context(data, generation)
            self._bearer(data, token, context)['revoked'] = 'client_disconnected'
