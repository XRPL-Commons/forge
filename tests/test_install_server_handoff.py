"""The CLI hands preparation to the server before waiting for browser consent."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from forge_client.cli import parser
from forge_client.contracts import SetupError
from forge_client.installation import execute
from forge_client import install_prepare
from tests import test_install_coordinator


class ServerHandoffTests(unittest.TestCase):
    setUp = test_install_coordinator.CoordinatorTests.setUp
    stages = test_install_coordinator.CoordinatorTests.stages

    def server(self):
        factory = Mock()
        factory.state = {'intent': {'id': 'install-id'}, 'factory_binding': {'instance': 'host', 'generation': 'generation-id'}}
        self.stages(factory)
        self.remote = {'available': False}
        self.merged = False
        def call(action, **parameters):
            if action == 'status':
                return {'phase': 'setup', 'installation_preparation': 1, 'harnesses': []}
            if action == 'progress': return {'status': 'progress_saved'}
            if action == 'preparation': return deepcopy(self.remote)
            if action == 'prepare':
                self.remote = self.preparation(parameters['plan'])
                return deepcopy(self.remote)
            if action == 'smoke_status':
                if not self.merged: return {'status': 'smoke_pending'}
                approved = self.install.state['server_preparation']['plan']
                return {'status': 'handover_complete', 'setup_id': 'install-id', 'generation': 'generation-id',
                        'intent_digest': approved['intent_digest'], 'repository': approved['smoke_repository'],
                        'number': self.remote['smoke']['number'], 'started_at': approved['started_at'],
                        'repositories': approved['repositories'], 'verified_at': self.install.clock()}
            raise AssertionError('Unexpected remote operation: ' + action)
        factory.call.side_effect = call
        factory.prepare_repository.return_value = {'status': 'repository_ready'}
        self.factory = factory
        return factory

    def preparation(self, approved, **changes):
        value = {**deepcopy(approved), 'available': True, 'setup_id': 'install-id', 'generation': 'generation-id',
                 'status': 'preparation_pending', 'ready': False, 'needs_operator': [], 'stages': [], 'providers': [], 'smoke': None}
        value.update(changes)
        return value

    def server_smoke(self, *, merged=False):
        approved = self.install.state['server_preparation']['plan']
        self.remote = self.preparation(approved, ready=True, status='preparation_ready',
            smoke={'repository': approved['smoke_repository'], 'number': 7, 'smoke_policy': 'auto'})
        self.merged = merged

    def test_capability_hands_off_after_identity_before_codex_or_repository_work(self):
        factory = self.server()
        events = []; self.install.notify = events.append
        result = self.install.apply()
        self.assertTrue(result['web_owned']); self.assertFalse(result['handover_complete'])
        self.assertEqual(result['setup_url'], 'https://anvil.example.ts.net/installation')
        self.assertEqual(result['stages']['identity']['status'], 'complete')
        self.assertNotIn('account.codex', result['stages'])
        factory.prepare_repository.assert_not_called(); self.smoke.run.assert_not_called()
        self.assertNotIn('device_account', [call.args[0] for call in factory.call.call_args_list])
        self.assertTrue(any(event.get('event') == 'web_setup' for event in events))
        saved = self.journal.read()['server_preparation']['plan']
        self.assertEqual(saved['providers'], [{'id': 'codex', 'kind': 'browser'}])
        self.assertNotIn('github_protection', saved['repositories'][0])

    def test_unconfirmed_handoff_keeps_cli_resume_and_adopts_lost_reply(self):
        factory = self.server()
        original = factory.call.side_effect
        def lose_reply(action, **parameters):
            result = original(action, **parameters)
            if action == 'prepare':
                raise SetupError('connection_lost', 'Reply lost')
            return result
        factory.call.side_effect = lose_reply
        with self.assertRaises(SetupError):
            self.install.apply()
        saved = self.journal.read()['server_preparation']
        self.assertFalse(saved['registered'])
        result = self.install.status()
        self.assertFalse(result['web_owned'])
        self.assertIsNone(result['setup_url'])
        self.assertEqual(result['next_action'], ['forge', 'install', '--ssh', 'anvil', '--resume'])
        from forge_client.installation import display
        lines = []; display(result, tell=lines.append)
        self.assertNotIn('You can close this terminal', '\n'.join(lines))
        self.assertIn('Setup still needs this CLI', '\n'.join(lines))
        factory.call.side_effect = original
        result = self.install.apply()
        self.assertTrue(result['web_owned'])
        self.assertIsNone(result['next_action'])
        self.assertEqual(sum(call.args[0] == 'prepare' for call in factory.call.call_args_list), 1)
        lines = []; display(result, tell=lines.append)
        self.assertIn('You can close this terminal', '\n'.join(lines))

    def test_operator_attention_never_replays_retired_rule_repairs(self):
        factory = self.server(); self.install.apply()
        factory.prepare_repository.assert_not_called()
        approved = self.install.state['server_preparation']['plan']
        self.remote = self.preparation(approved, status='preparation_needs_operator', needs_operator=[
            {'repository': {'id': 10, 'name': 'owner/one'}, 'reason': 'Required rules missing', 'path': '/settings/repositories/10'}])
        factory.prepare_repository.side_effect = lambda name, **options: {'status': 'repository_ready' if options['apply'] else 'needs_setup'}
        self.install.prepare_on_server(factory, self.targets, self.report)
        factory.prepare_repository.assert_not_called()
        # No unrelated repository is repaired and no device login is launched.
        self.assertNotIn('device_account', [call.args[0] for call in factory.call.call_args_list])

    def test_unknown_repair_target_is_rejected_before_administrative_work(self):
        factory = self.server(); self.install.apply()
        approved = self.install.state['server_preparation']['plan']
        self.remote = self.preparation(approved, status='preparation_needs_operator', needs_operator=[
            {'repository': {'id': 999, 'name': 'other/private'}, 'reason': 'Missing rules', 'path': '/settings/repositories'}])
        with self.assertRaises(SetupError): self.install.prepare_on_server(factory, self.targets, self.report)
        factory.prepare_repository.assert_not_called()

    def test_legacy_bound_plan_is_preserved_without_rule_writes(self):
        self.install.state['intent']['rules_repair'] = 'apply-if-admin'
        self.install.state['intent']['repositories'][0]['github_protection'] = 'required'
        from forge_client.contracts import digest
        self.install.state['inputs_digest'] = digest(self.install.state['intent'])
        factory = self.server(); self.install.apply()
        approved = self.install.state['server_preparation']['plan']
        approved['repositories'][0]['github_protection'] = 'required'
        original = deepcopy(approved)
        self.remote = self.preparation(approved, status='preparation_needs_operator', needs_operator=[
            {'repository': {'id': 10, 'name': 'owner/one'}, 'reason': 'External review pending', 'path': '/settings/repositories'}])
        self.install.prepare_on_server(factory, self.targets, self.report)
        self.assertEqual(self.install.state['server_preparation']['plan'], original)
        self.assertEqual(self.install.state['inputs_digest'], digest(self.install.state['intent']))
        factory.prepare_repository.assert_not_called()

    def test_poll_and_resume_follow_server_without_replaying_bootstrap(self):
        factory = self.server(); self.install.apply()
        self.server_smoke()
        self.observe.reset_mock(); self.host.execute.reset_mock(); factory.prepare_repository.reset_mock()
        self.install.apply(poll=True)
        self.assertEqual(self.install.state['smoke']['number'], 7)
        self.install.apply()
        self.observe.assert_not_called(); self.host.execute.assert_not_called()
        factory.prepare_repository.assert_not_called()

    def test_server_attention_without_repository_repairs_does_not_replay_preflight(self):
        factory = self.server(); self.install.apply()
        approved = self.install.state['server_preparation']['plan']
        self.remote = self.preparation(approved, status='preparation_needs_operator', needs_operator=[])
        self.observe.reset_mock(); self.host.execute.reset_mock()
        self.install.apply()
        self.observe.assert_not_called(); self.host.execute.assert_not_called()
        factory.prepare_repository.assert_not_called()

    def test_server_attention_skip_policy_does_not_replay_preflight_or_repair(self):
        from forge_client.contracts import digest
        self.install.state['intent']['rules_repair'] = 'skip'
        self.install.state['inputs_digest'] = digest(self.install.state['intent'])
        factory = self.server(); self.install.apply()
        approved = self.install.state['server_preparation']['plan']
        self.remote = self.preparation(approved, status='preparation_needs_operator', needs_operator=[
            {'repository': {'id': 10, 'name': 'owner/one'}, 'reason': 'Missing rules', 'path': '/settings/repositories'}])
        self.observe.reset_mock(); self.host.execute.reset_mock()
        self.install.apply()
        self.observe.assert_not_called(); self.host.execute.assert_not_called()
        factory.prepare_repository.assert_not_called()

    def test_resume_preserves_attention_without_local_rule_repairs(self):
        factory = self.server(); self.install.apply()
        approved = self.install.state['server_preparation']['plan']
        self.remote = self.preparation(approved, status='preparation_needs_operator', needs_operator=[
            {'repository': {'id': 10, 'name': 'owner/one'}, 'reason': 'Missing rules', 'path': '/settings/repositories'}])
        factory.prepare_repository.side_effect = lambda name, **options: {'status': 'repository_ready' if options['apply'] else 'needs_setup'}
        self.observe.reset_mock(); self.host.execute.reset_mock(); factory.call.reset_mock()
        self.install.apply()
        self.observe.assert_not_called(); self.host.execute.assert_not_called()
        factory.prepare_repository.assert_not_called()
        self.assertNotIn('device_account', [call.args[0] for call in factory.call.call_args_list])
        self.assertEqual(self.install.state['stages']['preparation']['receipt']['status'], 'preparation_needs_operator')

    def test_online_status_adopts_server_smoke_and_normalized_handover(self):
        factory = self.server(); self.install.apply(); self.server_smoke(merged=True)
        self.assertNotIn('smoke', self.install.state)
        self.observe.reset_mock(); self.host.execute.reset_mock()
        with patch('forge_client.installation.Install', return_value=self.install):
            result = execute(parser().parse_args(['install', '--ssh', 'anvil', '--status', '--refresh', '--json']))
        self.assertTrue(result['handover_complete'])
        self.assertEqual(self.journal.read()['smoke']['number'], 7)
        self.observe.assert_not_called(); self.host.execute.assert_not_called()

    def test_mismatched_preparation_or_smoke_cannot_be_adopted(self):
        factory = self.server(); self.install.apply(); self.server_smoke()
        valid = deepcopy(self.remote)
        for field in ('setup_id', 'generation', 'intent_digest', 'repositories', 'smoke_repository'):
            with self.subTest(field=field):
                bad = deepcopy(valid); bad[field] = None
                with self.assertRaises(SetupError): install_prepare.observe(self.install.state, factory, bad)
                self.assertNotIn('smoke', self.install.state)
        for field, invalid in (('repository', {'id': 999, 'name': 'other/private'}), ('number', True), ('smoke_policy', 'human')):
            bad = deepcopy(valid); bad['smoke'][field] = invalid
            with self.assertRaises(SetupError): install_prepare.observe(self.install.state, factory, bad)
            self.assertNotIn('smoke', self.install.state)

    def test_unknown_status_and_non_boolean_ready_are_rejected(self):
        factory = self.server(); self.install.apply()
        for field, invalid in (('status', 'unknown'), ('ready', 'false'), ('ready', True)):
            bad = deepcopy(self.remote); bad[field] = invalid
            with self.subTest(field=field, invalid=invalid), self.assertRaises(SetupError):
                install_prepare.observe(self.install.state, factory, bad)

    def test_human_cli_returns_web_destination_without_waiting(self):
        self.server()
        with patch('forge_client.installation.Install', return_value=self.install), patch('forge_client.installation.time.sleep') as sleep:
            result = execute(parser().parse_args(['install', '--ssh', 'anvil', '--apply', '--wait', '3600']))
        self.assertTrue(result['web_owned']); self.assertEqual(result['setup_url'], 'https://anvil.example.ts.net/installation')
        sleep.assert_not_called()

    def test_json_wait_keeps_following_server_until_handover(self):
        self.server()
        polls = []
        def finish(_):
            polls.append(True)
            self.server_smoke(merged=len(polls) > 1)
        with patch('forge_client.installation.Install', return_value=self.install), patch('forge_client.installation.time.sleep', side_effect=finish) as sleep:
            result = execute(parser().parse_args(['install', '--ssh', 'anvil', '--apply', '--wait', '3600', '--json']))
        self.assertTrue(result['handover_complete']); self.assertEqual(sleep.call_count, 2)


if __name__ == '__main__':
    unittest.main()
