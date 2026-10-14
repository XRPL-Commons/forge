"""Browser-approved client grants. No GitHub administrative credentials enter here."""
from contextlib import contextmanager
from copy import deepcopy
import json
import re
import secrets
from urllib.parse import urlencode

from forge_client.contracts import positive, repository
from .operator_auth import OperatorAuth, digest
from .sqlite_identity import active_in, audit_in, identity_effect_lock
from .store.files import StateError
from .store.identity import session_in, valid_connection_in
from .store.runtime import required
from .store.transitions import _json

CAPABILITIES = ('repositories:read', 'repositories:prepare', 'repositories:activate', 'repositories:policy', 'repositories:triage')
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
    from .store.runtime import required
    with required(root).read_snapshot() as db:
        paused = db.execute("SELECT enabled FROM runtime_controls WHERE name='factory_paused'").fetchone()
        state = db.execute("SELECT generation,phase FROM setup_instances WHERE scope='live'").fetchone()
    need(state is not None and state['phase'] == 'active' and not (paused and paused[0]),
         'setup_required', 'Finish initial setup or reset before connecting an agent.', 409)
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
    def __init__(self, root, *, store=None, auth=None, generation=None, owner_check=verify_owner, identity=None):
        store = store if store is not None else required(root)
        self.root, self.sqlite_store, self.store = root, store, store
        self.auth = auth or OperatorAuth(root, store=store)
        if not isinstance(self.auth, OperatorAuth):
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
