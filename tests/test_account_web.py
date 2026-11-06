"""Post-install browser recovery shares device ownership and scoped verification."""
import unittest
from unittest.mock import Mock, patch
from forge.account_web import AccountWeb
from forge_client.contracts import SetupError
from tests.test_installation_web import InstallationWebTests, InstallationHTTPTests


class AccountWebTests(unittest.TestCase):
    active = InstallationWebTests.active
    def setUp(self):
        InstallationWebTests.setUp(self)
        self.active()
        self.device = Mock()
        self.device.call.return_value = {'status': 'account_login_pending', 'provider': 'codex'}
        self.web = AccountWeb(self.root, setup=self.setup, clock=lambda: self.now, device=self.device)
        self.harnesses = patch.object(self.setup, 'harnesses', return_value=[{'id': 'codex', 'connected': False, 'models': []}]).start()
        self.addCleanup(patch.stopall)

    # Do not inherit installation-specific assertions.
    def request_account(self, action='connect', **changes):
        view = self.web.view()
        value = {key: view[key] for key in ('generation', 'revision', 'setup_revision')}
        return self.web.request({**value, 'action': action, 'provider': 'codex', **changes}, actor_id='github:7')

    def test_durable_connect_survives_reload_without_duplicate_start(self):
        first = self.request_account()
        self.assertEqual(first['operation']['status'], 'queued')
        self.device.call.assert_not_called()
        self.request_account()
        self.web.tick()
        self.assertEqual(self.web.view()['operation']['status'], 'connecting')
        resumed = AccountWeb(self.root, setup=self.setup, device=self.device)
        resumed.tick()
        self.assertEqual(self.device.call.call_count, 2)
        self.assertEqual(self.device.call.call_args_list[0].kwargs['setup_id'], self.device.call.call_args_list[1].kwargs['setup_id'])

    def test_stale_generation_revision_and_unsupported_provider_refused(self):
        for changes in ({'generation': 'old'}, {'revision': 99}, {'setup_revision': 'old'}, {'provider': 'claude'}):
            with self.assertRaises(SetupError): self.request_account(**changes)
        self.device.call.assert_not_called()

    def test_duplicate_old_request_cannot_restart_the_accepted_login(self):
        view = self.web.view()
        value = {key: view[key] for key in ('generation', 'revision', 'setup_revision')}
        value.update(provider='codex', action='connect')
        self.web.request(value, actor_id='github:7')
        with self.assertRaises(SetupError): self.web.request(value, actor_id='github:7')
        self.device.call.assert_not_called()

    def test_reset_or_pause_does_not_advance_login(self):
        self.request_account()
        with self.setup.store.transaction() as data: data['phase'] = 'resetting'
        self.web.tick(); self.device.call.assert_not_called()
        with self.assertRaises(SetupError): self.request_account()

    def test_expired_login_requires_explicit_retry(self):
        self.request_account()
        self.device.call.return_value = {'status': 'account_login_failed'}
        self.web.tick()
        self.assertEqual(self.web.view()['operation']['status'], 'failed')
        count = self.device.call.call_count
        self.web.tick(); self.assertEqual(count, self.device.call.call_count)
        self.request_account('retry'); self.web.tick()
        self.assertTrue(self.device.call.call_args.kwargs['retry'])

    def test_retry_after_runtime_interruption_resumes_unexpired_login(self):
        with self.setup.store.transaction() as data:
            data['device_logins'] = {'codex': {'id': 'saved', 'phase': 'running', 'expires_at': 900,
                                              'setup_id': 'a'*32}}
        self.request_account('retry')
        self.web.tick()
        self.assertFalse(self.device.call.call_args.kwargs['retry'])
        self.assertFalse(self.device.call.call_args.kwargs['reconnect'])
        self.assertEqual(self.device.call.call_args.kwargs['setup_id'], 'a'*32)

    def test_completed_login_queues_only_codex_verification(self):
        self.request_account()
        self.device.call.return_value = {'status': 'connected'}
        with patch.object(self.setup, 'queue') as queue:
            self.web.tick()
        self.assertEqual(queue.call_args.args[0], 'harness_check')
        self.assertEqual(queue.call_args.kwargs, {'harness': 'codex'})
        self.assertEqual(self.web.view()['operation']['status'], 'verifying')
        self.harnesses.return_value = [{'id': 'codex', 'connected': True, 'models': [{'status': 'passed'}]}]
        self.web.tick()
        self.assertEqual(self.web.view()['operation']['status'], 'complete')

    def test_observation_filters_provider_output(self):
        with self.setup.store.transaction() as data:
            data['device_logins'] = {'codex': {'id': 'a', 'setup_id': 'a'*32}}
        self.device.call.return_value = {'provider': 'codex', 'status': 'account_login_required',
                                         'code': 'ABCD-12345', 'secret': 'never', 'reason': 'private output'}
        view = self.web.view()
        self.assertEqual(view['account']['code'], 'ABCD-12345')
        self.assertNotIn('secret', view['account']); self.assertNotIn('reason', view['account'])


