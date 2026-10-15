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


from . import tailnet_identity
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
        'GitHub sign-in must not grant scopes': 'github_token_rejected',
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
    # Identity only: the Forge App's user token (or a legacy identity-only OAuth
    # App's) must carry no scopes. It is used once here and never stored.
    if (not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{10,256}', token)
            or result.get('scope') != '' or result.get('token_type') != 'bearer'):
        raise AuthError('GitHub sign-in must not grant scopes')
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
            reference, hashed = self._store_secret(config['client_secret'])
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

    def _store_secret(self, secret):
        raw = secret.encode()
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
        return reference, hashed

    def rotate_client(self, client_id, client_secret):
        """Move "Login with GitHub" onto the Forge App without touching who may sign in.

        Unlike configure(), operators, epochs, sessions and agent grants are
        preserved: the same GitHub accounts sign in, only the client changes.
        In-flight sign-ins started with the old client are consumed. Replaying
        a finished rotation is a no-op, so the migration can resume."""
        with identity_effect_lock(self.sqlite_store):
            current = self.config()
            if not current:
                raise AuthError('Operator sign-in is not configured')
            configuration(current['origin'], client_id, client_secret, current['operators'])
            if current['client_id'] == client_id and hmac.compare_digest(current['client_secret'], client_secret):
                return {'status': 'unchanged'}
            reference, hashed = self._store_secret(client_secret)
            with self.sqlite_store.write() as db:
                active_in(db)
                before = db.execute('SELECT client_id FROM operator_auth_configuration WHERE singleton=1').fetchone()
                if before is None:
                    raise AuthError('Operator sign-in is not configured')
                db.execute('UPDATE operator_auth_configuration SET client_id=?,credential_ref=?,credential_digest=? WHERE singleton=1',
                           (client_id, reference, hashed))
                db.execute('UPDATE operator_flows SET consumed_at=? WHERE consumed_at IS NULL', (self.clock(),))
                audit_in(db, self.clock, 'operator_auth_client_rotated',
                         details={'client_changed': before['client_id'] != client_id})
        return {'status': 'rotated'}

    # Tailscale Serve identity -------------------------------------------------

    def _identity_in(self, db):
        row = db.execute("SELECT settings_json FROM configuration WHERE domain=? AND subject='instance' AND active=1",
                         (tailnet_identity.DOMAIN,)).fetchone()
        return tailnet_identity.validate(json.loads(row[0])) if row else json.loads(json.dumps(tailnet_identity.DEFAULT))

    def identity_settings(self):
        with self.sqlite_store.read_snapshot() as db:
            value = self._identity_in(db)
        return {'mode': value['mode'], 'tailnet_operators': [
            {'login': row['login'], 'github_id': row['github_id'], 'linked_at': row['linked_at']}
            for row in value['tailnet_operators']]}

    def _save_identity_in(self, db, value):
        from .store.configuration import Configuration
        row = db.execute("SELECT revision FROM configuration WHERE domain=? AND subject='instance' AND active=1",
                         (tailnet_identity.DOMAIN,)).fetchone()
        Configuration(self.sqlite_store, clock=self.clock).replace_in(
            db, tailnet_identity.DOMAIN, tailnet_identity.validate(value), expected_revision=row[0] if row else 0)

    def set_identity_mode(self, mode):
        """'tailscale' trusts Serve identity first; GitHub sign-in stays the fallback."""
        if mode not in tailnet_identity.MODES:
            raise AuthError('Choose github or tailscale operator identity')
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            if not self._config_in(db):
                raise AuthError('Operator sign-in is not configured')
            value = self._identity_in(db)
            if value['mode'] == mode:
                return {'mode': mode, 'changed': False}
            value['mode'] = mode
            if mode == 'tailscale':
                # A deliberate re-enable starts fresh sessions, including within
                # the window whose rows were revoked when leaving this mode.
                for link in value['tailnet_operators']:
                    link['nonce'] = secrets.token_urlsafe(32)
            self._save_identity_in(db, value)
            if mode == 'github':
                # Leaving Tailscale identity ends every session it created.
                db.execute("UPDATE operator_sessions SET revoked_at=? WHERE id LIKE 'tailnet:%' AND revoked_at IS NULL", (self.clock(),))
            audit_in(db, self.clock, 'operator_identity_mode', details={'mode': mode})
        return {'mode': mode, 'changed': True}

    def _link_in(self, db, value, tailnet_login, operator):
        rows = [row for row in value['tailnet_operators'] if row['login'] == tailnet_login]
        if rows:
            if rows[0]['github_id'] != operator['id']:
                raise AuthError('This Tailscale login is linked to another operator')
            return False
        value['tailnet_operators'].append({'login': tailnet_login, 'github_id': operator['id'],
                                           'nonce': secrets.token_urlsafe(32), 'linked_at': self.clock()})
        self._save_identity_in(db, value)
        audit_in(db, self.clock, 'tailnet_identity_linked', operator=operator)
        return True

    def link_tailnet(self, tailnet_login, github_id):
        normalized = tailnet_identity.login(tailnet_login)
        if normalized is None:
            raise AuthError('Use a Tailscale user login, not a tagged device')
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            config = self._config_in(db)
            if not config or github_id not in config['operators']:
                raise AuthError('This GitHub account is not an authorized Forge operator')
            return self._link_in(db, self._identity_in(db), normalized, {'id': github_id})

    def unlink_tailnet(self, tailnet_login):
        """Revoke a tailnet login now: its sessions end; the GitHub operator remains."""
        normalized = tailnet_identity.login(tailnet_login)
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            self._config_in(db)
            value = self._identity_in(db)
            rows = [row for row in value['tailnet_operators'] if row['login'] == normalized]
            if not rows:
                return {'unlinked': False}
            value['tailnet_operators'] = [row for row in value['tailnet_operators'] if row['login'] != normalized]
            self._save_identity_in(db, value)
            db.execute('UPDATE operator_sessions SET revoked_at=? WHERE id LIKE ? AND revoked_at IS NULL',
                       (self.clock(), self._tailnet_prefix(rows[0]) + '%'))
            audit_in(db, self.clock, 'tailnet_identity_unlinked', operator={'id': rows[0]['github_id']})
        return {'unlinked': True}

    @staticmethod
    def _tailnet_prefix(row):
        return 'tailnet:' + hashlib.sha256(row['nonce'].encode()).hexdigest()[:32] + ':'

    def _tailnet_in(self, db, normalized):
        config = self._config_in(db)
        value = self._identity_in(db)
        if not config or value['mode'] != 'tailscale':
            return None
        rows = [row for row in value['tailnet_operators'] if row['login'] == normalized]
        if not rows or rows[0]['github_id'] not in config['operators']:
            return None
        row = rows[0]
        context = db.execute('SELECT * FROM operator_auth_configuration WHERE singleton=1').fetchone()
        window = int(self.clock() // SESSION_SECONDS)
        key = hashlib.sha256(config['client_secret'].encode() + b'|' + context['source_epoch'].encode()).digest()
        message = f"{row['nonce']}|{row['github_id']}|{context['authorization_epoch']}|{window}".encode()
        token = base64.urlsafe_b64encode(hmac.new(key, message, hashlib.sha256).digest()).decode().rstrip('=')
        existing = db.execute('SELECT revoked_at FROM operator_sessions WHERE token_hash=?', (digest(token),)).fetchone()
        return dict(row=row, token=token, window=window, epoch=context['authorization_epoch'], existing=existing)

    def tailnet_session(self, tailnet_login):
        """(token, session) for a trusted Serve login, or None to fall back to GitHub.

        The token never leaves the server: it is derived per eight-hour window
        from the link's nonce, the operator's epoch and the sign-in credential,
        and a cookie can never present it (session() refuses tailnet rows).
        Reads stay read-only; only the first request of a window writes."""
        normalized = tailnet_identity.login(tailnet_login)
        if normalized is None:
            return None
        with self.sqlite_store.read_snapshot() as db:
            found = self._tailnet_in(db, normalized)
            if found is None or found['existing'] is not None and found['existing']['revoked_at'] is not None:
                return None
            if found['existing'] is not None:
                session = session_in(db, digest(found['token']), self.clock())
                return (found['token'], session) if session else None
        with self.sqlite_store.write() as db:
            found = self._tailnet_in(db, normalized)
            if found is None:
                return None
            token, row = found['token'], found['row']
            if found['existing'] is None:
                if db.execute('SELECT count(*) FROM operator_sessions WHERE revoked_at IS NULL AND expires_at>?',
                              (self.clock(),)).fetchone()[0] >= 1000:
                    raise AuthError('Too many active sessions')
                operator = db.execute('SELECT * FROM operator_identities WHERE github_id=?', (row['github_id'],)).fetchone()
                db.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,authorization_epoch,created_at,expires_at,csrf,operator_login) '
                           'VALUES(?,?,?,?,?,?,?,?)', (self._tailnet_prefix(row) + digest(token), operator['id'], digest(token),
                               found['epoch'], self.clock(), (found['window'] + 1) * SESSION_SECONDS,
                               secrets.token_urlsafe(32), operator['login'] or normalized))
                audit_in(db, self.clock, 'operator_signed_in', operator={'id': row['github_id']}, details={'via': 'tailscale'})
            session = session_in(db, digest(token), self.clock())
        return (token, session) if session else None

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

    def callback(self, state, code, browser, previous='', *, with_target=False, tailnet_login=None):
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
            linked = tailnet_identity.login(tailnet_login) if tailnet_login else None
            identity_value = self._identity_in(db)
            if linked and identity_value['mode'] == 'tailscale':
                # Signing in with GitHub through Serve proves both identities at
                # once: record the link (the first operator at bootstrap).
                try:
                    self._link_in(db, identity_value, linked, identity)
                except AuthError:
                    pass  # Linked to another operator: never re-point it implicitly.
            db.execute('INSERT INTO operator_sessions(id,operator_id,token_hash,authorization_epoch,created_at,expires_at,csrf,operator_login) '
                       'VALUES(?,?,?,?,?,?,?,?)', (digest(token), str(identity['id']), digest(token), current['authorization_epoch'],
                           self.clock(), self.clock() + SESSION_SECONDS, secrets.token_urlsafe(32), identity['login']))
            audit_in(db, self.clock, 'operator_signed_in', operator=identity)
        result = cookie(SESSION_COOKIE, token, SESSION_SECONDS)
        return (result, flow['return_path']) if with_target else result

    def session(self, token):
        """A cookie session. Tailscale sessions exist only with the trusted header."""
        with self.sqlite_store.read_snapshot() as db:
            if not self._config_in(db) or not token:
                return None
            session = session_in(db, digest(token), self.clock())
            return None if session is None or session['via'] != 'github' else session

    def logout(self, token):
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            if not self._config_in(db):
                raise AuthError('Operator sign-in is not configured')
            row = db.execute('SELECT s.*,i.github_id FROM operator_sessions s JOIN operator_identities i ON i.id=s.operator_id '
                             'WHERE token_hash=? AND revoked_at IS NULL', (digest(token),)).fetchone()
            if row and row['id'].startswith('tailnet:'):
                # Forget the proved link until explicit GitHub sign-in proves it
                # again. Revoking only this window would silently sign back in
                # on the next window while the browser still sends Serve identity.
                value = self._identity_in(db)
                links = [link for link in value['tailnet_operators']
                         if row['id'].startswith(self._tailnet_prefix(link))]
                for link in links:
                    db.execute('UPDATE operator_sessions SET revoked_at=? WHERE id LIKE ? AND revoked_at IS NULL',
                               (self.clock(), self._tailnet_prefix(link) + '%'))
                value['tailnet_operators'] = [link for link in value['tailnet_operators'] if link not in links]
                self._save_identity_in(db, value)
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
