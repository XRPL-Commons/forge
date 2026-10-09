"""Verified HTTPS and OS-keyring connection lifecycle; no server imports."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from urllib.error import HTTPError

from .contracts import SetupError, require, repository, positive

PATHS = {'device', 'token', 'refresh', 'status', 'disconnect', 'requirements', 'authorize', 'read', 'operation', 'policy', 'planning', 'delivery'}
CAPABILITIES = ('repositories:read', 'repositories:prepare', 'repositories:activate', 'repositories:policy', 'repositories:triage')
TOKEN = re.compile(r'[A-Za-z0-9_-]{43}')


def origin(value):
    require(isinstance(value, str), 'invalid_server', 'Choose a verified HTTPS Forge address.')
    parsed = urlsplit(value)
    require(parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password
            and parsed.path in ('', '/') and not parsed.query and not parsed.fragment
            and re.fullmatch(r'[a-z0-9.-]+(?::[0-9]{1,5})?', parsed.netloc),
            'invalid_server', 'Choose a canonical HTTPS Forge address.')
    return 'https://' + parsed.netloc


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise SetupError('redirect_rejected', 'Forge client requests cannot follow redirects.')


class Transport:
    def __init__(self, base):
        self.base = origin(base)
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def call(self, operation, value, token=None):
        require(operation in PATHS)
        body = json.dumps(value, allow_nan=False).encode()
        require(len(body) <= 16384)
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json', 'User-Agent': 'Forge-Client/0.1'}
        if token:
            require(isinstance(token, str) and TOKEN.fullmatch(token))
            headers['Authorization'] = 'Bearer ' + token
        request = Request(self.base + '/api/client/' + operation, data=body, method='POST', headers=headers)
        try:
            try:
                response = self.opener.open(request, timeout=20)
            except HTTPError as exc:
                response = exc
            with response:
                raw = response.read(1024 * 1024 + 1)
                require(len(raw) <= 1024 * 1024)
                result = json.loads(raw)
                require(isinstance(result, dict))
                if response.status != 200 or result.get('ok') is not True:
                    code = result.get('error')
                    allowed = {'authorization_pending', 'slow_down', 'access_denied', 'expired_token', 'invalid_grant',
                               'invalid_token', 'sign_in_required', 'setup_required', 'insufficient_scope', 'scope_changed',
                               'connection_changed', 'operation_conflict', 'operation_capacity', 'revision_changed', 'repository_not_configured'}
                    message = {'revision_changed':'Runtime settings changed. Read the current policy and make a new decision.',
                               'repository_not_configured':'Configure this repository in Forge before changing its merge policy.'}.get(code,
                               'Forge did not authorize this client request.')
                    raise SetupError(code if code in allowed else 'connection_failed', message)
                return result
        except SetupError:
            raise
        except Exception:
            raise SetupError('connection_failed', 'Could not contact the verified Forge HTTPS endpoint.') from None


class Vault:
    """Reject plaintext/null/third-party fallbacks instead of silently saving secrets."""
    def __init__(self):
        try:
            import keyring
            backend = keyring.get_keyring()
            if type(backend).__module__ == 'keyring.backends.chainer':
                choices = backend.backends
            else:
                choices = [backend]
            self.backend = next(b for b in choices if type(b).__module__ in (
                'keyring.backends.macOS', 'keyring.backends.SecretService', 'keyring.backends.libsecret'))
        except Exception:
            raise SetupError('credential_store_unavailable', 'Unlock a supported macOS Keychain or Linux Secret Service credential store.') from None

    def get(self, key):
        try:
            value = self.backend.get_password('Forge client', key)
            return json.loads(value) if value else None
        except Exception:
            raise SetupError('credential_store_unavailable', 'The OS credential store could not be read.') from None

    def set(self, key, value):
        try:
            self.backend.set_password('Forge client', key, json.dumps(value, allow_nan=False))
        except Exception:
            raise SetupError('credential_store_unavailable', 'The OS credential store could not be updated. Reconnect if token rotation completed.') from None

    def delete(self, key):
        try:
            if self.backend.get_password('Forge client', key) is not None:
                self.backend.delete_password('Forge client', key)
        except Exception:
            raise SetupError('credential_store_unavailable', 'The OS credential store could not be updated.') from None


@contextmanager
def connection_lock(key, root=None):
    directory = Path(root) if root else Path.home() / '.local/state/forge-client'
    require(not directory.is_symlink(), 'invalid_storage', 'Client state must be a private directory.')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    info = directory.stat()
    require(info.st_uid == os.getuid() and not info.st_mode & 0o077, 'invalid_storage', 'Client state must be private.')
    fd = os.open(directory / (key + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('connection_busy', 'Another client command is using this connection.') from None
        yield


def public_connection(value, base, instance=None, generation=None):
    keys = {'id', 'name', 'operator', 'capabilities', 'scope', 'created', 'expires', 'idle_expires',
            'last_used', 'instance', 'generation', 'origin', 'status'}
    require(isinstance(value, dict) and set(value) == keys)
    require(value['origin'] == base and isinstance(value['instance'], str) and TOKEN.fullmatch(value['instance']))
    require(isinstance(value['generation'], str) and 0 < len(value['generation']) <= 128)
    require(instance is None or instance == value['instance'], 'connection_changed', 'Forge instance changed. Connect again.')
    require(generation is None or generation == value['generation'], 'connection_changed', 'Forge generation changed. Connect again.')
    require(isinstance(value['id'], str) and re.fullmatch(r'[A-Za-z0-9_-]{24}', value['id']))
    require(isinstance(value['name'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', value['name']))
    require(isinstance(value['operator'], dict) and set(value['operator']) == {'id', 'login'} and positive(value['operator']['id'])
            and isinstance(value['operator']['login'], str) and re.fullmatch(r'[A-Za-z0-9-]{1,39}', value['operator']['login']))
    require(isinstance(value['capabilities'], list) and value['capabilities'] and all(c in CAPABILITIES for c in value['capabilities']))
    validate_scope(value['scope'])
    require(all(type(value[k]) in (int, float) and 0 <= value[k] < 2**53 for k in ('created', 'expires', 'idle_expires')))
    require(value['last_used'] is None or type(value['last_used']) in (int, float) and 0 <= value['last_used'] < 2**53)
    require(value['status'] in ('active', 'revoked', 'expired'))
    return value


def validate_scope(value):
    require(isinstance(value, dict) and set(value) == {'repositories', 'owners'})
    require(isinstance(value['repositories'], list) and isinstance(value['owners'], list)
            and 0 < len(value['repositories']) + len(value['owners']) <= 100)
    for repo in value['repositories']:
        repository(repo)
    for owner in value['owners']:
        require(isinstance(owner, dict) and set(owner) == {'id', 'login'} and positive(owner['id'])
                and isinstance(owner['login'], str) and re.fullmatch(r'[A-Za-z0-9-]{1,39}', owner['login']))
    return value


class Client:
    def __init__(self, base, *, vault=None, transport=None, clock=time.time, sleep=time.sleep):
        self.base = origin(base)
        self.key = hashlib.sha256(self.base.encode()).hexdigest()
        self.vault = vault or Vault()
        self.transport = transport or Transport(base)
        self.clock, self.sleep = clock, sleep

    def begin(self, name, capabilities, scope):
        require(self.vault.get(self.key) is None, 'already_connected', 'Resume or disconnect the existing connection first.')
        validate_scope(scope)
        value = self.transport.call('device', {'client_id': 'forge-cli', 'name': name, 'capabilities': capabilities, 'scope': scope})
        require(set(value) == {'ok', 'device_code', 'user_code', 'verification_uri', 'verification_uri_complete', 'expires_in', 'interval', 'instance', 'generation'})
        require(isinstance(value['device_code'], str) and TOKEN.fullmatch(value['device_code']))
        require(isinstance(value['user_code'], str) and re.fullmatch(r'[A-Z2-9]{4}-[A-Z2-9]{4}', value['user_code']))
        require(value['verification_uri'] == self.base + '/settings/agents'
                and value['verification_uri_complete'] == value['verification_uri'] + '?code=' + value['user_code'])
        require(type(value['expires_in']) is int and 0 < value['expires_in'] <= 600 and value['interval'] == 5)
        require(isinstance(value['instance'], str) and TOKEN.fullmatch(value['instance'])
                and isinstance(value['generation'], str) and 0 < len(value['generation']) <= 128)
        self.vault.set(self.key, {'state': 'pending', 'base': self.base, **value,
            'deadline': self.clock() + value['expires_in'], 'next_poll': self.clock() + value['interval'],
            'requested_capabilities': capabilities, 'requested_scope': scope})
        return {k: value[k] for k in ('user_code', 'verification_uri_complete', 'expires_in')}

    def _saved(self):
        value = self.vault.get(self.key)
        require(isinstance(value, dict) and value.get('base') == self.base, 'not_connected', 'Connect this Forge server first.')
        return value

    def _accept(self, result, saved):
        require(set(result) == {'ok', 'access_token', 'refresh_token', 'token_type', 'expires_in', 'connection'})
        require(result['token_type'] == 'Bearer' and result['expires_in'] == 900)
        for key in ('access_token', 'refresh_token'):
            require(isinstance(result[key], str) and TOKEN.fullmatch(result[key]))
        connection = public_connection(result['connection'], self.base, saved['instance'], saved['generation'])
        allowed_caps = saved.get('requested_capabilities', saved.get('connection', {}).get('capabilities', []))
        allowed_scope = saved.get('requested_scope', saved.get('connection', {}).get('scope', {}))
        require(set(connection['capabilities']) <= set(allowed_caps))
        require(all(item in allowed_scope.get(kind, []) for kind in ('repositories', 'owners') for item in connection['scope'][kind]))
        value = {'state': 'connected', 'base': self.base, 'instance': saved['instance'], 'generation': saved['generation'],
                 'access_token': result['access_token'], 'refresh_token': result['refresh_token'],
                 'access_expires': self.clock() + result['expires_in'], 'connection': connection}
        if 'apps' in saved:
            value['apps'] = saved['apps']
        self.vault.set(self.key, value)
        return value

    def resume(self, wait=0):
        require(type(wait) is int and 0 <= wait <= 600)
        saved = self._saved()
        if saved['state'] == 'connected':
            return self.status()
        deadline = min(saved['deadline'], self.clock() + wait)
        while True:
            if self.clock() >= saved['deadline']:
                self.vault.delete(self.key)
                raise SetupError('expired_token', 'Connection request expired. Start again.')
            delay = max(0, saved['next_poll'] - self.clock())
            if self.clock() + delay > deadline:
                return {'status': 'authorization_pending', 'user_code': saved['user_code'],
                        'verification_uri_complete': saved['verification_uri_complete']}
            if delay:
                self.sleep(delay)
            try:
                result = self.transport.call('token', {'client_id': 'forge-cli', 'device_code': saved['device_code']})
                return self._accept(result, saved)['connection']
            except SetupError as error:
                if error.code not in ('authorization_pending', 'slow_down'):
                    if error.code in ('access_denied', 'expired_token'):
                        self.vault.delete(self.key)
                    raise
                if error.code == 'slow_down':
                    saved['interval'] += 5
                saved['next_poll'] = self.clock() + saved['interval']
                self.vault.set(self.key, saved)
                if self.clock() >= deadline:
                    return {'status': 'authorization_pending', 'user_code': saved['user_code'],
                            'verification_uri_complete': saved['verification_uri_complete']}

    def _credential(self):
        saved = self._saved()
        require(saved['state'] == 'connected', 'authorization_pending', 'Approve this connection in the browser first.')
        if saved['access_expires'] <= self.clock() + 30:
            result = self.transport.call('refresh', {'refresh_token': saved['refresh_token']})
            saved = self._accept(result, saved)
        return saved

    def status(self):
        saved = self._credential()
        result = self.transport.call('status', {}, saved['access_token'])
        require(result.pop('ok', None) is True)
        return public_connection(result, self.base, saved['instance'], saved['generation'])

    def disconnect(self):
        saved = self._saved()
        if saved['state'] == 'connected':
            try:
                saved = self._credential()
                self.transport.call('disconnect', {}, saved['access_token'])
            except SetupError as error:
                if error.code not in ('invalid_token', 'invalid_grant'):
                    raise
        self.vault.delete(self.key)
        return {'status': 'disconnected'}

    def requirements(self, target):
        from .contracts import requirements, connection_identity
        repository(target)
        saved = self._credential()
        value = self.transport.call('requirements', {'repository': target, 'instance': saved['instance'], 'generation': saved['generation']}, saved['access_token'])
        require(value.pop('ok', None) is True)
        value = requirements(value)
        require(value['repository']['id'] == target['id'] and value['repository']['name'].lower() == target['name'].lower(),
                'repository_identity_changed', 'Forge returned a different repository. Resolve the target again.')
        apps = connection_identity(value)
        require('apps' not in saved or saved['apps'] == apps, 'app_identity_changed',
                'Forge App identities changed. Review the server configuration and reconnect before granting access.')
        if 'apps' not in saved:
            saved['apps'] = apps
            self.vault.set(self.key, saved)
        return value

    def request(self, operation, target, **extra):
        repository(target)
        saved = self._credential()
        value = self.transport.call(operation, {'repository': target, 'instance': saved['instance'],
            'generation': saved['generation'], **extra}, saved['access_token'])
        require(value.pop('ok', None) is True)
        return value

    def authorize(self, instance, generation, target, capability):
        saved = self._saved()
        require((instance, generation) == (saved['instance'], saved['generation']),
                'connection_changed', 'Connect to the current Forge instance again.')
        require(self.request('authorize', target, capability=capability) == {'authorized': True})

    def read(self, target, section='repository', number=None, page=1, *, after=''):
        return self.request('read', target, section=section, number=number, page=page, **({'after': after} if section == 'delivery' else {}))
