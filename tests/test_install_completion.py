"""A web handover can finish a pinned installation after the client is upgraded."""
from copy import deepcopy
import unittest
from unittest.mock import Mock, patch

from forge_client.cli import parser
from forge_client.contracts import SetupError, digest
from forge_client.installation import execute
from tests import test_install_coordinator


class CompletionTests(unittest.TestCase):
    setUp = test_install_coordinator.CoordinatorTests.setUp

    def completed_server(self):
        self.install.state['smoke'] = {'repository': {'id': 12, 'name': 'owner/smoke'},
                                       'number': 1, 'smoke_policy': 'auto'}
        self.install.state['stages']['smoke'] = {
            'inputs_digest': digest({'intent': self.install.state['inputs_digest'], 'stage': 'smoke'}),
            'started_at': self.install.state['started_at'], 'status': 'pending',
            'receipt': {'status': 'smoke_pending'}, 'uncertain_write': False}
        self.install.save()
        factory = Mock()
        factory.state = {'intent': {'id': 'installation-id'}, 'factory_binding': {'generation': 'generation-id'}}
        receipt = {'status': 'handover_complete', 'setup_id': 'installation-id', 'generation': 'generation-id',
                   'intent_digest': self.install.state['inputs_digest'], 'repository': self.install.state['smoke']['repository'],
                   'number': 1, 'started_at': self.install.state['started_at'], 'verified_at': self.install.clock(),
                   'repositories': [{**row, 'target': {key: row['target'][key] for key in ('id', 'name')}}
                                    for row in self.install.state['intent']['repositories']]}
        factory.call.side_effect = lambda action: {'phase': 'active'} if action == 'status' else receipt
        patched = patch('forge_client.installation.FactorySetup', return_value=factory)
        patched.start(); self.addCleanup(patched.stop)
        return factory, receipt

    def test_resume_observes_web_completion_before_pinned_preflight(self):
        factory, receipt = self.completed_server()
        self.observe.side_effect = AssertionError('must not replay old revision preflight')
        result = self.install.apply()
        self.assertTrue(result['handover_complete'])
        self.assertEqual(result['completed_at'], receipt['verified_at'])
        self.assertEqual([call.args[0] for call in factory.call.call_args_list], ['status', 'smoke_status'])
        self.host.execute.assert_not_called()
        self.assertEqual(self.journal.read()['stages']['handover']['receipt'], receipt)

    def test_mismatched_receipt_cannot_complete_or_replay_installation(self):
        factory, receipt = self.completed_server()
        original = deepcopy(receipt)
        for key in ('setup_id', 'generation', 'intent_digest', 'repository', 'number', 'started_at', 'repositories'):
            with self.subTest(key=key):
                receipt.clear(); receipt.update(original); receipt[key] = None
                before = self.journal.read()
                with self.assertRaises(SetupError): self.install.apply()
                self.assertEqual(self.journal.read(), before)
                self.assertNotIn('handover', self.install.state['stages'])
        self.observe.assert_not_called(); self.host.execute.assert_not_called()

    def test_unknown_or_unavailable_status_does_not_replay(self):
        factory, receipt = self.completed_server()
        receipt['status'] = 'unknown'
        with self.assertRaises(SetupError): self.install.apply()
        factory.call.side_effect = SetupError('ssh_unavailable', 'Disconnected')
        with self.assertRaises(SetupError): self.install.apply()
        self.observe.assert_not_called(); self.host.execute.assert_not_called()
        self.assertNotIn('handover', self.journal.read()['stages'])

    def test_pending_remote_still_requires_preflight(self):
        factory, receipt = self.completed_server()
        receipt['status'] = 'smoke_pending'
        self.observe.side_effect = SetupError('preflight_required', 'Pinned revision still applies')
        with self.assertRaisesRegex(SetupError, 'Pinned revision'): self.install.apply()
        self.observe.assert_called_once(); self.host.execute.assert_not_called()

    def test_pending_smoke_preserves_wait_and_failed_triage_requires_attention(self):
        factory, receipt = self.completed_server()
        receipt.clear(); receipt.update(status='smoke_pending')
        self.assertFalse(self.install.reconcile_completion())
        self.assertEqual(self.install.status()['pending'][0]['status'], 'smoke_pending')
        receipt.update(status='smoke_triage_failed', proposal_id='failed-proposal')
        self.assertFalse(self.install.reconcile_completion())
        row = self.journal.read()['stages']['smoke']
        self.assertEqual(row['status'], 'needs_user_action')
        self.assertEqual(self.install.status()['pending'][0]['next_action']['command'][-3:], ['failed-proposal', '--wait', '3600'])

    def test_pending_smoke_rejects_conflicting_identity_and_binding(self):
        factory, receipt = self.completed_server()
        expected = {key: value for key, value in receipt.items() if key not in ('status', 'verified_at')}
        receipt.clear(); receipt.update(status='smoke_pending', binding={**expected, 'smoke_policy': 'auto'})
        before = self.journal.read()
        receipt['binding']['generation'] = 'different-generation'
        with self.assertRaises(SetupError): self.install.reconcile_completion()
        self.assertEqual(self.journal.read(), before)
        receipt.clear(); receipt.update(status='smoke_pending', repository={'id': 99, 'name': 'other/private'})
        with self.assertRaises(SetupError): self.install.reconcile_completion()
        self.assertEqual(self.journal.read(), before)

    def test_completed_receipt_requires_valid_time_and_stage_binding(self):
        factory, receipt = self.completed_server()
        completed = receipt['verified_at']
        for bad in (None, float('nan'), True, self.install.state['started_at'] - 1, self.install.clock() + 3600):
            receipt['verified_at'] = bad
            with self.assertRaises(SetupError): self.install.reconcile_completion()
        receipt['verified_at'] = completed
        self.install.state['stages']['smoke']['inputs_digest'] = 'wrong'
        with self.assertRaises(SetupError): self.install.reconcile_completion()
        self.assertNotIn('handover', self.install.state['stages'])

    def test_status_offline_by_default_and_refresh_is_explicit(self):
        factory, _ = self.completed_server()
        with patch('forge_client.installation.Install', return_value=self.install):
            args = parser().parse_args(['install', '--ssh', 'anvil', '--status', '--json'])
            result = execute(args)
            self.assertFalse(result['handover_complete']); factory.call.assert_not_called()
            args = parser().parse_args(['install', '--ssh', 'anvil', '--status', '--refresh', '--json'])
            result = execute(args)
            self.assertTrue(result['handover_complete'])
        self.observe.assert_not_called(); self.host.execute.assert_not_called()

    def test_refresh_cannot_imply_resume(self):
        args = parser().parse_args(['install', '--ssh', 'anvil', '--resume', '--refresh', '--json'])
        with self.assertRaisesRegex(SetupError, 'Use --refresh with --status'): execute(args)

    def test_new_plan_has_no_remote_completion_to_reconcile(self):
        with patch('forge_client.installation.FactorySetup') as factory:
            self.assertFalse(self.install.reconcile_completion())
            factory.assert_not_called()


if __name__ == '__main__':
    unittest.main()
