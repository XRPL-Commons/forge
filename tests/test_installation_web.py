from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge_client.contracts import SetupError
from forge.installation_progress import publish
from forge.installation_web import InstallationWeb
from forge.onboarding import Onboarding
from forge.store.configuration import Configuration
from tests.test_sqlite_identity import selected_store


class InstallationWebTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.store = selected_store(self.root)
        self.now = 200
        self.setup = Onboarding(self.root, clock=lambda: self.now)
        self.generation = self.setup.read()['generation']
        self.config = Configuration(self.store, clock=lambda: self.now)
        self.binding = {'setup_id': 'install', 'generation': self.generation, 'started_at': 100,
                        'repository': {'id': 1, 'name': 'owner/smoke'}, 'number': 1,
                        'repositories': [{'target': {'id': 2, 'name': 'owner/project'}}]}
        self.smoke = Mock()
        self.smoke.call.return_value = {'status': 'smoke_pending', 'attempts': {'total': 0, 'charged_failures': 0}}
        self.web = InstallationWeb(self.root, setup=self.setup, clock=lambda: self.now, smoke=self.smoke)

    def bind(self, phase='pending'):
        self.config.replace('installation_smoke', {'binding': self.binding, 'phase': phase},
                            subject=self.generation, expected_revision=0)

    def active(self):
        with self.setup.store.transaction() as state:
            state['phase'] = 'active'

    def request(self, **values):
        return self.web.request({'generation': self.generation, 'action': 'continue', **values}, actor_id='github:7')

    def test_unavailable_has_empty_contract(self):
        view = self.web.view()
        self.assertFalse(view['available']); self.assertEqual(view['stages'], []); self.assertEqual(view['actions'], [])
        with self.assertRaises(SetupError): self.request()

    def test_server_owns_smoke_completion_and_exposes_no_binding(self):
        self.bind()
        snapshot = {'version': 1, 'runner': 'cli', 'state': 'running', 'started_at': 100, 'updated_at': 150,
                    'stages': [{'id': 'smoke', 'label': 'Smoke', 'status': 'complete'}]}
        publish(self.root, snapshot, setup_id='install', generation=self.generation, clock=lambda: self.now)
        view = self.web.view()
        self.assertEqual(view['updated_at'], 200)
        self.assertEqual(view['stages'][0]['status'], 'waiting')
        self.assertEqual(view['smoke']['issue_url'], 'https://github.com/owner/smoke/issues/1')
        self.assertNotIn('binding', view['smoke'])
        self.assertFalse(view['complete'])

    def test_progress_rejects_unknown_fields_and_wrong_generation(self):
        snapshot = {'version': 1, 'runner': 'cli', 'state': 'waiting', 'started_at': 100, 'updated_at': 150, 'stages': []}
        for value in ({**snapshot, 'secret': 'never'}, {**snapshot, 'stages': [{'id': 'identity', 'label': 'Identity', 'status': 'complete', 'receipt': {}}]}):
            with self.assertRaises(SetupError):
                publish(self.root, value, setup_id='install', generation=self.generation)
        with self.assertRaises(SetupError):
            publish(self.root, snapshot, setup_id='install', generation='stale')
        self.assertIsNone(self.config.get('installation_progress', self.generation))

    def test_continue_is_durable_idempotent_and_uses_exact_binding(self):
        self.bind(); self.active()
        self.request(); self.request()
        record = self.config.get('installation_web', self.generation)
        self.assertEqual(record['revision'], 1)
        replacement = InstallationWeb(self.root, setup=self.setup, clock=lambda: self.now, smoke=self.smoke)
        replacement.tick()
        self.smoke.call.assert_any_call('launch', {}, setup_id='install', generation=self.generation)
        self.assertEqual(self.config.get('installation_web', self.generation)['settings']['status'], 'waiting')

    def test_handover_requires_smoke_evidence_and_uses_existing_verifier(self):
        self.bind(); self.active(); self.request()
        self.smoke.call.side_effect = [{'status': 'smoke_merged'}, {'status': 'handover_complete'}]
        self.web.tick()
        self.assertEqual([call.args[0] for call in self.smoke.call.call_args_list[-2:]], ['launch', 'handover'])
        self.assertEqual(self.config.get('installation_web', self.generation)['settings']['status'], 'complete')

    def test_retry_is_bound_to_failed_proposal_and_not_automatic(self):
        self.bind(); self.active()
        self.smoke.call.return_value = {'status': 'smoke_triage_failed', 'proposal_id': 'failed-1', 'reason': 'Planner stopped'}
        with self.assertRaises(SetupError): self.request(action='retry', proposal_id='wrong')
        self.request(action='retry', proposal_id='failed-1')
        self.smoke.call.side_effect = [{'status': 'smoke_retry_queued'}, {'status': 'smoke_pending'}]
        self.web.tick()
        self.smoke.call.assert_any_call('retry', {'proposal_id': 'failed-1'}, setup_id='install', generation=self.generation)
        self.assertEqual(self.config.get('installation_web', self.generation)['settings']['action'], 'continue')

    def test_busy_retry_retains_intent_until_the_retry_is_confirmed(self):
        self.bind(); self.active()
        self.smoke.call.return_value = {'status': 'smoke_triage_failed', 'proposal_id': 'failed-1'}
        self.request(action='retry', proposal_id='failed-1')
        self.smoke.reset_mock()
        self.smoke.call.return_value = {'status': 'smoke_pending', 'phase': 'controller'}
        self.web.tick()
        self.smoke.call.assert_called_once_with('retry', {'proposal_id': 'failed-1'},
                                               setup_id='install', generation=self.generation)
        saved = self.config.get('installation_web', self.generation)['settings']
        self.assertEqual(saved['action'], 'retry'); self.assertEqual(saved['status'], 'waiting')
        self.now += 20
        self.smoke.call.side_effect = [{'status': 'smoke_retry_queued'}, {'status': 'smoke_pending'}]
        self.web.tick()
        self.assertEqual(self.config.get('installation_web', self.generation)['settings']['action'], 'continue')
        self.assertEqual(self.smoke.call.call_args.args[0], 'launch')

    def test_reset_before_tick_lock_cannot_apply_old_repository_to_new_setup(self):
        from contextlib import contextmanager
        from forge.sqlite_identity import identity_effect_lock
        self.bind(); self.request()
        @contextmanager
        def reset_then_lock(store):
            with identity_effect_lock(store):
                with self.setup.store.transaction() as state:
                    state['generation'] = 'new-generation'
                    state['phase'] = 'setup'
                yield
        with patch('forge.installation_web.identity_effect_lock', reset_then_lock), \
                patch.object(self.setup, 'select') as select, patch.object(self.setup, 'queue') as queue:
            self.web.tick()
        select.assert_not_called(); queue.assert_not_called()
        self.assertIsNone(self.config.get('installation_web', 'new-generation'))

    def test_setup_selection_and_queue_share_identity_lock(self):
        from forge.sqlite_identity import _locks
        self.bind(); self.request()
        held = []
        def capture(*args):
            held.append(str(self.store.root.absolute()) in _locks.held)
        with patch.object(self.setup, 'select', side_effect=capture), \
                patch.object(self.setup, 'queue', side_effect=capture), \
                patch.object(self.setup, 'view', return_value={'can_start': False, 'revision': 'ready'}):
            self.web.tick()
        self.assertEqual(held, [True, True])

    def test_service_failure_is_safe_and_needs_operator_retry(self):
        self.bind(); self.active(); self.request()
        self.smoke.call.side_effect = RuntimeError('secret from upstream')
        self.web.tick()
        record = self.config.get('installation_web', self.generation)['settings']
        self.assertEqual(record['error'], 'installation_failed')
        self.assertEqual(record['status'], 'failed')
        count = self.smoke.call.call_count; self.now += 20; self.web.tick()
        self.assertEqual(self.smoke.call.call_count, count)

    def test_stale_generation_and_extra_repository_cannot_be_submitted(self):
        self.bind()
        with self.assertRaises(SetupError): self.request(generation='stale')
        with self.assertRaises(SetupError): self.request(repository={'id': 99, 'name': 'other/private'})
        self.assertIsNone(self.config.get('installation_web', self.generation))

    def test_worker_heartbeat_is_separate_from_changed_progress(self):
        self.bind(); self.active(); self.request(); self.web.tick()
        first = self.config.get('installation_web', self.generation)['settings']
        self.now += 20; self.web.tick()
        following = self.config.get('installation_web', self.generation)['settings']
        self.assertEqual(first['updated_at'], following['updated_at'])
        self.assertEqual(following['last_seen_at'], 220)

    def test_maintenance_pause_stops_browser_continuation(self):
        from forge.onboarding import control_in
        self.bind(); self.active()
        with self.store.write() as db: control_in(db, 'factory_paused', True, self.now)
        with self.assertRaises(SetupError): self.request()
        self.assertIsNone(self.config.get('installation_web', self.generation))

    def test_reset_stops_durable_request(self):
        self.bind(); self.active(); self.request()
        with self.setup.store.transaction() as state: state['phase'] = 'resetting'
        self.smoke.reset_mock(); self.web.tick(); self.smoke.call.assert_not_called()


