"""Tailscale operator identity, same-App GitHub fallback, spoofing and revocation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from forge import tailnet_identity
from forge.client_connections import ClientConnections, ConnectionError
from forge.operator_auth import (AuthError, FLOW_COOKIE, OperatorAuth, SESSION_COOKIE, SESSION_SECONDS,
                                 digest, read_cookie)
from tests import test_settings_http
from tests.sqlite_fixture import selected_store
from tests.test_dashboard import dashboard

LOGIN = 'alice@example.com'


class LoginTests(unittest.TestCase):
    def test_people_only(self):
        self.assertEqual(tailnet_identity.login(' Alice@Example.com '), LOGIN)
        self.assertEqual(tailnet_identity.login('alice@github'), 'alice@github')
        for value in ('tagged-devices', 'tagged-devices@example.com', '', 'alice', 'a,b@example.com',
                      'alice@example.com, mallory@example.com', None, 'x' * 400 + '@example.com', 7):
            with self.subTest(value=value):
                self.assertIsNone(tailnet_identity.login(value))


class IdentityBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.database = selected_store(self.root)
        self.now = 1000
        self.identity = Mock(return_value={'id': 123, 'login': 'operator'})
        self.auth = OperatorAuth(self.temp.name, clock=lambda: self.now, identity=self.identity)
        self.auth.configure('https://forge.example', 'oauth-client', 's' * 40, [123])

    def github_login(self, tailnet_login=None):
        url, cookie = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        return read_cookie(self.auth.callback(state, 'code', read_cookie(cookie, FLOW_COOKIE),
                                              tailnet_login=tailnet_login), SESSION_COOKIE)


class TailnetSessionTests(IdentityBase):
    def test_github_mode_ignores_tailnet_logins(self):
        self.auth.link_tailnet(LOGIN, 123)
        self.assertIsNone(self.auth.tailnet_session(LOGIN))

    def test_first_github_sign_in_through_serve_records_the_link(self):
        self.auth.set_identity_mode('tailscale')
        self.assertIsNone(self.auth.tailnet_session(LOGIN))
        self.github_login(tailnet_login=LOGIN)
        self.assertEqual([(r['login'], r['github_id']) for r in self.auth.identity_settings()['tailnet_operators']],
                         [(LOGIN, 123)])
        token, session = self.auth.tailnet_session(LOGIN)
        self.assertEqual((session['operator']['id'], session['via']), (123, 'tailscale'))
        # Stable within a window: no new rows per request.
        self.assertEqual(self.auth.tailnet_session(LOGIN)[0], token)
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM operator_sessions WHERE id LIKE 'tailnet:%'").fetchone()[0], 1)
            rows = json.dumps([list(r) for r in db.execute('SELECT * FROM operator_sessions')])
        self.assertNotIn(token, rows)
        # A new window derives a new session.
        self.now += SESSION_SECONDS
        self.assertNotEqual(self.auth.tailnet_session(LOGIN)[0], token)

    def test_a_tailnet_session_can_never_be_presented_as_a_cookie(self):
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        token, _ = self.auth.tailnet_session(LOGIN)
        self.assertIsNone(self.auth.session(token))
        self.auth.logout(token)
        self.assertIsNone(self.auth.tailnet_session(LOGIN))

    def test_rotation_keeps_tailnet_available_in_the_same_window(self):
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        old, _ = self.auth.tailnet_session(LOGIN)
        self.auth.rotate_client('Iv23liForgeApp', 'a' * 40)
        fresh, session = self.auth.tailnet_session(LOGIN)
        self.assertNotEqual(old, fresh)
        self.assertEqual(session['operator']['id'], 123)
        self.assertEqual(self.auth.tailnet_session(LOGIN)[0], fresh)

    def test_reenable_starts_a_new_session_in_the_same_window(self):
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        old, _ = self.auth.tailnet_session(LOGIN)
        self.auth.set_identity_mode('github')
        self.auth.set_identity_mode('tailscale')
        fresh, _ = self.auth.tailnet_session(LOGIN)
        self.assertNotEqual(old, fresh)
        with self.database.read_snapshot() as db:
            self.assertIsNotNone(db.execute('SELECT revoked_at FROM operator_sessions WHERE token_hash=?',
                                           (digest(old),)).fetchone()[0])

    def test_logout_requires_explicit_sign_in_even_after_a_new_window(self):
        self.auth.set_identity_mode('tailscale')
        self.github_login(tailnet_login=LOGIN)
        token, _ = self.auth.tailnet_session(LOGIN)
        self.auth.logout(token)
        self.assertIsNone(self.auth.tailnet_session(LOGIN))
        self.now += SESSION_SECONDS
        self.assertIsNone(self.auth.tailnet_session(LOGIN))
        # Mode toggles cannot restore a link removed by sign-out.
        self.auth.set_identity_mode('github')
        self.auth.set_identity_mode('tailscale')
        self.assertIsNone(self.auth.tailnet_session(LOGIN))
        github = self.github_login(tailnet_login=LOGIN)
        self.assertIsNotNone(self.auth.session(github))
        self.assertNotEqual(self.auth.tailnet_session(LOGIN)[0], token)

    def test_unlinking_revokes_immediately_and_relinking_starts_fresh(self):
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        token, _ = self.auth.tailnet_session(LOGIN)
        self.assertTrue(self.auth.unlink_tailnet(LOGIN)['unlinked'])
        self.assertIsNone(self.auth.tailnet_session(LOGIN))
        self.auth.link_tailnet(LOGIN, 123)
        self.assertNotEqual(self.auth.tailnet_session(LOGIN)[0], token)

    def test_removed_operator_and_returning_to_github_mode_end_tailnet_access(self):
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        self.assertIsNotNone(self.auth.tailnet_session(LOGIN))
        self.auth.configure('https://forge.example', 'oauth-client', 's' * 40, [456])
        self.assertIsNone(self.auth.tailnet_session(LOGIN))
        self.auth.configure('https://forge.example', 'oauth-client', 's' * 40, [123])
        token, _ = self.auth.tailnet_session(LOGIN)
        self.auth.set_identity_mode('github')
        with self.database.read_snapshot() as db:
            self.assertIsNotNone(db.execute('SELECT revoked_at FROM operator_sessions WHERE token_hash=?',
                                            (digest(token),)).fetchone()[0])

    def test_links_are_explicit_and_never_repointed(self):
        with self.assertRaises(AuthError):
            self.auth.link_tailnet('tagged-devices', 123)
        with self.assertRaises(AuthError):
            self.auth.link_tailnet(LOGIN, 999)
        self.auth.configure('https://forge.example', 'oauth-client', 's' * 40, [123, 456])
        self.auth.link_tailnet(LOGIN, 123)
        with self.assertRaisesRegex(AuthError, 'another operator'):
            self.auth.link_tailnet(LOGIN, 456)
        # A GitHub sign-in by another operator through the same Serve login does not steal it.
        self.auth.set_identity_mode('tailscale')
        self.identity.return_value = {'id': 456, 'login': 'other'}
        self.github_login(tailnet_login=LOGIN)
        self.assertEqual(self.auth.identity_settings()['tailnet_operators'][0]['github_id'], 123)


class SameAppSignInTests(IdentityBase):
    def test_rotating_to_the_forge_app_client_keeps_operators_sessions_and_agents(self):
        token = self.github_login()
        with self.database.write() as db:
            before = dict(db.execute('SELECT * FROM operator_auth_configuration').fetchone())
        url, _ = self.auth.begin()  # An in-flight sign-in started with the old client.
        result = self.auth.rotate_client('Iv23liForgeApp', 'a' * 40)
        self.assertEqual(result, {'status': 'rotated'})
        self.assertEqual(self.auth.rotate_client('Iv23liForgeApp', 'a' * 40), {'status': 'unchanged'})
        config = self.auth.config()
        self.assertEqual((config['client_id'], config['operators']), ('Iv23liForgeApp', [123]))
        self.assertEqual(self.auth.session(token)['operator']['id'], 123)
        with self.database.read_snapshot() as db:
            after = dict(db.execute('SELECT * FROM operator_auth_configuration').fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM operator_flows WHERE consumed_at IS NULL').fetchone()[0], 0)
            actions = [row[0] for row in db.execute('SELECT action FROM operator_audit ORDER BY sequence')]
        for key in ('authorization_epoch', 'source_epoch', 'instance_id', 'origin'):
            self.assertEqual(after[key], before[key])
        self.assertIn('operator_auth_client_rotated', actions)
        state = parse_qs(urlsplit(url).query)['state'][0]
        with self.assertRaises(AuthError):
            self.auth.callback(state, 'code', 'x' * 43)
        new_url, _ = self.auth.begin()
        self.assertEqual(parse_qs(urlsplit(new_url).query)['client_id'], ['Iv23liForgeApp'])

    def test_rotation_requires_existing_sign_in_and_a_valid_client(self):
        with self.assertRaises(AuthError):
            self.auth.rotate_client('x', 'a' * 40)
        fresh = tempfile.TemporaryDirectory(); self.addCleanup(fresh.cleanup)
        selected_store(Path(fresh.name))
        with self.assertRaises(AuthError):
            OperatorAuth(fresh.name).rotate_client('Iv23liForgeApp', 'a' * 40)


class AgentRevocationTests(IdentityBase):
    def test_an_agent_approved_through_tailscale_is_revocable_and_loses_access(self):
        self.assert_agent_revoked('revoke')

    def test_sign_out_revokes_approved_agent_access_and_refresh(self):
        self.assert_agent_revoked('logout')

    def assert_agent_revoked(self, action):
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        token, _ = self.auth.tailnet_session(LOGIN)
        target = {'id': 10, 'name': 'example/project'}
        clients = ClientConnections(self.temp.name, auth=self.auth, generation=lambda: 'g1',
                                    owner_check=lambda owner: None, identity=lambda target: 1)
        device = clients.begin({'client_id': 'forge-cli', 'name': 'agent', 'capabilities': ['repositories:read'],
                                'scope': {'repositories': [target], 'owners': []}})
        approved = clients.decide(token, {'code': device['user_code'], 'decision': 'approve', 'capabilities': ['repositories:read'],
                                          'scope': {'repositories': [target], 'owners': []}, 'confirmed': True})
        self.now += 5
        grant = clients.poll({'client_id': 'forge-cli', 'device_code': device['device_code']})
        instance = grant['connection']['instance']
        clients.authorize(grant['access_token'], target, 'repositories:read', instance, 'g1')
        if action == 'logout':
            self.auth.logout(token)
        else:
            clients.revoke(token, approved['id'])
        with self.assertRaises(ConnectionError) as raised:
            clients.authorize(grant['access_token'], target, 'repositories:read', instance, 'g1')
        self.assertEqual(raised.exception.code, 'invalid_token')
        with self.assertRaises(ConnectionError):
            clients.refresh(grant['refresh_token'])
        with self.database.read_snapshot() as db:
            row = db.execute('SELECT operator_id,revocation_reason FROM client_connections WHERE id=?', (approved['id'],)).fetchone()
            audit_action = 'operator_signed_out' if action == 'logout' else '/api/connections/revoke'
            audit = db.execute('SELECT operator_id FROM operator_audit WHERE action=?', (audit_action,)).fetchone()
        # One identity across sign-in methods: the grant and the audit name the GitHub operator.
        reason = 'operator_signed_out' if action == 'logout' else 'operator_revoked'
        self.assertEqual((row[0], row[1], audit[0]), ('123', reason, '123'))

    def test_unlinked_tailnet_login_cannot_manage_agents(self):
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        token, _ = self.auth.tailnet_session(LOGIN)
        self.auth.unlink_tailnet(LOGIN)
        clients = ClientConnections(self.temp.name, auth=self.auth, generation=lambda: 'g1')
        with self.assertRaises(ConnectionError) as raised:
            clients.view(token)
        self.assertEqual(raised.exception.code, 'sign_in_required')


class TailnetHTTPTests(unittest.TestCase):
    handler = test_settings_http.SettingsHTTPTests.handler

    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.database = selected_store(Path(temp.name))
        patcher = patch.object(dashboard, 'WD', temp.name)
        patcher.start(); self.addCleanup(patcher.stop)
        self.auth = OperatorAuth(temp.name, identity=lambda *args: {'id': 123, 'login': 'operator'})
        self.auth.configure('https://forge.example', 'client-id', 's' * 40, [123])

    def request(self, path, body=None, **headers):
        handler = self.handler(path, body, **{'X-Forwarded-Host': 'forge.example', **headers})
        handler.send_response = Mock(); handler.send_header = Mock(); handler.end_headers = Mock()
        getattr(handler, 'do_POST' if body is not None else 'do_GET')()
        return handler

    def session(self, **headers):
        handler = self.request('/api/auth/session', **headers)
        return json.loads(handler._send.call_args.args[0])

    def test_trusted_header_signs_in_a_linked_operator_only_in_tailscale_mode(self):
        header = {tailnet_identity.UPSTREAM_HEADER: LOGIN}
        self.auth.link_tailnet(LOGIN, 123)
        self.assertIsNone(self.session(**header)['operator'])
        self.auth.set_identity_mode('tailscale')
        result = self.session(**header)
        self.assertEqual((result['operator']['id'], result['via'], result['mode']), (123, 'tailscale', 'tailscale'))
        with patch.object(dashboard, 'settings_service') as service:
            handler = self.request('/api/settings/sync', {}, **header, **{'X-Forge-Action': result['controls_token']})
            handler.send_error.assert_not_called()
            service.return_value.start_refresh.assert_called_once()
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT operator_id,session_id FROM operator_audit WHERE action='/api/settings/sync'").fetchone()
        self.assertEqual(row[0], '123')
        self.assertTrue(row[1].startswith('tailnet:'))

    def test_spoofed_tagged_unlinked_or_duplicate_headers_get_nothing(self):
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        for headers in ({tailnet_identity.UPSTREAM_HEADER: 'mallory@example.com'},
                        {tailnet_identity.UPSTREAM_HEADER: 'tagged-devices'},
                        {tailnet_identity.UPSTREAM_HEADER: LOGIN + ', mallory@example.com'},
                        # Tailscale's own header name is never trusted by the API; only the proxy's is.
                        {'Tailscale-User-Login': LOGIN}):
            with self.subTest(headers=headers):
                self.assertIsNone(self.session(**headers)['operator'])
                self.request('/api/settings', **headers).send_error.assert_called_once_with(401)

    def test_identity_routes_switch_mode_and_unlink(self):
        header = {tailnet_identity.UPSTREAM_HEADER: LOGIN}
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        token = self.session(**header)['controls_token']
        handler = self.request('/api/auth/identity', **header)
        self.assertEqual(json.loads(handler._send.call_args.args[0])['tailnet_operators'][0]['login'], LOGIN)
        handler = self.request('/api/auth/identity', {'action': 'unlink', 'login': LOGIN}, **header, **{'X-Forge-Action': token})
        self.assertTrue(json.loads(handler._send.call_args.args[0])['unlinked'])
        self.assertIsNone(self.session(**header)['operator'])

    def test_logout_of_a_tailscale_session_clears_identity(self):
        header = {tailnet_identity.UPSTREAM_HEADER: LOGIN}
        self.auth.set_identity_mode('tailscale')
        self.auth.link_tailnet(LOGIN, 123)
        token = self.session(**header)['controls_token']
        handler = self.request('/api/auth/logout', {}, **header, **{'X-Forge-Action': token})
        self.assertTrue(json.loads(handler._send.call_args.args[0])['ok'])
        self.assertIsNone(self.session(**header)['operator'])


if __name__ == '__main__':
    unittest.main()
