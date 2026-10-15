"""Saved installation intent gates mutation and keeps independent repository receipts."""
from copy import deepcopy
from contextlib import ExitStack
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

    def stages(self, factory):
        stack = ExitStack(); self.addCleanup(stack.close)
        network = Mock(); network.execute.return_value = {'status': 'network_ready'}
        self.targets = {'status': 'targets_ready', 'smoke': {'id': 12, 'name': 'owner/smoke'},
                        'publication': {'id': 13, 'name': 'owner/probe'},
                        'repositories': [{'id': n, 'name': 'owner/' + name}
                                         for n, name in ((10, 'one'), (11, 'two'), (12, 'smoke'), (13, 'probe'))]}
        consent = Mock(); consent.observe.return_value = {'status': 'consent_ready'}
        self.smoke = Mock()
        self.smoke.prepare.return_value = {'status': 'smoke_repository_ready'}
        self.smoke.run.return_value = {'status': 'smoke_pending'}
        self.smoke.handover.return_value = {'status': 'smoke_pending'}
        patches = {
            'forge_client.installation.Network': network,
            'forge_client.installation.FactorySetup': factory,
            'forge_client.install_targets.prepare_targets': self.targets,
            'forge_client.install_consent.InstallConsent': consent,
            'forge_client.install_identity.configure': {'status': 'identity_ready', 'origin': 'https://anvil.example.ts.net'},
            'forge_client.install_publication.qualify': {'status': 'qualified'},
            'forge_client.install_smoke.InstallSmoke': self.smoke,
        }
        for name, result in patches.items(): stack.enter_context(patch(name, return_value=result))

    def factory(self):
        factory = Mock()
        view = {'instance': 'instance', 'phase': 'setup',
                                     'roles': {'controller': {'registered': True}},
                                     'harnesses': [{'id': 'codex', 'connected': True}]}
        factory.call.side_effect = lambda action, **kw: {'status': 'connected'} if action == 'device_account' else view
        factory.prepare_repository.return_value = {'status': 'repository_ready'}
        self.stages(factory)
        return factory

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

    def test_other_repository_owner_is_rejected_before_saving_or_host_work(self):
        self.github.repository.side_effect = lambda name: {'id': 20, 'name': name, 'branch': 'main', 'admin': True, 'owner_id': 99, 'owner_type': 'User'}
        supplied = interview(); supplied['repositories'] = supplied['repositories'][:1]
        journal = Journal('other-owner', 'anvil', root=self.journal.root)
        workflow = Install('anvil', github=self.github, installation=self.host, journal=journal)
        with self.assertRaisesRegex(SetupError, 'selected GitHub owner'):
            workflow.plan(supplied)
        self.assertIsNone(journal.read())
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
        factory = self.factory()
        def prepare(name, **options):
            if not options.get('apply'): return {'status': 'planned', 'plan': {'steps': ['prepare'], 'blockers': []}}
            if name.endswith('one'): return {'status': 'write_unconfirmed'}
            return {'status': 'repository_ready'}
        factory.prepare_repository.side_effect = prepare
        result = self.install.apply()
        self.assertTrue(result['stages']['repository.10']['uncertain_write'])
        self.assertEqual(result['stages']['repository.11']['status'], 'complete')
        self.assertFalse(result['handover_complete'])
        self.assertNotIn('handover', result['stages'])
        self.smoke.run.assert_not_called()
        self.assertTrue(all(not call.kwargs.get('initial', True) for call in factory.prepare_repository.call_args_list))
        factory.check.assert_not_called()
        self.assertEqual(result['intent']['repositories'][0]['merge_policy'], 'human')

    def test_no_handover_until_real_smoke_verification(self):
        self.factory()
        result = self.install.apply()
        self.assertEqual(result['status'], 'installation_pending')
        self.assertFalse(result['handover_complete'])
        self.smoke.handover.assert_not_called()

    def test_consent_polling_does_not_reopen_installation_tab(self):
        self.factory()
        self.install.interactive = True
        consent = Mock()
        consent.observe.return_value = {'status': 'app_installation_required'}
        def construct(*args, **kwargs):
            def apply(observed):
                kwargs['open_browser']('https://github.example/install/1')
                return {'status': 'app_installation_required'}
            consent.apply.side_effect = apply
            return consent
        with patch('forge_client.install_consent.InstallConsent', side_effect=construct), \
             patch('webbrowser.open') as browser:
            self.install.apply()
            self.install.apply()
        browser.assert_called_once_with('https://github.example/install/1')

    def test_verified_smoke_handover_completes_and_resume_does_not_reenable(self):
        self.factory()
        self.smoke.run.return_value = {'status': 'smoke_verified'}
        self.smoke.handover.side_effect = lambda **kw: {'status': 'handover_complete' if kw.get('apply') else 'smoke_verified'}
        result = self.install.apply()
        self.assertEqual(result['status'], 'installation_complete')
        self.assertTrue(result['handover_complete'])
        self.smoke.handover.reset_mock()
        self.observe.reset_mock()
        self.install.apply()
        self.smoke.handover.assert_not_called()
        self.observe.assert_not_called()

    def test_remote_handover_survives_lost_local_completion(self):
        self.factory()
        self.smoke.run.return_value = {'status': 'handover_complete'}
        self.smoke.handover.return_value = {'status': 'handover_complete'}
        self.assertTrue(self.install.apply()['handover_complete'])
        self.assertTrue(all(not call.kwargs.get('apply') for call in self.smoke.handover.call_args_list))

    def test_publication_scratch_never_receives_a_pr_baseline(self):
        factory = self.factory()
        self.install.apply()
        self.assertNotIn('owner/probe', [call.args[0] for call in factory.prepare_repository.call_args_list])

    def test_active_smoke_resume_refreshes_expired_repository_observations(self):
        factory = Mock()
        stale = {'revision': 'r', 'readiness': {'access_ready': True, 'protection_ready': False}}
        fresh = {**stale, 'readiness': {'access_ready': True, 'protection_ready': True}}
        factory.call.side_effect = lambda action, **kw: {'phase': 'active'} if action == 'status' else fresh if action == 'verify_repository' else stale
        target = {'id': 10, 'name': 'owner/one'}
        self.assertEqual(self.install.repository(factory, target)['status'], 'repository_changed')
        self.assertEqual(self.install.repository(factory, target, apply=True)['status'], 'repository_ready')
        self.assertIn(('verify_repository',), [call.args for call in factory.call.call_args_list])

    def test_connected_codex_still_finishes_owned_login_cleanup(self):
        factory = Mock()
        factory.call.side_effect = lambda action, **kw: (
            {'status': 'connected' if kw['apply'] else 'account_login_saved'} if action == 'device_account'
            else {'harnesses': [{'id': 'codex', 'connected': True}]})
        provider = {'id': 'codex', 'kind': 'browser'}
        self.assertEqual(self.install.account(factory, provider)['status'], 'account_login_saved')
        self.assertEqual(self.install.account(factory, provider, apply=True)['status'], 'connected')

    def test_mutated_saved_intent_cannot_change_stage_authority(self):
        self.install.state['intent']['repositories'][0]['target']['id'] = 123
        with self.assertRaises(SetupError): self.install.apply()
        self.observe.assert_not_called()