class InstallationHTTPTests(unittest.TestCase):
    from tests.test_settings_http import SettingsHTTPTests as Fixture
    setUp = Fixture.setUp
    handler = Fixture.handler

    def test_progress_requires_trusted_operator_session(self):
        import json
        with patch('forge.installation_web.InstallationWeb') as service:
            service.return_value.view.return_value = {'available': True, 'stages': [], 'actions': []}
            handler = self.handler('/api/installation', Host='attacker.example')
            handler.do_GET(); handler.send_error.assert_called_once_with(403)
            service.assert_not_called()
            handler = self.handler('/api/installation'); handler.do_GET()
            self.assertTrue(json.loads(handler._send.call_args.args[0])['available'])
            self.assertIn('controls_token', json.loads(handler._send.call_args.args[0]))

    def test_continuation_uses_normal_csrf_and_operator_identity(self):
        value = {'generation': 'g', 'action': 'continue'}
        with patch('forge.installation_web.InstallationWeb') as service:
            service.return_value.request.return_value = {'available': True}
            handler = self.handler('/api/installation', value, Origin='http://attacker.example')
            handler.do_POST(); handler.send_error.assert_called_once_with(403)
            service.assert_not_called()
            handler = self.handler('/api/installation', value); handler.do_POST()
            self.assertEqual(service.return_value.request.call_args.args, (value,))
            self.assertTrue(service.return_value.request.call_args.kwargs['actor_id'].startswith('github:'))
