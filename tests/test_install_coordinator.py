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
        version = patch('forge_client.version.report', return_value={
            'client_revision': REVISION, 'server_revision': REVISION})
        version.start(); self.addCleanup(version.stop)
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

    def test_stage_announces_long_running_work_before_applying_and_when_waiting(self):
        events = []
        self.install.notify = events.append
        def apply(observed):
            self.assertEqual(events[-1]['status'], 'applying')
            self.assertEqual(events[-1]['instruction'], 'Setting up: host installation.')
            return {'status': 'installing'}
        self.install.stage('host', lambda: {'status': 'planned'}, apply,
                           complete=lambda row: row['status'] == 'installed')
        self.assertEqual([row['status'] for row in events], ['checking', 'applying', 'pending'])
        self.install.stage('host', lambda: {'status': 'installed'}, apply,
                           complete=lambda row: row['status'] == 'installed')
        self.assertEqual(events[-1]['instruction'], 'Ready: host installation.')

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

    def test_active_resume_applies_explicit_protection_before_verification(self):
        self.install.state['intent']['repositories'][0]['github_protection'] = 'optional'
        self.install.state['inputs_digest'] = digest(self.install.state['intent'])
        factory = Mock()
        current = {'mode': 'required', 'revision': 'a'*64}
        calls = []
        def call(action, **parameters):
            calls.append((action, parameters))
            if action == 'status': return {'phase': 'active'}
            if action == 'github_protection':
                if parameters['operation'] == 'set':
                    self.assertEqual(parameters['revision'], current['revision'])
                    current.update(mode=parameters['mode'], revision='b'*64)
                return deepcopy(current)
            if action == 'verify_repository':
                self.assertEqual(current['mode'], 'optional')
                self.assertEqual(parameters['revision'], 'b'*64)
            return {'revision': current['revision'], 'baseline': {'github_protection': current['mode']},
                    'readiness': {'access_ready': True, 'protection_ready': True}}
        factory.call.side_effect = call
        target = self.install.state['intent']['repositories'][0]['target']
        self.assertEqual(self.install.repository(factory, target)['status'], 'repository_changed')
        self.assertEqual([action for action, _ in calls], ['status', 'requirements'])
        self.assertEqual(self.install.repository(factory, target, apply=True)['status'], 'repository_ready')
        writes = [parameters for action, parameters in calls
                  if action == 'github_protection' and parameters['operation'] == 'set']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['mode'], 'optional')
        self.assertEqual(self.install.repository(factory, target, apply=True)['status'], 'repository_ready')
        self.assertEqual(sum(action == 'github_protection' and parameters['operation'] == 'set'
                             for action, parameters in calls), 1)

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
        with patch('forge_client.install_targets.prepare_targets') as targets:
            self.assertEqual(self.install.apply()['status'], 'preflight_blocked')
        targets.assert_not_called()
        self.host.execute.assert_not_called()
        with self.assertRaises(SetupError): self.install.apply(waivers=['host.identity'])
        self.host.execute.assert_not_called()

    def test_targets_precede_host_and_account_consent_precedes_repository_work(self):
        self.factory()
        with patch.object(self.install, 'stage', wraps=self.install.stage) as stages:
            self.install.apply()
        names = [call.args[0] for call in stages.call_args_list]
        self.assertLess(names.index('targets'), names.index('host'))
        self.assertLess(names.index('identity'), names.index('account.codex'))
        self.assertLess(names.index('account.codex'), names.index('repository.10'))
        self.assertLess(names.index('account.codex'), names.index('environment'))
        self.assertLess(names.index('account.codex'), names.index('publication.10'))

    def test_uncertain_target_creation_stops_before_host_installation(self):
        self.factory()
        uncertain = {'status': 'write_unconfirmed'}
        with patch('forge_client.install_targets.prepare_targets', return_value=uncertain) as targets:
            result = self.install.apply()
        targets.assert_called_once()
        self.host.execute.assert_not_called()
        self.assertNotIn('host', result['stages'])
        self.assertEqual(result['pending'][0]['status'], 'write_unconfirmed')

    def test_pending_login_pauses_later_work_and_resumes_existing_journal(self):
        factory = self.factory()
        # This is a journal made by the old ordering: repositories are already
        # prepared when the account needs attention on a later invocation.
        self.install.apply()
        original = deepcopy(self.install.state['stages']['repository.10'])
        original_targets = deepcopy(self.install.state['stages']['targets'])
        original_start = self.install.state['started_at']
        usual = factory.call.side_effect
        connected = False
        def call(action, **options):
            if action == 'device_account' and not connected:
                return {'status': 'account_login_pending', 'session_id': 'existing-login'}
            return usual(action, **options)
        factory.call.side_effect = call
        factory.prepare_repository.reset_mock()
        resumed = Install('anvil', github=self.github, installation=self.host, journal=self.journal,
                          preflight=self.observe)
        with patch('forge_client.install_environment.Environment') as environment, \
             patch('forge_client.install_publication.qualify') as publication:
            waiting = resumed.apply()
        factory.prepare_repository.assert_not_called()
        environment.assert_not_called(); publication.assert_not_called()
        self.assertEqual(waiting['stages']['account.codex']['receipt']['session_id'], 'existing-login')
        self.assertEqual(waiting['stages']['repository.10'], original)
        self.assertEqual(waiting['stages']['targets'], original_targets)
        connected = True
        result = resumed.apply()
        self.assertEqual(result['stages']['account.codex']['status'], 'complete')
        self.assertEqual(result['stages']['repository.10']['completed_at'], original['completed_at'])
        self.assertEqual(result['stages']['repository.10']['inputs_digest'], original['inputs_digest'])
        self.assertEqual(result['started_at'], original_start)
        self.assertTrue(all(not item.kwargs.get('apply') for item in factory.prepare_repository.call_args_list))
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

    def test_failed_smoke_receipt_exposes_exact_retry_command(self):
        self.factory()
        self.smoke.run.return_value = {'status': 'smoke_triage_failed', 'proposal_id': 'failed-proposal',
                                       'error': 'steward_cycle_limit'}
        result = self.install.apply(retry_smoke='previous-proposal')
        self.smoke.run.assert_called_with(apply=True, retry_proposal='previous-proposal')
        failure = next(row for row in result['pending'] if row['stage'] == 'smoke')
        self.assertEqual(failure['next_action']['command'], ['forge', 'install', '--ssh', 'anvil', '--resume',
            '--retry-smoke', 'failed-proposal', '--wait', '3600'])

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
