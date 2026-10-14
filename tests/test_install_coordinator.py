"""Saved installation intent gates mutation and keeps independent repository receipts."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge_client.contracts import SetupError, digest
from forge_client.installation import Install
from forge_client.preflight import finding
from forge_client.workflow import Journal

REVISION = 'a' * 40


def interview():
    return {'version': 1, 'ssh': 'anvil', 'name': 'Anvil', 'revision': REVISION, 'host_key': None,
            'hostname': 'anvil', 'owner': 'owner', 'operators': ['operator'],
            'repositories': [{'name': 'owner/one', 'merge_policy': 'human', 'allow_workflow_edits': False},
                             {'name': 'owner/two', 'merge_policy': 'auto', 'allow_workflow_edits': False}],
            'providers': [{'id': 'codex', 'kind': 'browser'}], 'rules_repair': 'apply-if-admin',
            'smoke_target': 'scratch', 'codex_parallel_auth': True}


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.journal = Journal('install', 'anvil', root=Path(directory.name))
        self.github = Mock()
        self.github.registration_owner.side_effect = lambda login: {'id': 1 if login == 'operator' else 2, 'login': login, 'type': 'User'}
        self.github.repository.side_effect = lambda name: {'id': 10 if name.endswith('one') else 11, 'name': name, 'branch': 'main', 'admin': True, 'owner_id': 2, 'owner_type': 'User'}
        self.host = Mock(); self.host.execute.return_value = {'status': 'installed'}
        self.report = {'status': 'preflight', 'items': [], 'repositories': []}
        self.observe = Mock(side_effect=lambda *_: deepcopy(self.report))
        self.install = Install('anvil', github=self.github, installation=self.host, journal=self.journal, preflight=self.observe)
        self.install.plan(interview())

    def test_changed_input_and_injected_secret_are_rejected_before_host_work(self):
        changed = interview(); changed['repositories'][0]['merge_policy'] = 'auto'
        with self.assertRaises(SetupError): self.install.plan(changed)
        self.host.execute.assert_not_called()
        self.assertNotIn('secret', self.journal.path.read_text())
        supplied = interview(); supplied['github_token'] = 'secret'
        other = Install('other', github=self.github, installation=self.host, journal=Journal('other', 'other', root=self.journal.root))
        supplied['ssh'] = 'other'
        with self.assertRaises(SetupError): other.plan(supplied)
        self.host.execute.assert_not_called()

    def test_failed_preflight_has_no_remote_mutation_and_cannot_waive_host_identity(self):
        self.report['items'] = [finding('host.identity', 'needs_you', 'Mismatch')]
        self.assertEqual(self.install.apply()['status'], 'preflight_blocked')
        self.host.execute.assert_not_called()
        with self.assertRaises(SetupError): self.install.apply(waivers=['host.identity'])
        self.host.execute.assert_not_called()

    def test_resume_reobserves_preflight_and_does_not_trust_old_pass(self):
        self.install.preflight()
        self.report['items'] = [finding('host.disk', 'needs_you', 'Disk became full')]
        resumed = Install('anvil', github=self.github, installation=self.host, journal=self.journal, preflight=self.observe)
        self.assertEqual(resumed.apply()['status'], 'preflight_blocked')
        self.assertEqual(self.observe.call_count, 2)
        self.host.execute.assert_not_called()

    def test_lost_host_response_is_observed_complete_before_any_reapply(self):
        self.install.stage('host', lambda: {'status': 'planned'}, Mock(side_effect=SetupError('ssh_unavailable', 'Connection lost')), complete=lambda row: row.get('status') == 'installed')
        self.assertTrue(self.install.state['stages']['host']['uncertain_write'])
        resumed = Install('anvil', github=self.github, installation=self.host, journal=self.journal, preflight=self.observe)
        writer = Mock()
        resumed.stage('host', lambda: {'status': 'installed'}, writer, complete=lambda row: row.get('status') == 'installed')
        writer.assert_not_called()
        self.assertFalse(resumed.state['stages']['host']['uncertain_write'])

    def test_independent_repositories_continue_and_neither_is_started(self):
        network = Mock(); network.execute.return_value = {'status': 'network_ready'}
        factory = Mock()
        factory.call.return_value = {'instance': 'instance', 'roles': {'controller': {'registered': True}},
                                     'harnesses': [{'id': 'codex', 'connected': True}]}
        def prepare(name, **options):
            if not options.get('apply'): return {'status': 'planned'}
            if name.endswith('one'): return {'status': 'write_unconfirmed'}
            return {'status': 'repository_ready'}
        factory.prepare_repository.side_effect = prepare
        with patch('forge_client.installation.Network', return_value=network), patch('forge_client.installation.FactorySetup', return_value=factory):
            result = self.install.apply()
        self.assertTrue(result['stages']['repository.10']['uncertain_write'])
        self.assertEqual(result['stages']['repository.11']['status'], 'complete')
        self.assertFalse(result['handover_complete'])
        self.assertEqual(result['stages']['handover']['receipt']['status'], 'smoke_required')
        self.assertTrue(all(not call.kwargs.get('initial', True) for call in factory.prepare_repository.call_args_list))
        factory.check.assert_not_called()
        self.assertEqual(result['intent']['repositories'][0]['merge_policy'], 'human')

    def test_mutated_saved_intent_cannot_change_stage_authority(self):
        self.install.state['intent']['repositories'][0]['target']['id'] = 123
        with self.assertRaises(SetupError): self.install.apply()
        self.observe.assert_not_called()
