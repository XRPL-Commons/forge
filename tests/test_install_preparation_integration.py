"""Preparation crosses the administrative SSH, web and admission boundaries."""
import unittest
from unittest.mock import Mock, patch

from forge_client.contracts import SetupError
from forge.installation_setup import InstallationSetup
from forge.installation_web import InstallationWeb
from forge.install_scope import check_repositories_in, execution_allowed, issue_allowed, pending
from tests import test_installation_prepare as fixtures


class PreparationIntegrationTests(unittest.TestCase):
    approve = fixtures.PreparationTests.approve

    def setUp(self):
        fixtures.PreparationTests.setUp(self)
        self.web = InstallationWeb(self.root, setup=self.setup, clock=lambda: self.now)
        status = patch.object(self.setup, 'service_status', return_value={'available': True})
        status.start(); self.addCleanup(status.stop)

    def request(self, action='continue', **overrides):
        return self.web.request({'generation': self.generation, 'action': action, **overrides}, actor_id='github:7')

    def test_ssh_registration_queues_durable_web_operation(self):
        handoff = Mock()
        ssh = InstallationSetup(self.root, setup=self.setup, handoff=handoff)
        view = ssh.call({'version': 1, 'setup_id': self.setup_id, 'actor_id': 7,
            'generation': self.generation, 'action': 'prepare', 'parameters': {'plan': self.plan}})
        self.assertTrue(view['available']); self.assertFalse(view['ready'])
        operation = self.configuration.get('installation_web', self.generation)['settings']
        self.assertEqual(operation['status'], 'queued')
        self.assertEqual(operation['setup_id'], self.setup_id)
        self.assertEqual(operation['generation'], self.generation)
        handoff.operator.assert_called_once_with(7)
        # Recreating the web service recovers the same request without the CLI.
        self.assertEqual(self.web.view()['operation']['status'], 'queued')
        self.assertIsNone(self.configuration.get('installation_smoke', self.generation))

    def test_web_shows_preparation_and_coding_consent_before_smoke_binding(self):
        self.connected = False; self.approve()
        with patch('forge.installation_web.DeviceAccount') as account:
            account.return_value.call.return_value = {'status': 'account_login_required', 'provider': 'codex',
                'private_output': 'never return raw output'}
            view = self.web.view()
        self.assertTrue(view['available']); self.assertFalse(view['complete'])
        self.assertEqual(view['source'], 'server')
        self.assertEqual(view['started_at'], self.plan['started_at'])
        self.assertEqual(view['repositories'], self.targets)
        self.assertIsNone(view['smoke'])
        self.assertIn('environment', [stage['id'] for stage in view['stages']])
        self.assertIn('connect_account', [action['id'] for action in view['actions']])
        self.assertNotIn('private_output', view['account'])

    def test_browser_account_action_requires_exact_generation_and_approved_provider(self):
        with patch('forge.installation_web.DeviceAccount') as account:
            with self.assertRaises(SetupError): self.request('connect_account')
            self.plan['providers'] = [{'id': 'claude', 'kind': 'token'}]; self.approve()
            with self.assertRaises(SetupError): self.request('connect_account')
            with self.assertRaises(SetupError): self.request('connect_account', generation='old')
            account.assert_not_called()

    def test_coding_account_action_uses_bound_setup_and_drops_private_output(self):
        self.connected = False; self.approve()
        with patch('forge.installation_web.DeviceAccount') as account:
            account.return_value.call.return_value = {'status': 'account_login_pending', 'provider': 'codex',
                'code': 'TEST-CODE', 'url': 'https://auth.openai.com/codex/device', 'secret': 'never'}
            view = self.request('connect_account')
        account.return_value.call.assert_any_call('codex', setup_id=self.setup_id,
            generation=self.generation, apply=True, retry=False)
        self.assertEqual(view['account']['code'], 'TEST-CODE')
        self.assertNotIn('secret', view['account'])

    def test_completed_device_login_is_collected_even_when_preparation_is_blocked(self):
        self.connected = False; self.approve()
        with self.setup.store.transaction() as data:
            data['device_logins'] = {'codex': {'setup_id': self.setup_id, 'generation': self.generation}}
        self.configuration.replace('installation_web', {'status': 'blocked', 'updated_at': self.now,
            'action': 'continue', 'setup_id': self.setup_id, 'generation': self.generation},
            subject=self.generation, expected_revision=0)
        with patch.object(self.web, 'account', side_effect=[{'status': 'account_login_pending'}, {'status': 'connected'}]) as account, \
                patch.object(self.web.preparation, 'tick') as preparation:
            self.web.tick()
        self.assertEqual(account.call_count, 2)
        self.assertTrue(account.call_args.kwargs['apply'])
        preparation.assert_not_called()

    def test_preparation_fence_blocks_all_repositories_until_exact_smoke_binding(self):
        self.approve()
        self.assertEqual(pending(self.store)['phase'], 'preparing')
        for target in self.targets + [self.smoke]:
            self.assertFalse(issue_allowed(self.store, target['id'], 1))
            self.assertFalse(issue_allowed(self.store, target['id'], None))
            self.assertFalse(execution_allowed(self.store, {'repo': target['name'], 'number': 1}))
            with self.store.read_snapshot() as db:
                with self.assertRaises(ValueError):
                    check_repositories_in(db, [dict(target, enabled=True)])
                check_repositories_in(db, [dict(target, enabled=False)])
        binding = {'repository': self.smoke, 'number': 1, 'started_at': self.plan['started_at']}
        revision = self.configuration.replace('installation_smoke', {'phase': 'pending', 'binding': binding},
            subject=self.generation, expected_revision=0)
        self.assertTrue(issue_allowed(self.store, self.smoke['id'], 1))
        self.assertFalse(issue_allowed(self.store, self.targets[0]['id'], 1))
        with self.store.read_snapshot() as db:
            check_repositories_in(db, [dict(self.smoke, enabled=True)])
            with self.assertRaises(ValueError): check_repositories_in(db, [dict(self.targets[0], enabled=True)])
        # Confirmed handover opens admission even though the older preparation
        # row remains as installation history.
        self.configuration.replace('installation_smoke', {'phase': 'complete', 'binding': binding},
            subject=self.generation, expected_revision=revision)
        self.assertIsNone(pending(self.store))
        self.assertTrue(issue_allowed(self.store, self.targets[0]['id'], 1))

    def test_new_setup_generation_does_not_inherit_previous_fence_or_queue(self):
        self.approve(); self.request()
        with self.setup.store.transaction() as data: data['generation'] = 'replacement'
        self.assertIsNone(pending(self.store))
        self.assertFalse(self.web.preparation.view()['available'])
        with patch.object(self.web.preparation, 'tick') as prepare:
            self.web.tick(); prepare.assert_not_called()

    def test_busy_account_read_keeps_progress_visible_and_never_starts_login(self):
        self.connected = False; self.approve()
        with patch('forge.installation_web.DeviceAccount') as account:
            account.return_value.call.side_effect = ValueError('private account runtime diagnostic')
            view = self.web.view()
            self.assertTrue(view['available'])
            self.assertEqual(view['account'], {'status': 'account_unavailable', 'provider': 'codex'})
            self.assertNotIn('private account runtime diagnostic', str(view))
            self.assertFalse(any(action['id'] in ('connect_account', 'retry_account') for action in view['actions']))
            self.request()
            with self.setup.store.transaction() as data:
                data['device_logins'] = {'codex': {'setup_id': self.setup_id, 'generation': self.generation}}
            with patch.object(self.web.preparation, 'tick') as prepare:
                self.web.tick()
                prepare.assert_called_once_with()
            self.assertTrue(all(not call.kwargs['apply'] for call in account.return_value.call.call_args_list))

    def test_failed_account_handoff_is_sanitized_without_escaping_service_tick(self):
        self.connected = False; self.approve()
        with patch('forge.installation_web.DeviceAccount') as account:
            account.return_value.call.return_value = {'status': 'account_login_pending', 'provider': 'codex'}
            self.request()
            with self.setup.store.transaction() as data:
                data['device_logins'] = {'codex': {'setup_id': self.setup_id, 'generation': self.generation}}
            def login(*args, **kwargs):
                if kwargs['apply']: raise OSError('credential bytes must never enter progress')
                return {'status': 'account_login_saved', 'provider': 'codex'}
            account.return_value.call.side_effect = login
            self.web.tick()
        operation = self.configuration.get('installation_web', self.generation)['settings']
        self.assertEqual(operation['status'], 'failed')
        self.assertEqual(operation['error'], 'installation_failed')
        self.assertNotIn('credential bytes', str(operation))

    def test_connected_account_projection_updates_while_repository_repair_is_blocked(self):
        self.connected = False; self.approve()
        saved = self.service.saved()
        value = saved['settings']
        value['stages']['account.codex'].update(status='waiting', reason='Connect this coding account.')
        value['stages']['repository.1'].update(status='needs_operator', reason='Repair rules.')
        self.configuration.replace('installation_prepare', value, subject=self.generation,
                                   expected_revision=saved['revision'])
        self.connected = True
        view = self.web.view()
        account = next(row for row in view['stages'] if row['id'] == 'account.codex')
        self.assertEqual(account['status'], 'complete')
        self.assertNotIn('reason', account)
        self.assertNotIn('completed_at', account)
        self.assertEqual(view['preparation']['status'], 'preparation_needs_operator')
        persisted = self.service.saved()['settings']['stages']['account.codex']
        self.assertEqual(persisted['status'], 'waiting')
