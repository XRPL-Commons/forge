from copy import deepcopy
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from forge_client.contracts import SetupError
from forge.installation_prepare import InstallationPrepare, DOMAIN
from forge.onboarding import Onboarding
from forge.settings import DEFAULT
from forge.store.configuration import Configuration
from tests.test_sqlite_identity import selected_store


class PreparationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.store = selected_store(self.root)
        self.now = 200
        self.setup = Onboarding(self.root, clock=lambda: self.now)
        self.generation = self.setup.read()['generation']; self.setup_id = 'a' * 32
        self.targets = [{'id': 1, 'name': 'owner/one'}, {'id': 2, 'name': 'owner/two'}]
        self.smoke = {'id': 3, 'name': 'owner/smoke'}; self.probe = {'id': 4, 'name': 'owner/probe'}
        self.plan = {'intent_digest': 'b' * 64, 'started_at': 100,
            'repositories': [{'target': target, 'merge_policy': 'human', 'allow_workflow_edits': False,
                              'github_protection': 'optional'} for target in self.targets],
            'smoke_repository': self.smoke, 'smoke_issue': None, 'publication_repository': self.probe,
            'environment': 'standard', 'providers': [{'id': 'codex', 'kind': 'browser'}]}
        with self.setup.store.transaction() as data:
            data['installation_consent'] = {'generation': self.generation, 'binding': {
                'setup_id': self.setup_id, 'repositories': self.targets + [self.smoke, self.probe], 'workflow_edits': False}}
        self.connected = True
        harness = patch.object(self.setup, 'harnesses', side_effect=lambda _: [{'id': 'codex', 'connected': self.connected}])
        harness.start(); self.addCleanup(harness.stop)
        self.service = InstallationPrepare(self.root, setup=self.setup, clock=lambda: self.now)
        self.configuration = Configuration(self.store)

    def approve(self, plan=None):
        return self.service.approve(plan or self.plan, setup_id=self.setup_id, generation=self.generation)

    def transport(self, *, barrier=None, failed_id=None, access_missing=None, during_report=None):
        settings = Mock()
        config = deepcopy(DEFAULT)
        config['repositories'] = [dict(target, enabled=False, github_protection='optional') for target in self.targets + [self.smoke]]
        settings.load.return_value = config
        settings.view.return_value = {'repositories': [dict(target, access_ready=target['id'] != access_missing) for target in self.targets + [self.smoke]]}
        settings.refresh.return_value = True
        policies = Mock()
        policies.protection_policy.return_value = {'mode': 'optional', 'source': 'explicit', 'revision': 'r'}
        main_thread = threading.get_ident()
        policies.save_observation.side_effect = lambda *args, **kwargs: self.assertEqual(threading.get_ident(), main_thread)
        readers = []
        def client(*args, **kwargs):
            github = Mock(); readers.append(github)
            github.target.return_value = 'main'
            def report(item, context):
                self.assertNotEqual(threading.get_ident(), main_thread)
                if barrier: barrier.wait(timeout=3)
                if during_report: during_report(item)
                return {'repo_id': item['repo_id'], 'branch': 'main', 'available': True,
                        'requirements': [{'passed': item['repo_id'] != failed_id}], 'merge_check_scope': 'all'}
            github.protection_report.side_effect = report
            return github
        patches = [patch.object(self.setup, 'settings', return_value=settings),
                   patch('forge.installation_prepare.RepositorySetup', return_value=policies),
                   patch('forge.installation_prepare.GitHub', side_effect=client)]
        for item in patches: item.start(); self.addCleanup(item.stop)
        return settings, policies, readers

    def test_plan_replay_is_immutable_and_rejects_unconsented_targets(self):
        self.approve(); self.approve()
        self.assertEqual(self.service.saved()['revision'], 1)
        changed = deepcopy(self.plan); changed['repositories'][0]['merge_policy'] = 'auto'
        with self.assertRaises(SetupError): self.approve(changed)
        with self.assertRaises(SetupError): self.approve({**self.plan, 'personal_token': 'never'})
        self.assertNotIn('personal_token', self.service.view())

    def test_approval_requires_same_generation_and_saved_repository_consent(self):
        with self.assertRaises(SetupError):
            self.service.approve(self.plan, setup_id=self.setup_id, generation='old')
        changed = deepcopy(self.plan); changed['smoke_repository'] = {'id': 9, 'name': 'owner/private'}
        with self.assertRaises(SetupError): self.approve(changed)
        self.assertIsNone(self.service.saved())

    def test_workflow_access_cannot_exceed_saved_consent(self):
        changed = deepcopy(self.plan); changed['repositories'][0]['allow_workflow_edits'] = True
        with self.assertRaises(SetupError): self.approve(changed)
        self.assertIsNone(self.service.saved())

    def test_environment_starts_while_account_waits_and_checks_run_in_parallel(self):
        self.connected = False; self.plan['environment'] = 'full'; self.approve()
        settings, policies, readers = self.transport(barrier=threading.Barrier(3))
        with patch('forge.installation_prepare.Environment') as environment:
            environment.return_value.view.return_value = {'status': 'environment_required'}
            environment.return_value.apply.return_value = {'status': 'environment_installing'}
            view = self.service.tick()
        environment.return_value.apply.assert_called_once_with()
        self.assertFalse(view['ready']); self.assertFalse(view['providers'][0]['connected'])
        self.assertEqual(settings.refresh.call_count, 1)
        self.assertEqual(len(readers), 3); self.assertEqual(policies.save_observation.call_count, 3)
        for call in policies.save_observation.call_args_list:
            self.assertEqual(call.kwargs['expected_generation'], self.generation)
            self.assertIn('expected_config', call.kwargs)
        stages = {row['id']: row for row in view['stages']}
        self.assertEqual(stages['environment']['status'], 'waiting')
        self.assertEqual(stages['account.codex']['status'], 'waiting')
        self.assertTrue(all(stages['repository.' + str(i)]['status'] == 'complete' for i in (1, 2, 3)))

    def test_missing_rules_require_operator_and_do_not_block_other_reads(self):
        self.approve(); settings, policies, readers = self.transport(failed_id=2)
        view = self.service.tick()
        self.assertEqual(view['status'], 'preparation_needs_operator')
        self.assertEqual([item['repository']['id'] for item in view['needs_operator']], [2])
        self.assertEqual(len(readers), 3)
        policies.set_github_protection.assert_not_called()
        # Completed reports do not repeat on idle polling; failed rules need an
        # explicit retry after the operator fixes GitHub administration.
        self.service.tick(); self.assertEqual(len(readers), 3)
        self.assertEqual(settings.refresh.call_count, 1)
        self.service.retry(generation=self.generation)
        self.service.tick()
        self.assertEqual(len(readers), 4)
        self.assertEqual(settings.refresh.call_count, 2)

    def test_missing_app_access_is_not_silently_granted(self):
        self.approve(); _, _, readers = self.transport(access_missing=1)
        view = self.service.tick()
        self.assertEqual([item['repository']['id'] for item in view['needs_operator']], [1])
        self.assertEqual(len(readers), 2)

    def test_parallel_batch_is_bounded(self):
        self.targets.append({'id': 5, 'name': 'owner/three'})
        self.plan['repositories'].append({'target': self.targets[-1], 'merge_policy': 'human',
            'allow_workflow_edits': False, 'github_protection': 'optional'})
        with self.setup.store.transaction() as data:
            data['installation_consent']['binding']['repositories'].append(self.targets[-1])
        self.approve(); _, _, readers = self.transport(barrier=threading.Barrier(3))
        view = self.service.tick()
        self.assertEqual(len(readers), 3)
        self.assertEqual(next(row for row in view['stages'] if row['id'] == 'repository.3')['status'], 'pending')

    def test_durable_resume_qualifies_once_per_target_then_binds_exact_smoke(self):
        self.approve(); self.transport(); self.service.tick()
        with patch('forge.installation_prepare.qualify', return_value={'status': 'qualified'}) as publication:
            for _ in range(3):
                self.service = InstallationPrepare(self.root, setup=self.setup, clock=lambda: self.now)
                self.service.tick()
            self.assertEqual(publication.call_count, 3)
            self.assertEqual({call.kwargs['repository']['id'] for call in publication.call_args_list}, {1, 2, 3})
            self.assertTrue(all(call.kwargs['probe_repository'] == self.probe for call in publication.call_args_list))
            calls = []
            def smoke_call(action, params, **scope):
                calls.append((action, params, scope))
                if action == 'issue': return {'status': 'smoke_issue_ready', 'number': 8}
                self.configuration.replace('installation_smoke', {'phase': 'pending', 'binding': {**params['binding'], **scope}},
                                           subject=self.generation, expected_revision=0)
                return {'status': 'smoke_pending'}
            with patch('forge.installation_prepare.InstallationSmoke') as smoke:
                smoke.return_value.call.side_effect = smoke_call
                view = self.service.tick()
        self.assertTrue(view['ready'])
        self.assertEqual(view['smoke'], {'repository': self.smoke, 'number': 8, 'smoke_policy': 'auto'})
        self.assertEqual(calls[-1][1]['binding']['repositories'], self.plan['repositories'])
        self.assertEqual(view['intent_digest'], self.plan['intent_digest'])
        self.assertEqual(calls[-1][2], {'setup_id': self.setup_id, 'generation': self.generation})

    def test_missing_coding_account_never_creates_smoke_issue(self):
        self.connected = False; self.approve(); self.transport(); self.service.tick()
        with patch('forge.installation_prepare.qualify', return_value={'status': 'qualified'}), \
                patch('forge.installation_prepare.InstallationSmoke') as smoke:
            for _ in range(4): view = self.service.tick()
            smoke.assert_not_called()
        self.assertFalse(view['ready'])

    def test_reset_cannot_resume_an_old_preparation(self):
        self.approve()
        with self.setup.store.transaction() as data: data['generation'] = 'replacement'
        with patch('forge.installation_prepare.Environment') as environment:
            self.assertFalse(self.service.tick()['available'])
            environment.assert_not_called()
        with self.assertRaises(SetupError): self.service.retry(generation=self.generation)

    def test_pause_stops_preparation(self):
        from forge.onboarding import control_in
        self.approve()
        with self.store.write() as db: control_in(db, 'factory_paused', True, self.now)
        with patch('forge.installation_prepare.Environment') as environment:
            self.service.tick(); environment.assert_not_called()
        self.assertTrue(all(row['status'] == 'pending' for row in self.service.saved()['settings']['stages'].values()))

    def test_remote_reads_release_identity_lock_for_account_consent(self):
        from forge.sqlite_identity import identity_effect_lock
        self.approve()
        entered = []
        def during_report(item):
            with identity_effect_lock(self.store):
                entered.append(item['repo_id'])
        _, policies, _ = self.transport(during_report=during_report)
        self.service.tick()
        self.assertEqual(sorted(entered), [1, 2, 3])
        self.assertEqual(policies.save_observation.call_count, 3)

    def test_reset_during_reads_discards_reports_before_any_write(self):
        from forge.sqlite_identity import identity_effect_lock
        self.approve()
        def during_report(item):
            if item['repo_id'] == 1:
                with identity_effect_lock(self.store):
                    with self.setup.store.transaction() as data:
                        data['generation'] = 'replacement'
        _, policies, _ = self.transport(during_report=during_report)
        with self.assertRaises(SetupError): self.service.tick()
        policies.save_observation.assert_not_called()
        self.assertFalse(self.service.view()['available'])

    def test_retry_during_reads_invalidates_old_batch(self):
        self.approve()
        def during_report(item):
            if item['repo_id'] == 1: self.service.retry(generation=self.generation)
        _, policies, _ = self.transport(during_report=during_report)
        with self.assertRaises(SetupError): self.service.tick()
        policies.save_observation.assert_not_called()
        self.assertEqual(next(row for row in self.service.view()['stages'] if row['id'] == 'access')['status'], 'pending')

    def test_preparation_requires_no_repository_automation_already_enabled(self):
        with patch.object(self.setup, 'settings') as settings:
            settings.return_value.load.return_value = {'repositories': [{'id': 1, 'enabled': True}]}
            with self.assertRaises(SetupError): self.approve()
        self.assertIsNone(self.service.saved())
