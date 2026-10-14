from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from forge.client_connections import ClientConnections, ConnectionError, CAPABILITIES, ACCESS_SECONDS, IDLE_SECONDS
from forge.operator_auth import OperatorAuth, read_cookie, SESSION_COOKIE, FLOW_COOKIE
from forge_client.connection import Client, Vault, Transport, public_connection, connection_lock, origin
from forge_client.contracts import SetupError
from tests import test_settings_http
from tests.sqlite_fixture import selected_store
from tests.test_dashboard import dashboard


class Fixture(unittest.TestCase):
    instance_id = None

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = self.create_store()
        self.now = int(time.time())
        self.generation = 'generation-one'
        self.auth = OperatorAuth(self.root, clock=lambda: self.now, identity=lambda *args: {'id': 123, 'login': 'operator'})
        self.auth.configure('https://forge.example', 'client-id', 's' * 40, [123])
        url, cookie = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.session = read_cookie(self.auth.callback(state, 'code', read_cookie(cookie, FLOW_COOKIE)), SESSION_COOKIE)
        self.owner_check = Mock()
        self.identity = Mock(return_value=88)
        self.service = ClientConnections(self.root, auth=self.auth, generation=lambda: self.generation,
                                         owner_check=self.owner_check, identity=self.identity)
        self.target = {'id': 321, 'name': 'example/project'}
        self.request = {'client_id': 'forge-cli', 'name': 'Local agent', 'capabilities': list(CAPABILITIES),
                        'scope': {'repositories': [self.target], 'owners': []}}

    def create_store(self):
        return selected_store(self.root, self.instance_id)

    def begin(self, **extra):
        return self.service.begin({**self.request, **extra})

    def approve(self, device, **extra):
        return self.service.decide(self.session, {'code': device['user_code'], 'decision': 'approve',
            'capabilities': self.request['capabilities'], 'scope': self.request['scope'], 'confirmed': True, **extra})

    def token(self):
        device = self.begin(); self.approve(device); self.now += 5
        return self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']})

    def assertError(self, code, operation):
        with self.assertRaises((ConnectionError, SetupError)) as caught:
            operation()
        self.assertEqual(caught.exception.code, code)