class AccountHTTPTests(unittest.TestCase):
    setUp = InstallationHTTPTests.setUp
    handler = InstallationHTTPTests.handler

    def test_accounts_require_operator_and_csrf(self):
        value = {'generation': 'g', 'revision': 0, 'setup_revision': 'r', 'provider': 'codex', 'action': 'connect'}
        with patch('forge.account_web.AccountWeb') as service:
            service.return_value.view.return_value = {'active': True}
            service.return_value.request.return_value = {'active': True}
            handler = self.handler('/api/accounts', Host='attacker.example'); handler.do_GET()
            handler.send_error.assert_called_once_with(403); service.assert_not_called()
            handler = self.handler('/api/accounts', value, Origin='http://attacker.example'); handler.do_POST()
            handler.send_error.assert_called_once_with(403); service.assert_not_called()
            handler = self.handler('/api/accounts', value); handler.do_POST()
            self.assertTrue(service.return_value.request.call_args.kwargs['actor_id'].startswith('github:'))

    def test_revoked_operator_session_cannot_read_codes_or_connect(self):
        with patch('forge.operator_auth.OperatorAuth') as auth, patch('forge.account_web.AccountWeb') as service:
            auth.return_value.config.return_value = {'origin': 'http://127.0.0.1:8787'}
            auth.return_value.session.return_value = None
            for body, method in ((None, 'do_GET'), ({}, 'do_POST')):
                handler = self.handler('/api/accounts', body)
                getattr(handler, method)()
                handler.send_error.assert_called_once_with(401)
            service.assert_not_called()


class AccountLockTests(unittest.TestCase):
    from tests.test_installation_accounts import DeviceLoginTests as Fixture
    setUp = Fixture.setUp
    call = Fixture.call

    def web(self):
        self.call(apply=True)
        config = self.setup.runtime()
        with self.setup.store.transaction() as data: data['phase'] = 'active'
        self.setup.save_runtime(config, active=True)
        web = AccountWeb(self.root, setup=self.setup, device=self.client, clock=lambda: self.now)
        view = web.view()
        web.request({**{key: view[key] for key in ('generation', 'revision', 'setup_revision')},
                     'provider': 'codex', 'action': 'connect'}, actor_id='github:7')
        return web

    def test_concurrent_get_waiting_on_identity_cannot_fail_tick(self):
        import threading
        from contextlib import contextmanager
        from forge.sqlite_identity import identity_effect_lock
        web = self.web()
        observing, ready, proceed = threading.Event(), threading.Event(), threading.Event()
        results = []
        original_call = self.client.call
        def observed_call(*args, **kwargs):
            if threading.current_thread().name == 'account-get':
                ready.set()
                if not proceed.wait(3): raise AssertionError('GET was not released')
            return original_call(*args, **kwargs)
        @contextmanager
        def observed_lock(root):
            if threading.current_thread().name == 'account-get': observing.set()
            with identity_effect_lock(root): yield
        with patch.object(self.client, 'call', side_effect=observed_call), \
             patch('forge.installation_accounts.identity_effect_lock', observed_lock):
            thread = threading.Thread(name='account-get', target=lambda: results.append(web.view()))
            thread.start()
            try:
                self.assertTrue(ready.wait(3), 'GET did not read its saved operation')
                with identity_effect_lock(self.root):
                    proceed.set()
                    self.assertTrue(observing.wait(3), 'GET did not reach the identity lock')
                    web.tick()
                    self.assertEqual(web.saved(self.generation)['settings']['status'], 'connecting')
            finally:
                proceed.set()
                thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(results[0]['account']['code'], 'ABCD-12345')
        self.assertEqual(sum(call[1] == 'create' for call in self.calls), 1)

    def test_provider_lock_contention_leaves_login_pending(self):
        from forge.host_setup import login_lock
        web = self.web()
        with login_lock(self.root, 'codex'): web.tick()
        self.assertEqual(web.saved(self.generation)['settings']['status'], 'queued')
        web.tick()
        self.assertEqual(web.saved(self.generation)['settings']['status'], 'connecting')

    def test_expired_login_can_be_retried_before_supervisor_observes_expiry(self):
        web = self.web(); web.tick()
        previous = self.setup.read()['device_logins']['codex']['id']
        self.now += 901
        view = web.view()
        web.request({**{key: view[key] for key in ('generation', 'revision', 'setup_revision')},
                     'provider': 'codex', 'action': 'retry'}, actor_id='github:7')
        self.assertEqual(web.saved(self.generation)['settings']['status'], 'queued')
        web.tick()
        self.assertNotEqual(previous, self.setup.read()['device_logins']['codex']['id'])
