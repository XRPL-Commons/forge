"""Live browser identity and agent grants backed by normalized SQLite rows.

No GitHub credential is written to the database. Credential files are immutable,
private, digest-checked references; a configuration commit selects one reference.
Remote identity and ownership checks run outside database transactions.
"""
from contextlib import contextmanager
from copy import deepcopy
import base64
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import time
import threading
import uuid
from pathlib import Path
from urllib.parse import urlencode

from .operator_auth import (AuthError, configuration, cookie, digest, github_identity, sign_in_destination,
                            FLOW_COOKIE, FLOW_SECONDS, SESSION_COOKIE, SESSION_SECONDS)
from .client_connections import (ConnectionError, need, fields, capabilities, scope, active_generation,
                                 verify_owner, target_identity, TOKEN, CODE, CAPABILITIES, CLIENT_ID,
                                 ACCESS_SECONDS, IDLE_SECONDS, ABSOLUTE_SECONDS, DEVICE_SECONDS)
from .state import StateError
from .store.db import _private, _new_file, _fsync_directory
from .store.identity import session_in, valid_connection_in
from .store.transitions import _json


def active_in(db):
    if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
        raise StateError('Staging imports cannot accept identity operations')


_locks = threading.local()


@contextmanager
def identity_effect_lock(root):
    """Serialize grant revocation and effects without a transaction spanning IO."""
    if hasattr(root, 'read_snapshot') and hasattr(root, 'root'):
        root = root.root
    else:
        from .store.runtime import selected
        selected_store = selected(root)
        root = selected_store.root if selected_store is not None else root
    root = Path(root).absolute()
    held = getattr(_locks, 'held', None)
    if held is None:
        held = _locks.held = set()
    key = str(root)
    if key in held:
        yield
        return
    fd = os.open(root / '.identity-effects.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise StateError('Identity effect lock must be a private owned regular file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        held.add(key)
        yield
    finally:
        held.discard(key)
        os.close(fd)


def audit_in(db, clock, action, *, operator=None, connection=None, request_id=None, outcome='applied', details=None):
    from .mutation_audit import current
    context = current()
    if context:
        context.details['effect_recorded'] = True
    db.execute('INSERT INTO operator_audit(operator_id,session_id,connection_id,request_id,action,outcome,at,details_json) '
               'VALUES(?,?,?,?,?,?,?,?)', (str(operator['id']) if operator else context.operator if context else None,
                 context.session if context else None, connection or (context.connection if context else None),
                 request_id or (context.request_id if context else str(uuid.uuid4())), action, outcome, clock(),
                 _json({'kind': 'identity_effect', **(details or {})})))


class SqliteOperatorAuth:
    def __init__(self, root, *, store, clock=time.time, identity=github_identity):
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


class SqliteClientConnections:
    def __init__(self, root, *, store, auth=None, generation=None, owner_check=verify_owner, identity=None):
        self.root, self.sqlite_store, self.store = root, store, store
        self.auth = auth or SqliteOperatorAuth(root, store=store)
        if not isinstance(self.auth, SqliteOperatorAuth):
            raise StateError('SQLite client grants require the selected identity authority')
        self.clock = self.auth.clock
        self.generation = generation or (lambda: active_generation(root))
        self.owner_check = owner_check
        self.identity = identity or (lambda target: target_identity(root, target))

    def _context_in(self, db, generation):
        need(self.auth._config_in(db) is not None, 'sign_in_required',
             'Configure HTTPS operator sign-in before connecting an agent.', 403)
        row = db.execute('SELECT * FROM operator_auth_configuration').fetchone()
        return dict(instance=row['instance_id'], generation=generation, epoch=row['source_epoch'],
                    origin=row['origin'], authorization_epoch=row['authorization_epoch'])

    def _operator_in(self, db, token):
        session = session_in(db, digest(token), self.clock()) if isinstance(token, str) else None
        need(session is not None, 'sign_in_required', 'Sign in as an authorized operator.', 401)
        return session

    def _public(self, row, status):
        return dict(id=row['id'], name=row['client_name'], operator={'id': int(row['operator_id']), 'login': row['operator_login']},
            capabilities=json.loads(row['scopes_json']), scope=json.loads(row['scope_json']), created=row['created_at'],
            expires=row['expires_at'], idle_expires=row['idle_expires'], last_used=row['last_used_at'],
            instance=row['instance_id'], generation=row['setup_generation'], origin=row['origin'], status=status)

    def _bearer_in(self, db, token, context):
        need(isinstance(token, str) and TOKEN.fullmatch(token), 'invalid_token', 'Invalid client credential.', 401)
        row = db.execute('SELECT * FROM client_connections WHERE access_hash=?', (digest(token),)).fetchone()
        need(valid_connection_in(db, row, context['generation'], self.clock()) and row['access_expires'] > self.clock(),
             'invalid_token', 'Connection expired or was revoked. Connect again.', 401)
        from .mutation_audit import current
        if current():
            current().bind(grant=self._public(row, 'active'))
        return dict(row)

    def _rate_in(self, db, kind, subject, *, increment=True):
        row = db.execute('SELECT * FROM auth_rate_limits WHERE kind=? AND subject=?', (kind, subject)).fetchone()
        start, count = (row['window_start'], row['request_count']) if row and self.clock() - row['window_start'] < 60 else (self.clock(), 0)
        if increment:
            count += 1
            db.execute('INSERT INTO auth_rate_limits VALUES(?,?,?,?) ON CONFLICT(kind,subject) '
                       'DO UPDATE SET window_start=excluded.window_start,request_count=excluded.request_count', (kind, subject, start, count))
        return count

    def begin(self, value):
        fields(value, ('client_id', 'name', 'capabilities', 'scope'))
        need(value['client_id'] == CLIENT_ID)
        need(isinstance(value['name'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', value['name']))
        requested, targets = capabilities(value['capabilities']), scope(value['scope'])
        generation, token = self.generation(), secrets.token_urlsafe(32)
        with self.sqlite_store.write() as db:
            context = self._context_in(db, generation)
            count = self._rate_in(db, 'device', 'instance', increment=False)
            active = db.execute('SELECT count(*) FROM client_pairings WHERE expires_at>?', (self.clock(),)).fetchone()[0]
            need(count < 20 and active < 100, 'slow_down', 'Too many connection requests. Try again later.', 429)
            self._rate_in(db, 'device', 'instance')
            alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
            while True:
                value_code = ''.join(secrets.choice(alphabet) for _ in range(8))
                code = value_code[:4] + '-' + value_code[4:]
                if not db.execute('SELECT 1 FROM client_pairings WHERE user_code=?', (code,)).fetchone():
                    break
            db.execute('INSERT INTO client_pairings(device_hash,user_code,instance_id,setup_generation,source_epoch,origin,client_name,'
                'capabilities_json,scope_json,expires_at,poll_interval,next_poll_at,status) VALUES(?,?,?,?,?,?,?,?,?,?,5,?,\'pending\')',
                (digest(token), code, context['instance'], generation, context['epoch'], context['origin'], value['name'],
                 _json(requested), _json(targets), self.clock() + DEVICE_SECONDS, self.clock() + 5))
        uri = context['origin'] + '/settings/agents'
        return dict(device_code=token, user_code=code, verification_uri=uri,
            verification_uri_complete=uri + '?' + urlencode({'code': code}), expires_in=DEVICE_SECONDS,
            interval=5, instance=context['instance'], generation=generation)

    def _find_device_in(self, db, code, context):
        need(isinstance(code, str) and CODE.fullmatch(code))
        row = db.execute('SELECT * FROM client_pairings WHERE user_code=?', (code,)).fetchone()
        need(row and row['expires_at'] > self.clock() and row['status'] == 'pending'
             and (row['instance_id'], row['setup_generation'], row['source_epoch']) ==
                 (context['instance'], context['generation'], context['epoch']),
             'expired_token', 'This code expired or has already been used.', 409)
        return dict(row)

    def preview(self, session_token, code):
        generation = self.generation()
        error = None
        with self.sqlite_store.write() as db:
            context = self._context_in(db, generation)
            session = self._operator_in(db, session_token)
            count = self._rate_in(db, 'lookup', str(session['operator']['id']))
            try:
                need(count <= 20, 'slow_down', 'Too many code lookups. Try again later.', 429)
                row = self._find_device_in(db, code, context)
                result = dict(name=row['client_name'], user_code=row['user_code'], capabilities=json.loads(row['capabilities_json']),
                              scope=json.loads(row['scope_json']), expires=row['expires_at'], origin=row['origin'])
            except ConnectionError as exc:
                error = exc
        if error:
            raise error
        return result

    def decide(self, session_token, value):
        fields(value, ('code', 'decision', 'capabilities', 'scope', 'confirmed'))
        need(value['decision'] in ('approve', 'deny') and value['confirmed'] is True)
        selected, targets = capabilities(value['capabilities']), scope(value['scope'])
        if value['decision'] == 'approve':
            for owner in targets['owners']:
                self.owner_check(owner)
        generation = self.generation()
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            context = self._context_in(db, generation)
            session = self._operator_in(db, session_token)
            device = self._find_device_in(db, value['code'], context)
            requested, requested_scope = json.loads(device['capabilities_json']), json.loads(device['scope_json'])
            need(set(selected) <= set(requested) and all(row in requested_scope['repositories'] for row in targets['repositories'])
                 and all(row in requested_scope['owners'] for row in targets['owners']),
                 'invalid_scope', 'Approval cannot expand the requested access.')
            if value['decision'] == 'deny':
                db.execute("UPDATE client_pairings SET status='denied' WHERE device_hash=?", (device['device_hash'],))
                audit_in(db, self.clock, '/api/connections/deny', operator=session['operator'])
                return {'status': 'denied'}
            count = db.execute('SELECT count(*) FROM client_connections WHERE expires_at>?', (self.clock(),)).fetchone()[0]
            need(count < 256, 'capacity_reached', 'Revoke expired or unused connections first.', 409)
            identifier = secrets.token_urlsafe(18)
            db.execute('INSERT INTO client_connections(id,operator_id,token_hash,client_name,authorization_epoch,scopes_json,created_at,expires_at,'
                'source_epoch,instance_id,setup_generation,origin,scope_json,access_hash,access_expires,refresh_hash,idle_expires,operator_login) '
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'',0,'',?,?)", (identifier, str(session['operator']['id']), 'connection:' + identifier,
                 device['client_name'], context['authorization_epoch'], _json(selected), self.clock(), self.clock() + ABSOLUTE_SECONDS,
                 context['epoch'], context['instance'], generation, context['origin'], _json(targets), self.clock() + IDLE_SECONDS,
                 session['operator']['login']))
            db.execute("UPDATE client_pairings SET status='approved',connection_id=? WHERE device_hash=?", (identifier, device['device_hash']))
            audit_in(db, self.clock, '/api/connections/approve', operator=session['operator'], connection=identifier)
        return {'status': 'approved', 'id': identifier}

    def _issue_in(self, db, row):
        from .mutation_audit import current
        if current():
            current().bind(grant=self._public(row, 'active'))
            current().details['effect_recorded'] = True
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        db.execute('UPDATE client_connections SET access_hash=?,access_expires=?,refresh_hash=?,idle_expires=?,last_used_at=? WHERE id=?',
            (digest(access), self.clock() + ACCESS_SECONDS, digest(refresh), min(row['expires_at'], self.clock() + IDLE_SECONDS), self.clock(), row['id']))
        current = db.execute('SELECT * FROM client_connections WHERE id=?', (row['id'],)).fetchone()
        return dict(token_type='Bearer', access_token=access, refresh_token=refresh, expires_in=ACCESS_SECONDS,
                    connection=self._public(current, 'active'))

    def poll(self, value):
        fields(value, ('client_id', 'device_code'))
        need(value['client_id'] == CLIENT_ID and isinstance(value['device_code'], str) and TOKEN.fullmatch(value['device_code']))
        generation, error, result = self.generation(), None, None
        with self.sqlite_store.write() as db:
            context = self._context_in(db, generation)
            device = db.execute('SELECT * FROM client_pairings WHERE device_hash=?', (digest(value['device_code']),)).fetchone()
            if (device is None or device['expires_at'] <= self.clock()
                    or (device['instance_id'], device['setup_generation'], device['source_epoch']) !=
                       (context['instance'], generation, context['epoch'])):
                error = 'expired_token'
            elif self.clock() < device['next_poll_at']:
                db.execute('UPDATE client_pairings SET poll_interval=poll_interval+5,next_poll_at=? WHERE device_hash=?',
                           (self.clock() + device['poll_interval'] + 5, device['device_hash']))
                error = 'slow_down'
            else:
                db.execute('UPDATE client_pairings SET next_poll_at=? WHERE device_hash=?',
                           (self.clock() + device['poll_interval'], device['device_hash']))
                error = {'pending': 'authorization_pending', 'denied': 'access_denied', 'consumed': 'expired_token'}.get(device['status'])
                if error is None:
                    grant = db.execute('SELECT * FROM client_connections WHERE id=?', (device['connection_id'],)).fetchone()
                    if not valid_connection_in(db, grant, generation, self.clock()):
                        error = 'access_denied'
                    else:
                        result = self._issue_in(db, grant)
                        db.execute("UPDATE client_pairings SET status='consumed' WHERE device_hash=?", (device['device_hash'],))
        if error:
            raise ConnectionError(error, 'Client authorization has not completed.' if error == 'authorization_pending' else 'Client authorization cannot complete.')
        return result

    def refresh(self, token):
        need(isinstance(token, str) and TOKEN.fullmatch(token))
        generation, result = self.generation(), None
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            self._context_in(db, generation)
            hashed = digest(token)
            consumed = db.execute('SELECT connection_id FROM consumed_refresh_tokens WHERE token_hash=?', (hashed,)).fetchone()
            if consumed:
                db.execute("UPDATE client_connections SET revoked_at=COALESCE(revoked_at,?),revocation_reason='refresh_replay' WHERE id=?",
                           (self.clock(), consumed[0]))
                audit_in(db, self.clock, 'refresh_replay', connection=consumed[0], outcome='revoked')
            else:
                row = db.execute('SELECT * FROM client_connections WHERE refresh_hash=?', (hashed,)).fetchone()
                if valid_connection_in(db, row, generation, self.clock()):
                    need(self.clock() >= (row['next_refresh_at'] or 0), 'slow_down', 'Wait before rotating this credential again.', 429)
                    count = db.execute('SELECT count(*) FROM consumed_refresh_tokens WHERE connection_id=?', (row['id'],)).fetchone()[0]
                    if count >= 10000:
                        db.execute("UPDATE client_connections SET revoked_at=?,revocation_reason='rotation_limit' WHERE id=?", (self.clock(), row['id']))
                    else:
                        db.execute('INSERT INTO consumed_refresh_tokens VALUES(?,?,?)', (hashed, row['id'], count))
                        db.execute('UPDATE client_connections SET next_refresh_at=? WHERE id=?', (self.clock() + 5, row['id']))
                        result = self._issue_in(db, row)
        need(result is not None, 'invalid_grant', 'Connection expired or was revoked. Connect again.', 401)
        return result

    def status(self, token):
        generation = self.generation()
        with self.sqlite_store.write() as db:
            context = self._context_in(db, generation)
            row = self._bearer_in(db, token, context)
            db.execute('UPDATE client_connections SET last_used_at=? WHERE id=?', (self.clock(), row['id']))
            row['last_used_at'] = self.clock()
            return self._public(row, 'active')

    def authorize(self, token, target, capability, instance, generation):
        from forge_client.contracts import repository
        repository(target)
        need(capability in CAPABILITIES)
        actual_generation = self.generation()
        with self.sqlite_store.read_snapshot() as db:
            context = self._context_in(db, actual_generation)
            row = self._bearer_in(db, token, context)
            grant = self._public(row, 'active')
        need(instance == context['instance'] and generation == actual_generation,
             'connection_changed', 'Forge instance or generation changed.', 409)
        need(capability in grant['capabilities'], 'insufficient_scope', 'This connection does not allow that action.', 403)
        if not any(r['id'] == target['id'] and r['name'].lower() == target['name'].lower() for r in grant['scope']['repositories']):
            need(bool(grant['scope']['owners']), 'insufficient_scope', 'Repository is outside this connection.', 403)
            owner_id = self.identity(target)
            need(any(owner['id'] == owner_id for owner in grant['scope']['owners']),
                 'insufficient_scope', 'Repository owner is outside this connection.', 403)
        return self.status(token)

    def view(self, session_token):
        generation = self.generation()
        with self.sqlite_store.read_snapshot() as db:
            self._context_in(db, generation)
            self._operator_in(db, session_token)
            rows = db.execute('SELECT * FROM client_connections ORDER BY created_at DESC,id LIMIT 256').fetchall()
            return {'enabled': True, 'connections': [self._public(row, 'active' if valid_connection_in(db, row, generation, self.clock())
                    else 'revoked' if row['revocation_reason'] or row['revoked_at'] is not None else 'expired') for row in rows]}

    def revoke(self, session_token, identifier):
        need(isinstance(identifier, str) and re.fullmatch(r'[A-Za-z0-9_-]{24}', identifier))
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            active_in(db)
            self.auth._config_in(db)
            session = self._operator_in(db, session_token)
            need(db.execute('SELECT 1 FROM client_connections WHERE id=?', (identifier,)).fetchone(), 'not_found', 'Connection not found.', 404)
            db.execute("UPDATE client_connections SET revoked_at=COALESCE(revoked_at,?),revocation_reason='operator_revoked' WHERE id=?", (self.clock(), identifier))
            audit_in(db, self.clock, '/api/connections/revoke', operator=session['operator'], connection=identifier)

    def disconnect(self, token):
        generation = self.generation()
        with identity_effect_lock(self.sqlite_store), self.sqlite_store.write() as db:
            context = self._context_in(db, generation)
            row = self._bearer_in(db, token, context)
            db.execute("UPDATE client_connections SET revoked_at=?,revocation_reason='client_disconnected' WHERE id=?", (self.clock(), row['id']))
            audit_in(db, self.clock, '/api/connections/disconnect', connection=row['id'])

    @contextmanager
    def effect_guard(self, token, instance, generation):
        with identity_effect_lock(self.sqlite_store):
            actual = self.generation()
            with self.sqlite_store.read_snapshot() as db:
                context = self._context_in(db, actual)
                self._bearer_in(db, token, context)
                need(instance == context['instance'] and generation == actual,
                     'connection_changed', 'Connect to the current Forge instance again.', 409)
            yield


class SqliteClientRepositories:
    def __init__(self, settings, clients, *, store, setup=None, launch=None):
        from .repository_setup import RepositorySetup
        self.settings, self.clients = settings, clients
        self.sqlite_store, self.store = store, store
        self.setup = setup or RepositorySetup(settings)
        self.launch = launch or (lambda fn: threading.Thread(target=fn, daemon=True).start())

    def authorize(self, token, value, capability):
        return self.clients.authorize(token, value['repository'], capability, value['instance'], value['generation'])

    def read(self, token, value):
        # Reuse the scoped response contract, whose backing services are selected
        # by the same runtime authority. It does not read operation persistence.
        from .client_repositories import ClientRepositories
        return ClientRepositories.read(self, token, value)

    @staticmethod
    def _record(row):
        value = dict(request=json.loads(row['payload_json']), status='complete' if row['status'] == 'completed' else row['status'],
                     created_at=row['created_at'])
        for source, target in [('finished_at', 'finished_at'), ('error_code', 'error'), ('expected_revision', 'expected_revision')]:
            if row[source] is not None:
                value[target] = row[source]
        if row['result_json'] is not None:
            value['result'] = json.loads(row['result_json'])
        return value

    @staticmethod
    def public(record):
        from .client_repositories import ClientRepositories
        return ClientRepositories.public(record)

    def _get(self, identifier):
        with self.sqlite_store.read_snapshot() as db:
            active_in(db)
            row = db.execute('SELECT * FROM client_operations WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            return self._record(row)

    def operation(self, token, value):
        from .client_repositories import OPERATIONS
        from forge_client.contracts import REVISION
        fields(value, ('repository', 'instance', 'generation', 'id', 'action', 'revision'))
        need(isinstance(value['id'], str) and re.fullmatch(r'[a-f0-9]{32}', value['id']))
        need(isinstance(value['action'], str) and value['action'] in OPERATIONS)
        need(isinstance(value['revision'], str) and REVISION.fullmatch(value['revision']))
        grant = self.authorize(token, value, OPERATIONS[value['action']])
        binding = {**deepcopy(value), 'client': grant['id'], 'operator': grant['operator']}
        with self.sqlite_store.write() as db:
            active_in(db)
            row = db.execute('SELECT * FROM client_operations WHERE id=?', (value['id'],)).fetchone()
            if row:
                need(json.loads(row['payload_json']) == binding, 'operation_conflict',
                     'This operation ID belongs to a different request.', 409)
            else:
                need(db.execute('SELECT count(*) FROM client_operations').fetchone()[0] < 10000,
                     'operation_capacity', 'Operation history needs maintenance.', 409)
                repository = db.execute('SELECT id,full_name FROM repositories WHERE id=?', (value['repository']['id'],)).fetchone()
                if repository:
                    need(repository['full_name'].lower() == value['repository']['name'].lower(),
                         'scope_changed', 'Repository identity changed.', 403)
                # An unconfigured target stays in the typed request until the
                # repository service admits it; do not enable it by inserting a
                # lifecycle repository row as a side effect of requesting setup.
                db.execute('INSERT INTO client_operations(id,request_id,connection_id,repository_id,kind,status,payload_json,created_at,updated_at) '
                           "VALUES(?,?,?,?,?,'pending',?,?,?)", (value['id'], value['id'], grant['id'],
                            repository['id'] if repository else None, value['action'], _json(binding), self.settings.clock(), self.settings.clock()))
                from .mutation_audit import current, MutationAudit
                audit = current() or MutationAudit(self.sqlite_store, '/api/client/operation', clock=self.settings.clock)
                audit.bind(grant=grant)
                audit.describe(value)
                audit.details['command_request_id'] = value['id']
                audit.finish_in(db, 'accepted', 'accepted')
                row = db.execute('SELECT * FROM client_operations WHERE id=?', (value['id'],)).fetchone()
            result = self.public(self._record(row))
        if result['status'] == 'pending':
            self.launch(lambda: self.run(token, value))
        return result

    def update(self, identifier, **values):
        if set(values) - {'status', 'error', 'result', 'finished_at', 'expected_revision'}:
            raise ValueError('Unsupported client operation field')
        mapping = dict(status='status', error='error_code', result='result_json', finished_at='finished_at', expected_revision='expected_revision')
        with self.sqlite_store.write() as db:
            active_in(db)
            row = db.execute('SELECT * FROM client_operations WHERE id=?', (identifier,)).fetchone()
            if row is None:
                raise KeyError(identifier)
            if row['status'] in {'completed', 'failed'}:
                return  # Completion and failure receipts cannot be overwritten.
            parameters, columns = [], []
            for key, value in values.items():
                if key == 'status':
                    need(value in {'pending', 'complete', 'failed'})
                    value = 'completed' if value == 'complete' else value
                elif key == 'result':
                    value = _json(value)
                columns.append(mapping[key] + '=?')
                parameters.append(value)
            db.execute('UPDATE client_operations SET ' + ','.join(columns + ['updated_at=?']) + ' WHERE id=?',
                       (*parameters, self.settings.clock(), identifier))
            if values.get('status') in {'complete', 'failed'}:
                audit_in(db, self.settings.clock, 'client_repository_' + row['kind'], connection=row['connection_id'],
                         request_id=identifier, outcome=values['status'], details={'kind': 'operation_effect', 'error_code': values.get('error')})

    def run(self, token, value):
        from .mutation_audit import MutationAudit, request_context
        binding = self._get(value['id'])['request']
        audit = MutationAudit(self.sqlite_store, '/api/client/operation', clock=self.settings.clock)
        audit.bind(grant={'operator': binding['operator'], 'id': binding['client']})
        audit.describe(value)
        with request_context(audit):
            return self._run(token, value)

    def _run(self, token, value):
        from .client_repositories import OPERATIONS
        from .repository_requirements import describe
        from .settings import digest as config_digest
        identifier, action, target = value['id'], value['action'], value['repository']
        try:
            with self.settings.locked('client-' + identifier, blocking=False):
                record = self._get(identifier)
                if record['status'] != 'pending':
                    return
                self.authorize(token, value, OPERATIONS[action])
                current = describe(self.settings, target)
                if action != 'verify' and record.get('expected_revision') == current['revision']:
                    self.finish(identifier, current)
                    return
                need(current['revision'] == value['revision'], 'revision_changed',
                     'Forge configuration changed. Inspect and create a new operation.', 409)
                if action in ('verify', 'enable'):
                    need(self.settings.refresh(), 'verification_busy', 'Access refresh is busy. Retry verification.', 409)
                    self.authorize(token, value, OPERATIONS[action])
                    current = describe(self.settings, target)
                    if current['readiness']['discovered']:
                        need(self.setup.check(target['id'], value['revision']), 'verification_busy',
                             'Repository verification is busy. Retry verification.', 409)
                self.authorize(token, value, OPERATIONS[action])
                current = describe(self.settings, target)
                need(current['revision'] == value['revision'], 'revision_changed',
                     'Forge configuration changed. Inspect and create a new operation.', 409)
                if action != 'verify':
                    if action == 'enable':
                        need(current['readiness']['access_ready'] and current['readiness']['protection_ready'],
                             'repository_not_ready', 'Refresh repository setup and repair the reported requirements.', 409)
                    config = deepcopy(self.settings.load())
                    row = next((row for row in config['repositories'] if row['id'] == target['id']), None)
                    if row is None:
                        config['repositories'].append({**target, 'enabled': action == 'enable'})
                    else:
                        row['enabled'] = action == 'enable'
                    self.update(identifier, expected_revision=config_digest(config))
                    with self.clients.effect_guard(token, value['instance'], value['generation']):
                        getattr(self.setup, action)(target['id'], value['revision'])
                self.authorize(token, value, OPERATIONS[action])
                self.finish(identifier, describe(self.settings, target))
        except BlockingIOError:
            return
        except ConnectionError as exc:
            self.update(identifier, status='failed', error=exc.code, finished_at=self.settings.clock())
        except (ValueError, OSError, RuntimeError, KeyError, TypeError):
            self.update(identifier, status='failed', error='verification_failed', finished_at=self.settings.clock())

    def finish(self, identifier, result):
        self.update(identifier, status='complete', result=result, finished_at=self.settings.clock())