class ConnectionTests(Fixture):
    def test_matching_code_approval_tokens_and_secret_free_listing(self):
        device = self.begin()
        preview = self.service.preview(self.session, device['user_code'])
        self.assertEqual(preview['scope'], self.request['scope'])
        self.assertNotIn('device_code', preview)
        self.approve(device)
        self.now += 5
        token = self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']})
        connection = self.service.status(token['access_token'])
        self.assertEqual(connection['operator'], {'id': 123, 'login': 'operator'})
        self.service.authorize(token['access_token'], self.target, 'repositories:activate', device['instance'], device['generation'])
        listing = json.dumps(self.service.view(self.session))
        with self.database.read_snapshot() as db:
            stored = '\n'.join(db.iterdump())
        for secret in (device['device_code'], token['access_token'], token['refresh_token'], 's' * 40):
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, listing)
        self.assertFalse((self.root / 'state/operator-auth').exists())
        self.now += 1
        self.assertEqual(self.service.status(token['access_token'])['last_used'], self.now)

    def test_pending_slow_down_and_expiry_persist_across_instances(self):
        device = self.begin()
        poll = lambda: self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']})
        self.assertError('slow_down', poll)
        self.now += 10
        self.assertError('authorization_pending', poll)
        self.now += 601
        self.assertError('expired_token', poll)
        self.assertError('expired_token', lambda: self.service.preview(self.session, device['user_code']))

    def test_denial_and_single_redemption(self):
        device = self.begin(); self.approve(device, decision='deny'); self.now += 5
        self.assertError('access_denied', lambda: self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']}))
        device = self.begin(); self.approve(device); self.now += 5
        def redeem():
            try:
                return self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']})
            except ConnectionError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: redeem(), range(2)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.now += 15
        self.assertError('expired_token', lambda: self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']}))

    def test_refresh_rotation_replay_revokes_new_access_and_refresh(self):
        token = self.token()
        fresh = self.service.refresh(token['refresh_token'])
        self.assertError('invalid_token', lambda: self.service.status(token['access_token']))
        self.service.status(fresh['access_token'])
        self.assertError('invalid_grant', lambda: self.service.refresh(token['refresh_token']))
        self.assertError('invalid_token', lambda: self.service.status(fresh['access_token']))
        self.assertError('invalid_grant', lambda: self.service.refresh(fresh['refresh_token']))

    def test_revoke_blocks_access_refresh_and_pending_redemption(self):
        token = self.token(); identifier = token['connection']['id']
        self.service.revoke(self.session, identifier)
        self.assertError('invalid_token', lambda: self.service.status(token['access_token']))
        self.assertError('invalid_grant', lambda: self.service.refresh(token['refresh_token']))
        device = self.begin(); approved = self.approve(device)
        self.service.revoke(self.session, approved['id']); self.now += 5
        self.assertError('access_denied', lambda: self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']}))

    def test_operator_logout_reconfiguration_and_reset_revoke(self):
        token = self.token()
        self.auth.logout(self.session)
        self.assertError('invalid_token', lambda: self.service.status(token['access_token']))
        self.assertError('sign_in_required', lambda: self.service.view(self.session))
        self.setUp()
        token = self.token(); self.generation = 'reset-generation'
        self.assertError('invalid_token', lambda: self.service.status(token['access_token']))
        self.setUp()
        token = self.token()
        self.auth.configure('https://forge.example', 'client-id', 's' * 40, [999])
        self.assertError('invalid_token', lambda: self.service.status(token['access_token']))

    def test_access_and_refresh_expiry(self):
        token = self.token(); self.now += ACCESS_SECONDS
        self.assertError('invalid_token', lambda: self.service.status(token['access_token']))
        fresh = self.service.refresh(token['refresh_token'])
        self.now += IDLE_SECONDS
        self.assertError('invalid_grant', lambda: self.service.refresh(fresh['refresh_token']))

    def test_capability_and_repository_scope_cannot_expand(self):
        device = self.begin(capabilities=['repositories:read'])
        self.assertError('invalid_scope', lambda: self.approve(device))
        self.approve(device, capabilities=['repositories:read']); self.now += 5
        token = self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']})
        arguments = (device['instance'], device['generation'])
        self.assertError('insufficient_scope', lambda: self.service.authorize(token['access_token'], self.target, 'repositories:activate', *arguments))
        self.assertError('insufficient_scope', lambda: self.service.authorize(token['access_token'], {'id': 999, 'name': 'example/other'}, 'repositories:read', *arguments))
        self.assertError('connection_changed', lambda: self.service.authorize(token['access_token'], self.target, 'repositories:read', 'another-server', device['generation']))

    def test_owner_scope_is_verified_at_approval_and_on_each_repository_request(self):
        scope = {'repositories': [], 'owners': [{'id': 88, 'login': 'example'}]}
        device = self.begin(scope=scope); self.approve(device, scope=scope); self.now += 5
        self.owner_check.assert_called_once_with(scope['owners'][0])
        token = self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']})
        call = lambda: self.service.authorize(token['access_token'], self.target, 'repositories:read', device['instance'], device['generation'])
        call(); self.identity.assert_called_with(self.target)
        self.identity.return_value = 99
        self.assertError('insufficient_scope', call)
        self.identity.side_effect = lambda target: (self.service.revoke(self.session, token['connection']['id']), 88)[1]
        self.assertError('invalid_token', call)

    def test_bounded_anonymous_requests_and_operator_code_lookups(self):
        device = self.begin()
        for _ in range(19): self.begin()
        self.assertError('slow_down', self.begin)
        for _ in range(20):
            self.assertError('expired_token', lambda: self.service.preview(self.session, 'ZZZZ-ZZZZ'))
        self.assertError('slow_down', lambda: self.service.preview(self.session, device['user_code']))
        self.now += 61
        self.service.preview(self.session, device['user_code'])

    def test_approval_requires_current_operator_and_confirmation(self):
        device = self.begin()
        self.assertError('invalid_request', lambda: self.approve(device, confirmed=False))
        self.assertError('sign_in_required', lambda: self.service.preview('x' * 43, device['user_code']))
        self.auth.logout(self.session)
        self.assertError('sign_in_required', lambda: self.approve(device))

    def test_oauth_returns_to_approval_page_and_rejects_open_redirect(self):
        destination = '/settings/agents?code=BCDF-2345'
        url, cookie = self.auth.begin(destination)
        code = parse_qs(urlsplit(url).query)['state'][0]
        _, target = self.auth.callback(code, 'code', read_cookie(cookie, FLOW_COOKIE), with_target=True)
        self.assertEqual(target, destination)
        for destination in ('https://evil.example', '//evil.example', '/settings/agents?url=evil'):
            with self.assertRaises(ValueError): self.auth.begin(destination)


class MemoryVault:
    def __init__(self): self.values = {}
    def get(self, key): return deepcopy(self.values.get(key))
    def set(self, key, value): self.values[key] = deepcopy(value)
    def delete(self, key): self.values.pop(key, None)


class ClientTests(Fixture):
    def create_store(self):
        from forge.store.fresh import initialize
        return initialize(self.root)

    def setUp(self):
        super().setUp()
        self.vault = MemoryVault()
        self.requests = []
        def call(operation, value, token=None):
            self.requests.append((operation, deepcopy(value), token))
            try:
                if operation == 'device': result = self.service.begin(value)
                elif operation == 'token': result = self.service.poll(value)
                elif operation == 'refresh': result = self.service.refresh(value['refresh_token'])
                elif operation == 'status': result = self.service.status(token)
                elif operation == 'disconnect': self.service.disconnect(token); result = {'disconnected': True}
                return {'ok': True, **result}
            except ConnectionError as error:
                raise SetupError(error.code, str(error)) from None
        self.transport = Mock(call=Mock(side_effect=call))
        self.client = Client('https://forge.example', vault=self.vault, transport=self.transport,
                             clock=lambda: self.now, sleep=lambda delay: setattr(self, 'now', self.now + delay))

    def connect(self):
        display = self.client.begin('Local agent', self.request['capabilities'], self.request['scope'])
        self.approve({'user_code': display['user_code']})
        return display, self.client.resume(wait=5)

    def test_fresh_pairing_preserves_store_identity_and_separate_credential_format(self):
        import uuid
        from forge.store.fresh import initialize
        identity = self.database.instance_id
        self.assertEqual(str(uuid.UUID(identity)), identity)
        _, connected = self.connect()
        self.assertEqual(connected['instance'], identity)
        self.assertEqual(self.vault.get(self.client.key)['instance'], identity)
        self.assertEqual(initialize(self.root).instance_id, identity)
        self.now += ACCESS_SECONDS
        self.assertEqual(self.client.status()['instance'], identity)
        # Existing token-shaped instance identities remain valid too.
        legacy = {**connected, 'instance': 'i' * 43}
        self.assertEqual(public_connection(legacy, self.client.base)['instance'], 'i' * 43)
        with self.assertRaises(SetupError) as changed:
            public_connection(connected, self.client.base, str(uuid.uuid4()))
        self.assertEqual(changed.exception.code, 'connection_changed')
        transport = Transport(self.client.base)
        transport.opener = Mock()
        with self.assertRaises(SetupError):
            transport.call('status', {}, token=identity)
        transport.opener.open.assert_not_called()

    def test_connect_resume_refresh_status_and_disconnect_without_secret_output(self):
        display, connected = self.connect()
        self.assertEqual(connected['status'], 'active')
        self.now += ACCESS_SECONDS
        status = self.client.status()
        saved = self.vault.get(self.client.key)
        for token in (saved['access_token'], saved['refresh_token']):
            self.assertNotIn(token, json.dumps([display, connected, status]))
        self.client.disconnect()
        self.assertIsNone(self.vault.get(self.client.key))
        self.assertError('invalid_token', lambda: self.service.status(saved['access_token']))

    def test_pending_poll_respects_interval_and_noninteractive_resume(self):
        self.client.begin('Local agent', self.request['capabilities'], self.request['scope'])
        self.assertEqual(self.client.resume()['status'], 'authorization_pending')
        self.assertEqual(len(self.requests), 1)
        self.client.resume(wait=16)
        polls = [r for r in self.requests if r[0] == 'token']
        self.assertEqual(len(polls), 3)

    def test_redirect_and_host_substitution_never_receive_credentials(self):
        for url in ('http://forge.example', 'https://user:secret@forge.example', 'https://forge.example/path', 'https://forge.example?x=1'):
            self.assertError('invalid_server', lambda: origin(url))
        self.transport.call.side_effect = None
        self.transport.call.return_value = {'ok': True, 'device_code': 'a' * 43, 'user_code': 'BCDF-2345',
            'verification_uri': 'https://evil.example/settings/agents', 'verification_uri_complete': 'https://evil.example/settings/agents?code=BCDF-2345',
            'expires_in': 600, 'interval': 5, 'instance': 'b' * 43, 'generation': 'g'}
        with self.assertRaises(SetupError): self.client.begin('Local agent', self.request['capabilities'], self.request['scope'])
        self.assertIsNone(self.vault.get(self.client.key))

    def test_failed_keyring_never_falls_back_to_a_file(self):
        fake = Mock()
        fake.get_keyring.return_value = object()
        with patch.dict('sys.modules', {'keyring': fake}):
            self.assertError('credential_store_unavailable', Vault)

    def test_lock_serializes_refreshes_and_contains_no_credentials(self):
        with connection_lock(self.client.key, self.root / 'client'):
            with self.assertRaises(SetupError):
                with connection_lock(self.client.key, self.root / 'client'): pass
        self.assertEqual(next((self.root / 'client').iterdir()).read_text(), '')

    def test_cli_sanitizes_unexpected_exceptions(self):
        from forge_client.cli import main
        output = io.StringIO()
        with patch('forge_client.cli.execute', side_effect=OSError('synthetic-secret')), patch('sys.stdout', output):
            self.assertEqual(main(['--json', 'commands']), 1)
        self.assertNotIn('synthetic-secret', output.getvalue())


class ClientHTTPTests(Fixture):
    handler = test_settings_http.SettingsHTTPTests.handler

    def setUp(self):
        super().setUp()
        patcher = patch.object(dashboard, 'WD', str(self.root)); patcher.start(); self.addCleanup(patcher.stop)
        from forge.onboarding import Onboarding
        with Onboarding(self.root).store.transaction() as data:
            data.update(phase='active', generation=self.generation)

    def client_request(self, path, value, token=None, **headers):
        handler = self.handler(path, value, **{'X-Forwarded-Host': 'forge.example', **headers})
        handler.headers.pop('Origin', None)
        handler.headers.pop('X-Forge-Action', None)
        if token: handler.headers['Authorization'] = 'Bearer ' + token
        handler.do_POST()
        return handler

    def test_no_login_cookie_or_ui_token_can_authorize_client_routes(self):
        handler = self.client_request('/api/client/device', self.request, Cookie=SESSION_COOKIE + '=' + self.session)
        self.assertEqual(handler._send.call_args.kwargs['status'], 403)
        handler = self.client_request('/api/client/status', {})
        self.assertEqual(handler._send.call_args.kwargs['status'], 401)
        handler = self.client_request('/api/client/device', self.request)
        self.assertIn('device_code', json.loads(handler._send.call_args.args[0]))

    def test_bearer_does_not_authorize_browser_operator_api(self):
        token = self.token()
        handler = self.handler('/api/settings', **{'X-Forwarded-Host': 'forge.example', 'Authorization': 'Bearer ' + token['access_token']})
        handler.do_GET(); handler.send_error.assert_called_once_with(401)

    def test_requirements_are_fenced_before_any_repository_read(self):
        token = self.token()
        with patch.object(dashboard, 'settings_service') as settings:
            handler = self.client_request('/api/client/requirements', {'repository': {'id': 999, 'name': 'other/private'},
                'instance': token['connection']['instance'], 'generation': self.generation}, token['access_token'])
            self.assertEqual(handler._send.call_args.kwargs['status'], 403)
            settings.assert_not_called()

    def test_repository_routes_fence_scope_before_state_and_never_accept_cookies(self):
        token = self.token()
        common = {'repository': {'id': 999, 'name': 'other/private'},
                  'instance': token['connection']['instance'], 'generation': self.generation}
        payloads = {
            'authorize': {**common, 'capability': 'repositories:prepare'},
            'read': {**common, 'section': 'repository', 'number': None, 'page': 1},
            'operation': {**common, 'id': 'a' * 32, 'revision': '0' * 64, 'action': 'enable'},
        }
        for route, value in payloads.items():
            with self.subTest(route=route), patch.object(dashboard, 'settings_service') as settings:
                handler = self.client_request('/api/client/' + route, value, token['access_token'])
                self.assertEqual(handler._send.call_args.kwargs['status'], 403)
                settings.assert_not_called()
                handler = self.client_request('/api/client/' + route, value, token['access_token'], Cookie='ambient')
                self.assertEqual(handler._send.call_args.kwargs['status'], 403)

    def test_repository_read_dispatches_only_after_authorization(self):
        token = self.token()
        value = {'repository': self.target, 'instance': token['connection']['instance'],
                 'generation': self.generation, 'section': 'repository', 'number': None, 'page': 1}
        with patch('forge.client_repositories.ClientRepositories') as service:
            service.return_value.read.return_value = {'repository': self.target}
            handler = self.client_request('/api/client/read', value, token['access_token'])
            self.assertEqual(json.loads(handler._send.call_args.args[0]), {'ok': True, 'repository': self.target})
            service.return_value.read.assert_called_once_with(token['access_token'], value)
