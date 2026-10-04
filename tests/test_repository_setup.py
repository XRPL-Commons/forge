import json
import unittest
from unittest.mock import Mock, patch

from forge.repository_setup import RepositorySetup, ruleset, RULESET_NAME, observation_key
from forge.factory.config import validate as runtime_config
from forge.deployments import Deployments
from forge.factory.github import GitHubUnavailable
from forge.state import StateError
from forge.settings import digest, write_json
from tests import test_settings as settings_tests
from tests import test_settings_http as http_tests
from tests.test_dashboard import dashboard
from tests import test_factory_cycle as cycle_tests


class RepositorySetupTests(unittest.TestCase):
    def setUp(self):
        settings_tests.SettingsTests.setUp(self)
        self.settings.refresh()
        config=self.settings.load();config['repositories']=[{'id':10,'name':'example/project','enabled':True}]
        write_json(self.settings.path,config)  # Existing enabled installation, before checks.
        self.github = Mock()
        self.github.return_value.target.return_value = 'release/v2'
        self.github.return_value.pages.return_value = []
        self.github.return_value.protection_report.return_value = {'repo_id': 10, 'branch': 'release/v2',
            'available': True, 'requirements': [{'id':'reviews','passed':True}, {'id':'stale','passed':True}],
            'bypass': {'status':'operator_review'}}
        self.service = RepositorySetup(self.settings, github_class=self.github)

    def test_check_is_read_only_and_download_uses_verified_branch(self):
        revision = digest(self.settings.load())
        self.service.check(10, revision)
        row = self.service.view(10)['repositories'][0]
        self.assertTrue(row['protection_ready'])
        self.assertTrue(self.settings.view()['repositories'][0]['dispatch_allowed'])
        payload, filename = self.service.download(10, revision)
        self.assertEqual(payload['conditions']['ref_name']['include'], ['refs/heads/release/v2'])
        controller = self.settings.load()['connections']['controller']['app_id']
        self.assertEqual(payload['bypass_actors'], [
            {'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'},
            {'actor_id': controller, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'},
        ])
        self.assertEqual(payload['rules'][0]['type'], 'pull_request')
        self.assertEqual(filename, 'forge-review-rules-10.json')
        self.assertEqual(row['protection']['bypass']['status'], 'operator_review')
        self.assertEqual(self.settings.load()['repositories'][0]['enabled'], True)
        self.github.return_value.call.assert_not_called()

    def test_refresh_retains_evidence_without_authorizing_from_previous_result(self):
        self.service.check(10)
        original = self.service.view(10)['repositories'][0]['protection']
        def during_check(item):
            row = self.service.view(10)['repositories'][0]
            self.assertEqual(row['protection_status'], 'checking')
            self.assertEqual(row['protection_previous_status'], 'ready')
            self.assertEqual(row['protection'], original)
            self.assertFalse(row['protection_ready'])
            with self.assertRaises(ValueError):
                self.service.download(10, digest(self.settings.load()))
            raise ValueError('Unavailable')
        self.github.return_value.target.side_effect = during_check
        self.service.check(10)
        row = self.service.view(10)['repositories'][0]
        self.assertEqual(row['protection_status'], 'unknown')
        self.assertEqual(row['protection_last_known'], original)
        self.assertFalse(row['protection_ready'])
        config = self.settings.load();config['connections']['build']['app_id'] = 99
        write_json(self.settings.path, config)
        self.assertIsNone(self.service.view(10)['repositories'][0]['protection_last_known'])

    def test_stale_wrong_identity_and_config_changes_cannot_download(self):
        revision=digest(self.settings.load())
        self.service.check(10, revision)
        for identifier, rev in [(99,revision),(10,'old')]:
            with self.assertRaises(ValueError): self.service.download(identifier, rev)
        self.now+=301
        self.assertEqual(self.service.view(10)['repositories'][0]['protection_status'],'stale')
        with self.assertRaises(ValueError): self.service.download(10, revision)
        self.now-=301
        config=self.settings.load();config['connections']['build']['app_id']=99
        write_json(self.settings.path,config)
        with self.assertRaises(ValueError): self.service.download(10,revision)

    def test_existing_ruleset_links_to_edit_and_missing_requirements_download(self):
        report=self.github.return_value.protection_report.return_value
        report['requirements'][1]['passed']=False
        self.github.return_value.pages.return_value=[{'id':21,'name':RULESET_NAME,'source_type':'Repository'}]
        self.service.check(10)
        row=self.service.view(10)['repositories'][0]
        self.assertEqual(row['protection_status'],'needs_setup')
        self.assertEqual(row['protection']['edit_url'],'https://github.com/example/project/settings/rules/21')
        self.service.download(10,digest(self.settings.load()))

    def test_unknown_observation_cannot_download_or_look_ready(self):
        self.github.return_value.target.side_effect=ValueError('Unavailable')
        self.service.check(10)
        row=self.service.view(10)['repositories'][0]
        self.assertEqual(row['protection_status'],'unknown')
        self.assertFalse(row['protection_ready'])
        with self.assertRaises(ValueError): self.service.download(10,digest(self.settings.load()))

    def test_repeated_checks_are_coalesced_and_interrupted_checks_expire(self):
        with self.settings.locked('repository-check'):
            self.assertFalse(self.service.check(10))
        config=self.settings.load()
        write_json(self.service.path,{'repositories':{'10':{'revision':observation_key(config,{'id':10,'name':'example/project'}),'name':'example/project','status':'checking','started_at':self.now-121}}})
        self.assertEqual(self.service.view(10)['repositories'][0]['protection_status'],'unknown')

    def test_batch_failure_does_not_hide_other_repository_or_branch_change(self):
        observed=self.settings.observation()
        for role in observed['roles'].values():
            role['repositories'].append({'id':11,'name':'example/second','archived':False})
        write_json(self.settings.state/'access.json',observed)
        config=self.settings.load()
        config['repositories'].append({'id':11,'name':'example/second','enabled':True})
        write_json(self.settings.path,config)  # Simulate a second pre-existing enabled repository.
        self.github.return_value.target.side_effect=[ValueError('Unavailable'),'release/v2']
        self.service.check()
        rows=self.service.view()['repositories']
        self.assertEqual([r['protection_status'] for r in rows],['unknown','ready'])
        self.github.return_value.target.side_effect=None
        self.github.return_value.target.return_value='next'
        self.github.return_value.protection_report.return_value['branch']='next'
        self.service.check(11)
        payload,_=self.service.download(11,digest(self.settings.load()))
        self.assertEqual(payload['conditions']['ref_name']['include'],['refs/heads/next'])

    def test_unrelated_repository_enablement_keeps_fresh_observation(self):
        self.service.check(10)
        config=self.settings.load()
        config['repositories'].append({'id':11,'name':'example/other','enabled':False})
        write_json(self.settings.path,config)
        self.assertTrue(self.service.view(10)['repositories'][0]['protection_ready'])

    def test_enable_requires_fresh_setup_and_preserves_config_on_failure(self):
        config=self.settings.load();config['repositories']=[];write_json(self.settings.path,config)
        revision=digest(config)
        with self.assertRaisesRegex(ValueError,'finish repository setup'):
            self.service.enable(10,revision)
        self.assertEqual(self.settings.load(),config)
        self.service.check(10)
        self.now+=301
        with self.assertRaisesRegex(ValueError,'finish repository setup'):
            self.service.enable(10,revision)
        self.assertEqual(self.settings.load(),config)
        self.now-=301
        with self.assertRaisesRegex(ValueError,'changed'):
            self.service.enable(10,'old')
        self.service.enable(10,revision)
        self.assertTrue(self.settings.view()['repositories'][0]['enabled'])

    def test_disable_preserves_other_settings_even_without_access_or_rules(self):
        config = self.settings.load()
        config['repositories'].append({'id': 11, 'name': 'example/other', 'enabled': False})
        write_json(self.settings.path, config)
        write_json(self.settings.state / 'access.json', {'roles': {}})
        result = self.service.disable(10, digest(config))
        config['repositories'][0]['enabled'] = False
        self.assertEqual(self.settings.load(), config)
        self.assertEqual(result['revision'], digest(config))
        self.assertFalse(result['repositories'][0]['enabled'])
        self.assertFalse(result['repositories'][0]['dispatch_allowed'])
        self.client.discover.reset_mock()
        self.service.disable(10, digest(config))  # Already disabled is harmless.
        self.assertEqual(self.settings.load(), config)
        self.client.discover.assert_not_called()

    def test_disable_records_a_discovered_repository_without_a_config_row(self):
        config = self.settings.load()
        config['repositories'] = []
        write_json(self.settings.path, config)
        self.service.disable(10, digest(config))
        config['repositories'] = [{'id': 10, 'name': 'example/project', 'enabled': False}]
        self.assertEqual(self.settings.load(), config)

    def test_disable_rejects_unknown_repository_and_stale_revision_without_writes(self):
        config = self.settings.load()
        for repo_id, revision in [(999, digest(config)), (10, 'old')]:
            with self.assertRaises(ValueError):
                self.service.disable(repo_id, revision)
            self.assertEqual(self.settings.load(), config)

    def test_disable_cannot_overwrite_a_concurrent_settings_change(self):
        config = self.settings.load()
        revision = digest(config)
        config['repositories'].append({'id': 11, 'name': 'example/other', 'enabled': False})
        save = self.settings.save
        def concurrent_save(candidate, expected):
            write_json(self.settings.path, config)
            return save(candidate, expected)
        with patch.object(self.settings, 'save', side_effect=concurrent_save):
            with self.assertRaisesRegex(ValueError, 'changed'):
                self.service.disable(10, revision)
        self.assertEqual(self.settings.load(), config)

    def test_disable_is_fenced_during_setup_and_factory_reset(self):
        config = self.settings.load()
        with patch.object(self.settings, 'root', self.settings.root / 'setup'):
            with self.assertRaisesRegex(ValueError, 'Finish initial Forge setup'):
                self.service.disable(10, digest(config))
        write_json(self.settings.root / 'state/factory-paused.json', {})
        with self.assertRaisesRegex(ValueError, 'reset is in progress'):
            self.service.disable(10, digest(config))
        self.assertEqual(self.settings.load(), config)

    def test_unknown_missing_rules_and_access_cannot_enable(self):
        config=self.settings.load();config['repositories']=[];write_json(self.settings.path,config)
        revision=digest(config)
        for available, passed in [(False,True),(True,False)]:
            self.github.return_value.protection_report.return_value.update(available=available, requirements=[{'id':'reviews','passed':passed}])
            self.service.check(10)
            with self.assertRaises(ValueError):self.service.enable(10,revision)
            self.assertEqual(self.settings.load(),config)
        self.github.return_value.protection_report.return_value.update(available=True,requirements=[{'id':'reviews','passed':True}])
        self.service.check(10)
        observed=self.settings.observation();observed['roles']['audit']['repositories']=[]
        write_json(self.settings.state/'access.json',observed)
        with self.assertRaisesRegex(ValueError,'verify access'):self.service.enable(10,revision)
        self.assertEqual(self.settings.load(),config)

    def test_live_direct_settings_write_cannot_skip_preparation_but_disable_is_allowed(self):
        config=self.settings.load();config['repositories'][0]['enabled']=False
        self.settings.save(config,digest(self.settings.load()))
        config['repositories'][0]['enabled']=True
        with self.assertRaisesRegex(ValueError,'finish repository setup'):
            self.settings.save(config,digest(self.settings.load()))
        self.assertFalse(self.settings.load()['repositories'][0]['enabled'])

    def test_view_exposes_app_actions_without_credentials(self):
        write_json(self.settings.root/'state/onboarding/setup.json',{'apps':{'audit':{
            'install_url':'https://github.com/apps/forge-audit/installations/new',
            'connection':{'app_id':3,'private_key':'never expose'}}}})
        view=self.service.view(10)
        self.assertEqual(view['connections']['audit']['install_url'],'https://github.com/apps/forge-audit/installations/new')
        self.assertEqual(view['connections']['build']['installation_url'],'https://github.com/settings/installations/101')
        self.assertNotIn('connection',view['connections']['audit'])
        self.assertTrue(view['activation_available'])
        config=self.settings.load();config['connections']['audit']['app_id']=90
        write_json(self.settings.path,config)
        self.assertIsNone(self.service.view(10)['connections']['audit']['install_url'])

    def proposal(self, **overrides):
        value = {'strategy': 'header', 'deploy': {'workflow': 'deploy.yaml', 'environment': 'production', 'trigger': 'push:main'},
                 'domain': {'value': 'academy.xrpl-commons.org', 'source': 'workflow', 'confidence': 'high'},
                 'exposure': {'kind': 'none', 'path': None, 'field': None, 'header': None},
                 'commit_source': 'image tag sha-<commit>',
                 'proposal': {'title': 'Expose the build commit', 'body': 'Add the X-Build-Commit header.'}}
        value.update(overrides)
        return value

    def store(self):
        from forge.deployments import Deployments
        return Deployments(self.settings.root, runner=None, github=None,
                           settings=self.settings, clock=self.settings.clock)

    def propose(self, repo_id=10, **overrides):
        store = self.store()
        store.save(repo_id, 'example/project', status='proposed', workflows_digest='digest-one',
                   head='a' * 40, summary='deploy.yaml pushes to production',
                   proposal=self.proposal(**overrides))
        return store

    def test_deployment_view_merges_the_store_with_the_confirmed_profile(self):
        row = self.settings.view()['repositories'][0]
        self.assertEqual(row['deployment'], {'status': 'idle', 'summary': None, 'proposal': None, 'profile': None,
                                             'observation': None, 'error': None, 'reason': None,
                                             'exposure_issue': None, 'reread_requested': False})
        self.propose()
        row = self.service.view(10)['repositories'][0]
        self.assertEqual(row['deployment']['status'], 'proposed')
        self.assertEqual(row['deployment']['summary'], 'deploy.yaml pushes to production')
        self.assertEqual(row['deployment']['proposal']['domain']['value'], 'academy.xrpl-commons.org')
        self.assertIsNone(row['deployment']['profile'])
        self.github.return_value.file_issue.return_value = 41
        self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.example.org')
        row = self.service.view(10)['repositories'][0]
        self.assertEqual(row['deployment']['status'], 'confirmed')
        self.assertEqual(row['deployment']['profile']['domain'], 'academy.example.org')
        self.assertEqual(row['deployment']['profile']['workflows_digest'], 'digest-one')
        self.assertEqual(row['deployment']['exposure_issue'], 41)

    def test_confirming_writes_the_profile_and_files_the_exposure_issue_once(self):
        self.propose()
        self.github.return_value.file_issue.return_value = 41
        view = self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.example.org')
        profile = self.settings.load()['repositories'][0]['deployment']
        self.assertEqual(profile['strategy'], 'header')
        self.assertEqual(profile['deploy']['workflow'], 'deploy.yaml')
        self.assertEqual(profile['domain'], 'academy.example.org')
        self.assertEqual(profile['workflows_digest'], 'digest-one')
        self.assertEqual(profile['exposure_issue'], 41)
        self.assertEqual(view['revision'], digest(self.settings.load()))
        item, title, body, labels = self.github.return_value.file_issue.call_args.args
        self.assertEqual(item, {'repo': 'example/project', 'repo_id': 10})
        self.assertEqual(title, 'Expose the build commit')
        self.assertIn('X-Build-Commit', body)
        self.assertEqual(labels, ['forge:deployment'])
        self.assertNotIn(runtime_config({})['ready_label'], labels)
        # A second read and confirmation must never file the issue again.
        self.propose()
        self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.xrpl-commons.org')
        self.github.return_value.file_issue.assert_called_once()
        profile = self.settings.load()['repositories'][0]['deployment']
        self.assertEqual(profile['exposure_issue'], 41)
        self.assertEqual(profile['domain'], 'academy.xrpl-commons.org')

    def test_an_observable_repository_confirms_without_filing_an_issue(self):
        self.propose(exposure={'kind': 'health', 'path': '/health', 'field': 'commit', 'header': None},
                     proposal=None, strategy='health')
        self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='')
        profile = self.settings.load()['repositories'][0]['deployment']
        self.assertEqual(profile['exposure'], {'kind': 'health', 'path': '/health', 'field': 'commit', 'header': None})
        self.assertIsNone(profile['exposure_issue'])
        self.github.return_value.file_issue.assert_not_called()

    def test_rejecting_and_re_reading_only_touch_the_deployment_store(self):
        self.propose()
        before = self.settings.load()
        self.service.deployment(10, digest(before), 'reject', reason='The domain is wrong')
        self.assertEqual(self.settings.load(), before)
        row = self.service.view(10)['repositories'][0]
        self.assertEqual(row['deployment']['status'], 'rejected')
        self.assertEqual(row['deployment']['reason'], 'The domain is wrong')
        self.assertFalse(row['deployment']['reread_requested'])
        self.service.deployment(10, digest(before), 'reread')
        self.assertEqual(self.settings.load(), before)
        self.assertTrue(self.service.view(10)['repositories'][0]['deployment']['reread_requested'])
        self.github.return_value.file_issue.assert_not_called()

    def test_a_stale_revision_or_an_absent_proposal_changes_nothing(self):
        for action, extra in (('confirm', {'domain': ''}), ('reject', {'reason': 'no'}), ('reread', {})):
            self.propose()
            with self.assertRaisesRegex(ValueError, 'Repository settings changed'):
                self.service.deployment(10, 'stale-revision', action, **extra)
            self.assertEqual(self.service.view(10)['repositories'][0]['deployment']['status'], 'proposed')
        self.assertNotIn('deployment', self.settings.load()['repositories'][0])
        with self.assertRaisesRegex(ValueError, 'bare host name'):
            self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='https://a.example.org/x')
        self.assertNotIn('deployment', self.settings.load()['repositories'][0])
        self.service.deployment(10, digest(self.settings.load()), 'reject', reason='The domain is wrong')
        with self.assertRaisesRegex(ValueError, 'no deployment proposal'):
            self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='a.example.org')
        self.assertNotIn('deployment', self.settings.load()['repositories'][0])

    def test_a_refused_settings_write_leaves_the_proposal_confirmable(self):
        self.propose()
        with patch.object(type(self.settings), 'save', side_effect=ValueError('Factory reset is in progress')):
            with self.assertRaisesRegex(ValueError, 'Factory reset is in progress'):
                self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.example.org')
        self.assertNotIn('deployment', self.settings.load()['repositories'][0])
        self.assertEqual(self.service.view(10)['repositories'][0]['deployment']['status'], 'proposed')
        self.github.return_value.file_issue.assert_not_called()
        self.github.return_value.file_issue.return_value = 7
        self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.example.org')
        self.assertEqual(self.settings.load()['repositories'][0]['deployment']['exposure_issue'], 7)

    def test_a_failed_rollback_does_not_replace_the_refusal_the_operator_must_read(self):
        self.propose()
        with patch.object(type(self.settings), 'save', side_effect=ValueError('Factory reset is in progress')), \
             patch.object(Deployments, 'save', side_effect=StateError('unreadable deployment state')):
            with self.assertRaisesRegex(ValueError, 'Factory reset is in progress'):
                self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.example.org')
        self.assertNotIn('deployment', self.settings.load()['repositories'][0])
        self.github.return_value.file_issue.assert_not_called()

    def test_an_unfiled_issue_is_reported_without_losing_the_profile(self):
        self.propose()
        self.github.return_value.file_issue.side_effect = GitHubUnavailable('GitHub observation is unconfirmed')
        self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.example.org')
        row = self.service.view(10)['repositories'][0]
        self.assertEqual(row['deployment']['status'], 'confirmed')
        self.assertIsNone(row['deployment']['exposure_issue'])
        self.assertEqual(row['deployment']['error'], 'GitHub observation is unconfirmed')

    def test_a_filed_issue_is_never_refiled_when_its_number_cannot_be_saved(self):
        self.propose()
        self.github.return_value.file_issue.return_value = 41
        original, saves = type(self.settings).save, []
        def save(settings, config, revision):
            saves.append(config)
            if len(saves) > 1:
                raise ValueError('Settings changed; reload before saving')
            return original(settings, config, revision)
        with patch.object(type(self.settings), 'save', save):
            self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.example.org')
        row = self.service.view(10)['repositories'][0]
        self.assertEqual(row['deployment']['status'], 'confirmed')
        self.assertIsNone(row['deployment']['profile']['exposure_issue'])
        self.assertEqual(row['deployment']['exposure_issue'], 41)
        # The next read and confirmation carries the filed number instead of filing again.
        self.propose()
        self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='')
        self.github.return_value.file_issue.assert_called_once()
        self.assertEqual(self.settings.load()['repositories'][0]['deployment']['exposure_issue'], 41)

    def test_a_malformed_deployment_store_does_not_take_down_the_settings_page(self):
        path = self.settings.root / 'state' / 'deployments.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        for text in ('{"repositories": []}', '[]', 'not json', '{"repositories": {"10": "broken"}}', '{}'):
            path.write_text(text)
            row = self.settings.view()['repositories'][0]
            self.assertEqual(row['deployment']['status'], 'idle')
            self.assertIsNone(row['deployment']['proposal'])
            self.assertFalse(row['deployment']['reread_requested'])

    def test_a_disabled_repository_cannot_confirm_a_deployment_profile(self):
        self.propose()
        config = self.settings.load(); config['repositories'][0]['enabled'] = False
        self.settings.save(config, digest(self.settings.load()))
        with self.assertRaisesRegex(ValueError, 'Enable this repository'):
            self.service.deployment(10, digest(self.settings.load()), 'confirm', domain='academy.example.org')
        self.assertNotIn('deployment', self.settings.load()['repositories'][0])
        self.assertEqual(self.service.view(10)['repositories'][0]['deployment']['status'], 'proposed')
        self.github.return_value.file_issue.assert_not_called()

    def test_reading_proposals_never_creates_deployment_state(self):
        from forge.settings import Settings
        root = self.settings.root / 'fresh'
        service = RepositorySetup(Settings(root, client=self.client, clock=self.settings.clock))
        self.assertEqual(service.proposals(), {})
        self.assertFalse((root / 'state').exists())

    def test_guided_setup_cannot_confirm_a_deployment_profile(self):
        from forge.settings import Settings
        setup = Settings(self.settings.root / 'setup', client=self.client, clock=self.settings.clock)
        write_json(setup.path, self.settings.load())
        self.propose()
        with self.assertRaisesRegex(ValueError, 'Finish initial Forge setup'):
            RepositorySetup(setup, github_class=self.github).deployment(
                10, digest(setup.load()), 'confirm', domain='academy.example.org')
        self.assertNotIn('deployment', setup.load()['repositories'][0])
        self.assertNotIn('deployment', self.settings.load()['repositories'][0])

    def test_missing_bypass_blocks_enablement_and_cached_ready_state(self):
        report = self.github.return_value.protection_report.return_value
        report['bypass'] = {'status': 'missing', 'detail': 'Add the controller bypass.'}
        self.service.check(10)
        row = self.service.view(10)['repositories'][0]
        self.assertEqual(row['protection_status'], 'needs_setup')
        self.assertFalse(row['protection_ready'])
        self.assertFalse(self.settings.view()['repositories'][0]['dispatch_allowed'])
        self.service.download(10, digest(self.settings.load()))
        # Evidence from before this release cannot keep a known blocker ready.
        data = self.service.read()
        data['repositories']['10']['status'] = 'ready'
        write_json(self.service.path, data)
        self.assertEqual(self.service.view(10)['repositories'][0]['protection_status'], 'needs_setup')
        self.service.disable(10, digest(self.settings.load()))
        with self.assertRaisesRegex(ValueError, 'finish repository setup'):
            self.service.enable(10, digest(self.settings.load()))
        report['bypass']['status'] = 'ready'
        self.service.check(10)
        self.service.enable(10, digest(self.settings.load()))
        self.assertTrue(self.settings.view()['repositories'][0]['dispatch_allowed'])

    def test_generated_rules_pass_the_real_reader(self):
        from tests.test_repository_rules import RepositoryRuleTests
        payload=ruleset('main', 4)
        reader=RepositoryRuleTests().reader(None,payload['rules'])
        self.assertTrue(reader.protections({'repo_id':10,'repo':'example/project'},{'base_ref':'main'}))

    def test_template_does_not_add_forge_checks_or_exclusive_permissions(self):
        payload=ruleset('main', 4)
        self.assertEqual([r['type'] for r in payload['rules']],['pull_request'])
        self.assertFalse(payload['rules'][0]['parameters']['require_code_owner_review'])
        self.assertEqual(payload['enforcement'],'active')
        with self.assertRaises(ValueError):ruleset('main\ninvalid', 4)


