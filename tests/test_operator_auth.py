import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, parse_qs

from forge.operator_auth import (OperatorAuth, AuthError, SESSION_COOKIE, FLOW_COOKIE,
                                 read_cookie, digest, SESSION_SECONDS, github_identity, failure_reason)
from forge.state import StateError
from tests import test_settings_http
from tests.test_dashboard import dashboard


class OperatorAuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1000
        self.identity = Mock(return_value={'id': 123, 'login': 'operator'})
        self.auth = OperatorAuth(self.temp.name, clock=lambda: self.now, identity=self.identity)
        self.configure()

    def configure(self, **kwargs):
        self.auth.configure(**{'origin': 'https://forge.example', 'client_id': 'identity-client',
                               'client_secret': 's' * 40, 'operators': [123], **kwargs})

    def begin(self):
        url, cookie = self.auth.begin()
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        self.assertEqual(query['scope'], [''])
        self.assertEqual(query['code_challenge_method'], ['S256'])
        self.assertIn('Secure; SameSite=Lax', cookie)
        return query['state'][0], read_cookie(cookie, FLOW_COOKIE)

    def login(self):
        state, browser = self.begin()
        result = self.auth.callback(state, 'code', browser)
        return read_cookie(result, SESSION_COOKIE)

    def test_sessions_are_hashed_expire_and_logout_revokes(self):
        token = self.login()
        self.assertEqual(self.auth.session(token)['operator']['id'], 123)
        self.assertNotIn(token, self.auth.store.path.read_text())
        self.assertIn(digest(token), self.auth.store.path.read_text())
        self.auth.logout(token)
        self.assertIsNone(self.auth.session(token))
        token = self.login()
        self.now += SESSION_SECONDS
        self.assertIsNone(self.auth.session(token))

    def test_one_use_state_requires_matching_browser(self):
        state, browser = self.begin()
        with self.assertRaises(AuthError):
            self.auth.callback(state, 'code', 'x' * 43)
        self.identity.assert_not_called()
        self.auth.callback(state, 'code', browser)
        with self.assertRaises(AuthError):
            self.auth.callback(state, 'code', browser)
        self.identity.assert_called_once()

    def test_expired_state_is_not_exchanged(self):
        state, browser = self.begin()
        self.now += 601
        with self.assertRaises(AuthError):
            self.auth.callback(state, 'code', browser)
        self.identity.assert_not_called()

    def test_denied_account_and_renamed_login_cannot_claim_operator(self):
        self.identity.return_value = {'id': 999, 'login': 'operator'}
        with self.assertRaises(AuthError):
            self.login()
        self.assertEqual(self.auth.store.read()['sessions'], {})

    def test_reconfiguration_revokes_sessions_and_pending_flows(self):
        token = self.login()
        state, browser = self.begin()
        self.configure()
        self.assertIsNone(self.auth.session(token))
        with self.assertRaises(AuthError):
            self.auth.callback(state, 'code', browser)

    def test_reconfiguration_during_exchange_fails_closed(self):
        def changed(*args):
            self.configure(operators=[456])
            return {'id': 123, 'login': 'operator'}
        self.identity.side_effect = changed
        with self.assertRaises(AuthError):
            self.login()
        self.assertEqual(self.auth.store.read()['sessions'], {})

    def test_consumed_state_stays_consumed_when_identity_lookup_fails(self):
        self.identity.side_effect = AuthError('offline')
        state, browser = self.begin()
        for _ in range(2):
            with self.assertRaises(AuthError):
                self.auth.callback(state, 'code', browser)
        self.identity.assert_called_once()

    def test_private_state_cannot_be_readable_by_other_accounts(self):
        self.auth.store.path.chmod(0o644)
        with self.assertRaises(StateError):
            self.auth.config()

    def test_missing_corrupt_or_emptied_initialized_state_fails_closed(self):
        for value in ('broken', '{}'):
            self.auth.store.path.write_text(value)
            with self.assertRaises((StateError, ValueError)):
                self.auth.config()
        self.auth.store.path.unlink()
        with self.assertRaises(StateError):
            OperatorAuth(self.temp.name).config()

    def test_new_installation_remains_legacy_until_explicitly_configured(self):
        with tempfile.TemporaryDirectory() as root:
            auth = OperatorAuth(root)
            self.assertIsNone(auth.config())
            self.assertFalse(auth.store.marker.exists())
            with self.assertRaises(AuthError):
                auth.begin()
            self.assertIsNone(auth.config())

    def test_configuration_requires_https_and_explicit_ids(self):
        for values in ({'origin': 'http://forge.example'}, {'origin': 'https://forge.example/path'},
                       {'origin': 'https://user@forge.example'}, {'operators': []}, {'operators': [True]}):
            with self.assertRaises(AuthError):
                self.configure(**values)

    def test_flow_capacity_is_bounded(self):
        for _ in range(100):
            self.auth.begin()
        with self.assertRaises(AuthError):
            self.auth.begin()
        self.now += 601
        self.auth.begin()
        self.assertEqual(len(self.auth.store.read()['flows']), 1)

    def test_audit_attributes_request_without_credentials(self):
        token = self.login()
        self.auth.audit(self.auth.session(token), '/api/repositories/enable')
        text = (self.auth.store.root / 'requests.jsonl').read_text()
        self.assertEqual(json.loads(text)['operator'], {'id': 123, 'login': 'operator'})
        self.assertNotIn(token, text)
        self.assertNotIn(self.auth.session(token)['csrf'], text)
        self.assertEqual(self.auth.store.path.stat().st_mode & 0o777, 0o600)

    def test_ui_unit_receives_only_public_auth_configuration(self):
        from forge.host_update import desired_units
        units = desired_units(self.temp.name, '127.0.0.1')
        self.assertIn('NITRO_FORGE_PUBLIC_ORIGIN=https://forge.example', units['forge-ui'])
        self.assertIn('NITRO_ALLOWED_HOSTS=forge.example', units['forge-ui'])
        for unit in units.values():
            self.assertNotIn('s' * 40, unit)
            self.assertNotIn('state/operator-auth', unit)

    def test_fixed_identity_endpoints_reject_admin_scope_and_redact_errors(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps({'access_token': 'synthetic-token', 'scope': 'repo', 'token_type': 'bearer'}).encode()
        opener = Mock()
        opener.open.return_value = response
        with patch('forge.operator_auth.build_opener', return_value=opener):
            with self.assertRaises(AuthError):
                github_identity(self.auth.config(), 'code', 'verifier')

        self.assertEqual(opener.open.call_count, 1)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://github.com/login/oauth/access_token')
        opener.open.side_effect = OSError('synthetic-secret-in-error')
        with patch('forge.operator_auth.build_opener', return_value=opener):
            with self.assertRaisesRegex(AuthError, '^GitHub identity verification failed$'):
                github_identity(self.auth.config(), 'code', 'verifier')


    def test_failure_diagnostics_have_a_closed_vocabulary(self):
        self.assertEqual(failure_reason(AuthError('Missing or invalid sign-in cookie')), 'missing_flow_cookie')
        self.assertEqual(failure_reason(RuntimeError('credential-in-upstream-error')), 'internal_error')


class OperatorHTTPTests(unittest.TestCase):
    handler = test_settings_http.SettingsHTTPTests.handler

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        patcher = patch.object(dashboard, 'WD', temp.name)
        patcher.start(); self.addCleanup(patcher.stop)
        self.auth = OperatorAuth(temp.name, identity=lambda *args: {'id': 123, 'login': 'operator'})
        self.auth.configure('https://forge.example', 'client-id', 's' * 40, [123])
        url, cookie = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.token = read_cookie(self.auth.callback(state, 'code', read_cookie(cookie, FLOW_COOKIE)), SESSION_COOKIE)
        self.session = self.auth.session(self.token)

    def request(self, path, body=None, signed=False, **headers):
        headers = {'X-Forwarded-Host': 'forge.example', **headers}
        if signed:
            headers.update(Cookie=SESSION_COOKIE + '=' + self.token)
            headers.setdefault('X-Forge-Action', self.session['csrf'])
        handler = self.handler(path, body, **headers)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        getattr(handler, 'do_POST' if body is not None else 'do_GET')()
        return handler

    def test_signed_out_reads_and_mutations_are_protected_before_services(self):
        with patch.object(dashboard, 'settings_service') as service:
            for path in ('/api/settings', '/api/factory', '/api/factory/output?id=x', '/api/onboarding', '/onboarding/github/callback', '/api/issues'):
                self.request(path).send_error.assert_called_once_with(401)
            for path in ('/api/settings/sync', '/api/onboarding', '/api/repositories/enable', '/api/factory/action', '/api/runtime'):
                self.request(path, {}).send_error.assert_called_once_with(401)
            service.assert_not_called()

    def test_public_session_has_no_action_capability_or_config(self):
        handler = self.request('/api/auth/session')
        result = json.loads(handler._send.call_args.args[0])
        self.assertEqual(result, {'ok': True, 'enabled': True, 'operator': None})
        handler = self.request('/api/auth/session', signed=True)
        self.assertEqual(json.loads(handler._send.call_args.args[0])['controls_token'], self.session['csrf'])

    def test_github_callback_accepts_optional_matching_issuer(self):
        from urllib.parse import urlencode
        original = OperatorAuth.callback
        def callback(auth, *args, **kwargs):
            auth.identity = lambda *args: {'id': 123, 'login': 'operator'}
            return original(auth, *args, **kwargs)
        for issuer in ({}, {'iss': 'https://github.com'}):
            url, cookie = self.auth.begin()
            query = {'state': parse_qs(urlsplit(url).query)['state'][0], 'code': 'code', **issuer}
            with patch.object(OperatorAuth, 'callback', autospec=True, side_effect=callback):
                handler = self.request('/auth/github/callback?' + urlencode(query),
                                       Cookie=FLOW_COOKIE + '=' + read_cookie(cookie, FLOW_COOKIE))
            handler.send_header.assert_any_call('Location', '/')
            cookies = [call.args[1] for call in handler.send_header.call_args_list if call.args[0] == 'Set-Cookie']
            token = next(read_cookie(value, SESSION_COOKIE) for value in cookies if value.startswith(SESSION_COOKIE + '='))
            self.assertEqual(self.auth.session(token)['operator']['id'], 123)

    def test_callback_rejects_wrong_duplicate_or_empty_issuer_before_exchange(self):
        suffixes = ('&iss=https%3A%2F%2Fevil.example', '&iss=',
                    '&iss=https%3A%2F%2Fgithub.com&iss=https%3A%2F%2Fgithub.com',
                    '&unexpected=value', '&code=duplicate')
        for suffix in suffixes:
            with patch.object(OperatorAuth, 'callback') as callback, patch('sys.stdout', new_callable=io.StringIO) as output:
                handler = self.request('/auth/github/callback?state=' + 's' * 43 + '&code=private-code' + suffix)
            callback.assert_not_called()
            handler.send_header.assert_any_call('Location', '/login?error=sign_in')
            self.assertNotIn('private-code', output.getvalue())
            self.assertIn('operator_sign_in_failed', output.getvalue())

    def test_session_csrf_replaces_global_capability(self):
        with patch.object(dashboard, 'settings_service') as service:
            self.request('/api/settings/sync', {}, signed=True, **{'X-Forge-Action': dashboard.CONTROLS_TOKEN}).send_error.assert_called_once_with(403)
            service.assert_not_called()
            self.request('/api/settings/sync', {}, signed=True).send_error.assert_not_called()
            service.return_value.start_refresh.assert_called_once()

    def test_failed_audit_prevents_dispatch(self):
        with patch.object(OperatorAuth, 'audit', side_effect=OSError('disk full')), patch.object(dashboard, 'settings_service') as service:
            handler = self.request('/api/settings/sync', {}, signed=True)
            service.assert_not_called()
            self.assertFalse(json.loads(handler._send.call_args.args[0])['ok'])

    def test_logout_invalidates_cookie_and_persistent_session(self):
        handler = self.request('/api/auth/logout', {}, signed=True)
        self.assertIn('Max-Age=0', handler._send.call_args.kwargs['headers']['Set-Cookie'])
        self.assertIsNone(self.auth.session(self.token))
        self.request('/api/settings', signed=True).send_error.assert_called_once_with(401)

    def test_fail_closed_corrupt_config_and_wrong_public_host(self):
        self.request('/api/auth/session', **{'X-Forwarded-Host': 'evil.example'}).send_error.assert_called_once_with(403)
        self.auth.store.path.write_text('{}')
        self.request('/api/settings', signed=True).send_error.assert_called_once_with(503)
