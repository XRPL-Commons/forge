"""Operator identity only. Never accepts the local CLI's GitHub credentials.

Absent configuration preserves existing private-network installations. Once
configured, missing/corrupt state fails closed, including after process restart.
"""
from __future__ import annotations

import argparse
import base64
import fcntl
import getpass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from forge.state import AtomicStore, StateError

SESSION_COOKIE = '__Host-forge_session'
FLOW_COOKIE = '__Host-forge_oauth'
SESSION_SECONDS = 8 * 3600
FLOW_SECONDS = 600


class AuthError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def cookie(name, value='', age=0):
    return f'{name}={value}; Path=/; HttpOnly; Secure; SameSite=Lax; Max-Age={age}'


def read_cookie(header, name):
    from http.cookies import SimpleCookie, CookieError
    try:
        jar = SimpleCookie(header)
        value = jar[name].value if name in jar else ''
        return value if re.fullmatch(r'[A-Za-z0-9_-]{43}', value) else ''
    except CookieError:
        return ''


def configuration(origin, client_id, client_secret, operators):
    if not isinstance(origin, str):
        raise AuthError('Invalid HTTPS origin')
    parsed = urlsplit(origin)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.path or parsed.query or parsed.fragment or parsed.netloc != parsed.netloc.lower()
            or not re.fullmatch(r'[a-z0-9.-]+(?::[0-9]{1,5})?', parsed.netloc)):
        raise AuthError('Operator sign-in requires a canonical HTTPS origin without a path')
    if not isinstance(client_id, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{5,100}', client_id):
        raise AuthError('Invalid GitHub OAuth client ID')
    if not isinstance(client_secret, str) or not re.fullmatch(r'[A-Za-z0-9_-]{20,200}', client_secret):
        raise AuthError('Invalid GitHub OAuth client secret')
    if not isinstance(operators, list) or not operators or len(operators) > 100 or any(type(i) is not int or i <= 0 for i in operators):
        raise AuthError('Configure explicit numeric GitHub operator IDs')
    return {'origin': origin, 'client_id': client_id, 'client_secret': client_secret,
            'operators': sorted(set(operators))}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise AuthError('GitHub identity request redirected')


def github_identity(config, code, verifier):
    """Fixed HTTPS endpoints, bounded replies, no token persistence or logging."""
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(url, payload=None, token=None):
        headers = {'Accept': 'application/json', 'User-Agent': 'Forge-Operator-Sign-In'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
            headers['X-GitHub-Api-Version'] = '2022-11-28'
        if payload is not None:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        try:
            with opener.open(Request(url, data=urlencode(payload).encode() if payload is not None else None,
                                     headers=headers), timeout=10) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise AuthError('GitHub identity response too large')
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise AuthError('Invalid GitHub identity response')
                return result
        except Exception:
            # HTTPError bodies/URLs can contain credentials. Never expose them.
            raise AuthError('GitHub identity verification failed') from None

    result = request('https://github.com/login/oauth/access_token', {
        'client_id': config['client_id'], 'client_secret': config['client_secret'],
        'code': code, 'code_verifier': verifier,
        'redirect_uri': config['origin'] + '/auth/github/callback'})
    token = result.get('access_token')
    # A dedicated identity-only OAuth App must never grant administrative scopes.
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,256}', token)
            or result.get('scope') != '' or result.get('token_type') != 'bearer'):
        raise AuthError('Use a dedicated GitHub OAuth App with no requested scopes')
    identity = request('https://api.github.com/user', token=token)
    return {'id': identity.get('id'), 'login': identity.get('login')}