class RepositoryHTTPTests(unittest.TestCase):
    setUp=http_tests.SettingsHTTPTests.setUp
    handler=http_tests.SettingsHTTPTests.handler

    def test_repository_reads_require_trusted_host_and_check_requires_csrf(self):
        for path in ['/api/repositories','/api/repositories/ruleset?repo_id=10&revision=x','/settings/repository']:
            h=self.handler(path,Host='attacker.example');h.do_GET();h.send_error.assert_called_once_with(403)
        for path in ['/api/repositories/check','/api/repositories/enable','/api/repositories/disable','/api/repositories/deployment','/api/repositories/environment','/api/issues/environment']:
            h=self.handler(path,{'repo_id':10,'revision':'r'},Origin='http://attacker.example')
            h.do_POST();h.send_error.assert_called_once_with(403)

    def test_issue_environment_endpoint_validates_services_and_queues_a_decision(self):
        with patch('forge.factory.issue_actions.request', return_value='request-1') as request:
            value = {'key': '10:1', 'revision': 'r', 'environment': {'mode': 'full', 'services': ['mongodb']}}
            h = self.handler('/api/issues/environment', value); h.do_POST()
            self.assertTrue(json.loads(h._send.call_args.args[0])['ok'])
            self.assertEqual(request.call_args.kwargs['action'], 'environment')
            self.assertEqual(request.call_args.kwargs['environment'], value['environment'])
            for selected in ({'mode': 'full', 'services': ['docker']}, {'mode': 'full', 'services': []},
                             {'mode': 'automatic', 'services': ['mongodb']}, {'mode': 'standard'}, None):
                request.reset_mock()
                h = self.handler('/api/issues/environment', {**value, 'environment': selected}); h.do_POST()
                self.assertFalse(json.loads(h._send.call_args.args[0])['ok'])
                request.assert_not_called()

    def test_environment_actions_validate_envelope_and_return_repository_view(self):
        with patch('forge.repository_environments.RepositoryEnvironments') as environment, \
                patch('forge.repository_setup.RepositorySetup') as setup:
            setup.return_value.view.return_value = {'repositories': [{'id': 10}], 'revision': 'r'}
            for action in ('scan', 'confirm'):
                value = {'repo_id': 10, 'revision': 'r', 'environment_revision': 'env-r', 'action': action}
                environment.return_value.act.reset_mock()
                h = self.handler('/api/repositories/environment', value); h.do_POST()
                environment.return_value.act.assert_called_once_with(**value)
                self.assertTrue(json.loads(h._send.call_args.args[0])['ok'])
            for change in ({'action': 'launch'}, {'repo_id': True}, {'environment_revision': None}, {'extra': True}):
                environment.return_value.act.reset_mock()
                h = self.handler('/api/repositories/environment', {**value, **change}); h.do_POST()
                environment.return_value.act.assert_not_called()
                self.assertFalse(json.loads(h._send.call_args.args[0])['ok'])

    def test_download_is_attachment_and_does_not_start_a_check(self):
        with patch('forge.repository_setup.RepositorySetup') as cls:
            cls.return_value.download.return_value=(ruleset('main', 4),'forge-review-rules-10.json')
            h=self.handler('/api/repositories/ruleset?repo_id=10&revision=r');h.do_GET()
            cls.return_value.download.assert_called_once_with(10,'r')
            cls.return_value.start_check.assert_not_called()
            self.assertIn('attachment',h._send.call_args.args[2]['Content-Disposition'])

    def test_deployment_decisions_carry_one_action_and_the_current_revision(self):
        with patch('forge.repository_setup.RepositorySetup') as cls:
            cls.return_value.deployment.return_value={'revision':'new','repositories':[{'id':10}]}
            for value in ({'repo_id':10,'revision':'r','action':'confirm','domain':'academy.example.org'},
                          {'repo_id':10,'revision':'r','action':'reject','reason':'The domain is wrong'},
                          {'repo_id':10,'revision':'r','action':'reread'}):
                cls.return_value.deployment.reset_mock()
                h=self.handler('/api/repositories/deployment',value);h.do_POST()
                cls.return_value.deployment.assert_called_once_with(**value)
                body=json.loads(h._send.call_args.args[0])
                self.assertTrue(body['ok']);self.assertEqual(body['revision'],'new')
                self.assertEqual(body['controls_token'],dashboard.CONTROLS_TOKEN)
            for value in ({'repo_id':10,'revision':'r','action':'confirm'},
                          {'repo_id':10,'revision':'r','action':'reject'},
                          {'repo_id':10,'revision':'r','action':'reread','domain':'academy.example.org'},
                          {'repo_id':10,'revision':'r','action':'confirm','domain':'a','reason':'b'},
                          {'repo_id':None,'revision':'r','action':'reread'},
                          {'repo_id':True,'revision':'r','action':'reread'},
                          {'repo_id':10,'revision':'r','action':'enable'}):
                cls.return_value.deployment.reset_mock()
                h=self.handler('/api/repositories/deployment',value);h.do_POST()
                cls.return_value.deployment.assert_not_called()
                self.assertFalse(json.loads(h._send.call_args.args[0])['ok'])

    def test_disable_accepts_only_one_repository_and_current_revision(self):
        with patch('forge.repository_setup.RepositorySetup') as cls:
            cls.return_value.disable.return_value = {'revision': 'new', 'repositories': [{'id': 10, 'enabled': False}]}
            h = self.handler('/api/repositories/disable', {'repo_id': 10, 'revision': 'current'})
            h.do_POST()
            cls.return_value.disable.assert_called_once_with(10, 'current')
            result = json.loads(h._send.call_args.args[0])
            self.assertTrue(result['ok'])
            self.assertEqual(result['revision'], 'new')
            self.assertFalse(result['repositories'][0]['enabled'])
            self.assertEqual(result['controls_token'], dashboard.CONTROLS_TOKEN)
            cls.return_value.enable.assert_not_called()
            for change in ({'repo_id': None}, {'repo_id': True}, {'repo_id': 0}, {'repo_id': -1},
                           {'repo_id': '10'}, {'revision': None}, {'revision': ''}, {'config': {}}, {'enabled': True}):
                cls.return_value.disable.reset_mock()
                h = self.handler('/api/repositories/disable', {'repo_id': 10, 'revision': 'current', **change})
                h.do_POST()
                cls.return_value.disable.assert_not_called()
                self.assertFalse(json.loads(h._send.call_args.args[0])['ok'])

    def test_enable_accepts_only_one_repository_and_current_revision(self):
        with patch('forge.repository_setup.RepositorySetup') as cls:
            cls.return_value.enable.return_value={'repositories':[{'id':10,'enabled':True}]}
            h=self.handler('/api/repositories/enable',{'repo_id':10,'revision':'current'});h.do_POST()
            cls.return_value.enable.assert_called_once_with(10,'current')
            for value in ({'repo_id':None,'revision':'current'}, {'repo_id':True,'revision':'current'},
                          {'repo_id':10,'revision':'current','skip_checks':True}):
                cls.return_value.enable.reset_mock()
                h=self.handler('/api/repositories/enable',value);h.do_POST()
                cls.return_value.enable.assert_not_called()


class ProtectionRecoveryTests(unittest.TestCase):
    setUp=cycle_tests.CycleTest.setUp
    cycle=cycle_tests.CycleTest.cycle

    def test_only_prerequisite_hold_resumes_without_resetting_budget(self):
        self.github.protections=Mock(return_value=False)
        self.cycle()
        item=self.ledger.read()['issues'][self.key]
        self.assertEqual(item['state'],'hold')
        self.assertEqual(item['prerequisite_hold']['state'],'ready')
        self.assertEqual(self.ledger.failure_count(item),0)
        self.github.protections.return_value=True
        self.cycle()
        self.assertIsNotNone(self.ledger.read()['issues'][self.key]['active'])

    def test_manual_hold_cannot_be_resumed_by_rule_repair(self):
        self.ledger.pause_for_protection(self.key)
        self.ledger.hold(self.key,'Wait for operator')
        self.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['state'],'hold')
        self.assertFalse(self.ledger.read()['slots'])
