"""Operator identity only. Never accepts the local CLI's GitHub credentials.

Absent configuration preserves existing private-network installations. Once
configured, missing/corrupt state fails closed, including after process restart.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


from .sqlite_identity import active_in, audit_in, identity_effect_lock
from .store.db import _private, _new_file, _fsync_directory
from .store.files import StateError
from .store.identity import session_in
from .store.runtime import required

SESSION_COOKIE = '__Host-forge_session'
FLOW_COOKIE = '__Host-forge_oauth'
SESSION_SECONDS = 8 * 3600
FLOW_SECONDS = 600


class AuthError(ValueError):
    pass


def failure_reason(error):
    """A closed diagnostic vocabulary: never log upstream errors or credentials."""
    return {
        'Invalid callback': 'invalid_callback_parameters',
        'Invalid callback issuer': 'issuer_mismatch',
        'Invalid sign-in state': 'invalid_state',
        'Invalid sign-in code': 'invalid_code',
        'Missing or invalid sign-in cookie': 'missing_flow_cookie',
        'Sign-in expired or belongs to another browser': 'expired_or_mismatched_flow',
        'GitHub identity verification failed': 'github_exchange_failed',
        'Use a dedicated GitHub OAuth App with no requested scopes': 'github_token_rejected',
        'This GitHub account is not an authorized Forge operator': 'operator_not_authorized',
        'Operator authorization changed; sign in again': 'configuration_changed',
    }.get(str(error), 'internal_error')


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


def sign_in_destination(value):
    return isinstance(value, str) and (value == '/' or re.fullmatch(
        r'/settings/agents(?:\?code=[A-Z2-9]{4}-[A-Z2-9]{4})?|/onboarding/cli/github/start\?state=[A-Za-z0-9_-]{43}', value))


class OperatorAuth:
    def __init__(self, root, *, store=None, clock=time.time, identity=github_identity):
        store = store if store is not None else required(root)
        self.root, self.sqlite_store, self.store = Path(root), store, store
        self.clock, self.identity = clock, identity

    def _config_in(self, db):
        active_in(db)
        row = db.execute('SELECT * FROM operator_auth_configuration WHERE singleton=1').fetchone()
        if row is None:
            return None
        reference = row['credential_ref']
        if not re.fullmatch(r'credentials/operator-identity(?:-[a-f0-9]{64})?\.secret', reference):
            raise StateError('Invalid identity credential reference')
        path = self.sqlite_store.root / reference
        _private(path.parent, directory=True)
        _private(path)
        raw = path.read_bytes()
        if len(raw) > 200 or not hmac.compare_digest(hashlib.sha256(raw).hexdigest(), row['credential_digest']):
            raise StateError('Identity credential does not match its configured digest')
        operators = [value[0] for value in db.execute('SELECT github_id FROM operator_identities '
                     'WHERE enabled=1 AND authorization_epoch=? ORDER BY github_id', (row['authorization_epoch'],))]
        return configuration(row['origin'], row['client_id'], raw.decode(), operators)

    def config(self):
        with self.sqlite_store.read_snapshot() as db:
            return self._config_in(db)

    def configure(self, origin, client_id, client_secret, operators, *, if_unconfigured=False):
        config = configuration(origin, client_id, client_secret, operators)
        with identity_effect_lock(self.sqlite_store):
            with self.sqlite_store.read_snapshot() as db:
                active_in(db)
                if if_unconfigured and db.execute('SELECT 1 FROM operator_auth_configuration').fetchone():
                    raise AuthError('Operator sign-in was configured concurrently')
            raw = config['client_secret'].encode()
            hashed = hashlib.sha256(raw).hexdigest()
            directory = self.sqlite_store.root / 'credentials'
            directory.mkdir(mode=0o700, exist_ok=True)
            _private(directory, directory=True)
            reference = 'credentials/operator-identity-' + hashed + '.secret'
            path = self.sqlite_store.root / reference
            if path.exists():
                _private(path)
                if path.read_bytes() != raw:
                    raise StateError('Identity credential digest collision')
            else:
                _new_file(path, raw)
                _fsync_directory(directory)
            with self.sqlite_store.write() as db:
                active_in(db)
                old = db.execute('SELECT * FROM operator_auth_configuration').fetchone()
                if if_unconfigured and old:
                    raise AuthError('Operator sign-in was configured concurrently')
                epoch = old['authorization_epoch'] + 1 if old else 1
                instance = old['instance_id'] if old else self.sqlite_store.instance_id
                db.execute('INSERT INTO operator_auth_configuration VALUES(1,?,?,?,?,?,?,?) '
                    'ON CONFLICT(singleton) DO UPDATE SET origin=excluded.origin,client_id=excluded.client_id,'
                    'credential_ref=excluded.credential_ref,credential_digest=excluded.credential_digest,'
                    'source_epoch=excluded.source_epoch,authorization_epoch=excluded.authorization_epoch',
                    (origin, client_id, reference, hashed, instance, secrets.token_urlsafe(32), epoch))
                db.execute('UPDATE operator_identities SET enabled=0,authorization_epoch=?', (epoch,))
                for operator_id in config['operators']:
                    db.execute("INSERT INTO operator_identities(id,github_id,login,authorization_epoch,enabled) VALUES(?,?,'',?,1) "
                        'ON CONFLICT(github_id) DO UPDATE SET enabled=1,authorization_epoch=excluded.authorization_epoch',
                        (str(operator_id), operator_id, epoch))
                db.execute('UPDATE operator_sessions SET revoked_at=? WHERE revoked_at IS NULL', (self.clock(),))
                db.execute("UPDATE client_connections SET revoked_at=?,revocation_reason='authorization_changed' WHERE revoked_at IS NULL", (self.clock(),))
                db.execute('UPDATE operator_flows SET consumed_at=? WHERE consumed_at IS NULL', (self.clock(),))
                db.execute("UPDATE client_pairings SET status='denied' WHERE status IN ('pending','approved')")
                audit_in(db, self.clock, 'operator_auth_configured', details={'authorization_epoch': epoch})

    def begin(self, return_to='/'):
        if not sign_in_destination(return_to):
            raise AuthError('Invalid sign-in destination')
        state, browser, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        with self.sqlite_store.write() as db:
            config = self._config_in(db)
            if not config:
                raise AuthError('Operator sign-in is not configured')
            count = db.execute('SELECT count(*) FROM operator_flows WHERE consumed_at IS NULL AND expires_at>?', (self.clock(),)).fetchone()[0]
            if count >= 100:
                raise AuthError('Too many sign-in attempts; try again later')
            epoch = db.execute('SELECT source_epoch FROM operator_auth_configuration').fetchone()[0]
            db.execute('INSERT INTO operator_flows(id,state_hash,nonce_hash,return_path,created_at,expires_at,verifier,source_epoch) '
                       'VALUES(?,?,?,?,?,?,?,?)', (digest(state), digest(state), digest(browser), return_to,
                                                self.clock(), self.clock() + FLOW_SECONDS, verifier, epoch))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
        url = 'https://github.com/login/oauth/authorize?' + urlencode(dict(client_id=config['client_id'],
            redirect_uri=config['origin'] + '/auth/github/callback', state=state, scope='',
            code_challenge=challenge, code_challenge_method='S256', allow_signup='false', prompt='select_account'))
        return url, cookie(FLOW_COOKIE, browser, FLOW_SECONDS)

    def callback(self, state, code, browser, previous='', *, with_target=False):
        for value, pattern, message in ((state, r'[A-Za-z0-9_-]{43}', 'Invalid sign-in state'),
            (code, r'[A-Za-z0-9_-]{1,256}', 'Invalid sign-in code'),
            (browser, r'[A-Za-z0-9_-]{43}', 'Missing or invalid sign-in cookie')):
            if not isinstance(value, str) or not re.fullmatch(pattern, value):
                raise AuthError(message)
        with self.sqlite_store.write() as db:
            config = self._config_in(db)
            if not config:
                raise AuthError('Operator sign-in is not configured')
            context = dict(db.execute('SELECT * FROM operator_auth_configuration').fetchone())
            flow = db.execute('SELECT * FROM operator_flows WHERE state_hash=? AND consumed_at IS NULL AND expires_at>?',
                              (digest(state), self.clock())).fetchone()
            if (flow is None or flow['source_epoch'] != context['source_epoch']
                    or not hmac.compare_digest(flow['nonce_hash'], digest(browser))):
                raise AuthError('Sign-in expired or belongs to another browser')
            flow = dict(flow)
            db.execute('UPDATE operator_flows SET consumed_at=? WHERE id=?', (self.clock(), flow['id']))
        identity = self.identity(config, code, flow['verifier'])
        if (type(identity.get('id')) is not int or identity['id'] not in config['operators']
                or not isinstance(identity.get('login'), str) or not re.fullmatch(r'[A-Za-z0-9-]{1,39}', identity['login'])):
            raise AuthError('This GitHub account is not an authorized Forge operator')
        token = secrets.token_urlsafe(32)
        with self.sqlite_store.write() as db:
            active_in(db)
            current = db.execute('SELECT * FROM operator_auth_configuration').fetchone()
            if current is None or current['source_epoch'] != context['source_epoch']:
                raise AuthError('Operator authorization changed; sign in again')
            if db.execute('SELECT count(*) FROM operator_sessions WHERE revoked_at IS NULL AND expires_at>?', (self.clock(),)).fetchone()[0] >= 1000:
                raise AuthError('Too many active sessions')
            db.execute('UPDATE operator_sessions SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL', (self.clock(), digest(previous)))
            db.execute('UPDATE operator_identities SET login=? WHERE github_id=?', (identity['login'], identity['id']))
            db.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,authorization_epoch,created_at,expires_at,csrf,operator_login) '
                       'VALUES(?,?,?,?,?,?,?,?)', (digest(token), str(identity['id']), digest(token), current['authorization_epoch'],
                           self.clock(), self.clock() + SESSION_SECONDS, secrets.token_urlsafe(32), identity['login']))
            audit_in(db, self.clock, 'operator_signed_in', operator=identity)
        result = cookie(SESSION_COOKIE, token, SESSION_SECONDS)
        return (result, flow['return_path']) if with_target else result

    def session(self, token):
        with self.sqlite_store.read_snapshot() as db:
            if not self._config_in(db) or not token:
                return None
            return session_in(db, digest(token), self.clock())

    def logout(self, token):
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            if not self._config_in(db):
                raise AuthError('Operator sign-in is not configured')
            row = db.execute('SELECT s.*,i.github_id FROM operator_sessions s JOIN operator_identities i ON i.id=s.operator_id '
                             'WHERE token_hash=? AND revoked_at IS NULL', (digest(token),)).fetchone()
            if row:
                db.execute('UPDATE operator_sessions SET revoked_at=? WHERE id=?', (self.clock(), row['id']))
                db.execute("UPDATE client_connections SET revoked_at=?,revocation_reason='operator_signed_out' "
                           'WHERE operator_id=? AND revoked_at IS NULL', (self.clock(), row['operator_id']))
                audit_in(db, self.clock, 'operator_signed_out', operator={'id': row['github_id']})

    def audit(self, session, route):
        with self.sqlite_store.write() as db:
            active_in(db)
            from .mutation_audit import MutationAudit
            context = MutationAudit(self.sqlite_store, route, clock=self.clock)
            row = db.execute('SELECT id FROM operator_sessions WHERE operator_id=? AND csrf=?',
                             (str(session['operator']['id']), session['csrf'])).fetchone()
            context.bind(session=session, session_id=row[0] if row else None)
            from .mutation_audit import _insert
            _insert(db, self.clock, request_id=context.request_id, action=route, outcome='started',
                    details={**context.details, 'kind': 'request_started'}, operator=context.operator, session=context.session)