class OperatorAuth:
    def __init__(self, root, *, clock=time.time, identity=github_identity):
        directory = Path(root) / 'state/operator-auth'
        if directory.is_symlink():
            raise StateError('Operator state directory must not be a symlink')
        self.store = AtomicStore(directory, 'auth')
        self.clock = clock
        self.identity = identity

    def _config(self, data):
        for path in (self.store.root, self.store.path):
            if path.exists():
                info = path.lstat()
                if info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise StateError('Operator authorization state must be private')
        if not data:
            if self.store.marker.exists():
                raise StateError('Operator authorization state is empty')
            return None
        if (data.get('version') != 1 or not isinstance(data.get('config'), dict)
                or not isinstance(data.get('sessions'), dict) or not isinstance(data.get('flows'), dict)
                or not isinstance(data.get('epoch'), str)):
            raise StateError('Invalid operator authorization state')
        return configuration(**data['config'])

    def config(self):
        return self._config(self.store.read())

    def configure(self, origin, client_id, client_secret, operators):
        config = configuration(origin, client_id, client_secret, operators)
        with self.store.transaction() as data:
            instance = data.get('instance_id', secrets.token_urlsafe(32))
            data.clear()
            data.update(instance_id=instance, version=1, config=config, epoch=secrets.token_urlsafe(32), sessions={}, flows={})

    def _prune(self, data):
        now = self.clock()
        for field in ('sessions', 'flows'):
            data[field] = {key: value for key, value in data[field].items() if value['expires'] > now}

    def begin(self, return_to='/'):
        if return_to != '/' and not re.fullmatch(r'/settings/agents(?:\?code=[A-Z2-9]{4}-[A-Z2-9]{4})?', return_to):
            raise AuthError('Invalid sign-in destination')
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with self.store.transaction() as data:
            config = self._config(data)
            if not config:
                raise AuthError('Operator sign-in is not configured')
            self._prune(data)
            if len(data['flows']) >= 100:
                raise AuthError('Too many sign-in attempts; try again later')
            data['flows'][digest(state)] = {'browser': digest(browser), 'verifier': verifier,
                                           'expires': self.clock() + FLOW_SECONDS, 'return_to': return_to}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        url = 'https://github.com/login/oauth/authorize?' + urlencode({
            'client_id': config['client_id'], 'redirect_uri': config['origin'] + '/auth/github/callback',
            'state': state, 'scope': '', 'code_challenge': challenge, 'code_challenge_method': 'S256',
            'allow_signup': 'false', 'prompt': 'select_account'})
        return url, cookie(FLOW_COOKIE, browser, FLOW_SECONDS)

    def callback(self, state, code, browser, previous='', *, with_target=False):
        if (not re.fullmatch(r'[A-Za-z0-9_-]{43}', state) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', browser)
                or not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', code)):
            raise AuthError('Invalid sign-in callback')
        with self.store.transaction() as data:
            config = self._config(data)
            if not config:
                raise AuthError('Operator sign-in is not configured')
            self._prune(data)
            flow = data['flows'].get(digest(state))
            if not flow or not hmac.compare_digest(flow['browser'], digest(browser)):
                raise AuthError('Sign-in expired or belongs to another browser')
            del data['flows'][digest(state)]
            epoch = data['epoch']
        # Consume the browser-bound state before any network operation.
        identity = self.identity(config, code, flow['verifier'])
        if (type(identity.get('id')) is not int or identity['id'] not in config['operators']
                or not isinstance(identity.get('login'), str)
                or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', identity['login'])):
            raise AuthError('This GitHub account is not an authorized Forge operator')
        token = secrets.token_urlsafe(32)
        with self.store.transaction() as data:
            self._config(data)
            if data['epoch'] != epoch:
                raise AuthError('Operator authorization changed; sign in again')
            self._prune(data)
            if len(data['sessions']) >= 1000:
                raise AuthError('Too many active sessions')
            data['sessions'].pop(digest(previous), None)
            data['sessions'][digest(token)] = {'operator': identity, 'csrf': secrets.token_urlsafe(32),
                                              'expires': self.clock() + SESSION_SECONDS}
        result = cookie(SESSION_COOKIE, token, SESSION_SECONDS)
        return (result, flow.get('return_to', '/')) if with_target else result

    def session(self, token):
        data = self.store.read()
        config = self._config(data)
        if not config:
            return None
        session = data['sessions'].get(digest(token)) if token else None
        if session and session['expires'] > self.clock() and session['operator']['id'] in config['operators']:
            return session
        return None

    def logout(self, token):
        with self.store.transaction() as data:
            if not self._config(data):
                raise AuthError('Operator sign-in is not configured')
            session = data['sessions'].pop(digest(token), None)
            if session:
                for grant in data.get('clients', {}).values():
                    if grant['operator']['id'] == session['operator']['id']:
                        grant['revoked'] = 'operator_signed_out'

    def audit(self, session, route):
        """Record admission before mutation, without request bodies or credentials.

        This records an authorized request, not proof of successful execution.
        Full outcome/ledger correlation and external retention remain issue #77.
        """
        event = {'time': self.clock(), 'event': 'operator_request', 'operator': session['operator'], 'route': route}
        fd = os.open(self.store.root / 'requests.jsonl', os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(event, sort_keys=True) + '\n')
            stream.flush()
            os.fsync(stream.fileno())


def main():
    parser = argparse.ArgumentParser(description='Configure operator sign-in on this Forge host; also revokes all sessions.')
    parser.add_argument('--root', required=True)
    parser.add_argument('--origin', required=True, help='Canonical HTTPS address, without trailing slash')
    parser.add_argument('--client-id', required=True, help='Dedicated, identity-only GitHub OAuth App')
    parser.add_argument('--operator-id', action='append', required=True, type=int)
    args = parser.parse_args()
    secret = getpass.getpass('GitHub OAuth client secret (identity app only): ')
    try:
        OperatorAuth(args.root).configure(args.origin, args.client_id, secret, args.operator_id)
    except (ValueError, OSError, StateError):
        parser.exit(1, 'Unable to configure operator sign-in; check HTTPS origin, IDs, secret and private state.\n')
    print('Operator sign-in configured. All previous sessions revoked.')


if __name__ == '__main__':
    main()
