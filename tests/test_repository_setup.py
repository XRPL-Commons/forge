import unittest
from unittest.mock import Mock, patch

from forge.repository_setup import RepositorySetup, ruleset, RULESET_NAME, observation_key
from forge.settings import digest, write_json
from tests import test_settings as settings_tests
from tests import test_settings_http as http_tests
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
        self.assertEqual(payload['bypass_actors'], [])
        self.assertEqual(payload['rules'][0]['type'], 'pull_request')
        self.assertEqual(filename, 'forge-review-rules-10.json')
        self.assertEqual(row['protection']['bypass']['status'], 'operator_review')
        self.assertEqual(self.settings.load()['repositories'][0]['enabled'], True)
        self.github.return_value.call.assert_not_called()

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

    def test_generated_rules_pass_the_real_reader(self):
        from tests.test_repository_rules import RepositoryRuleTests
        payload=ruleset('main')
        reader=RepositoryRuleTests().reader(None,payload['rules'])
        self.assertTrue(reader.protections({'repo_id':10,'repo':'example/project'},{'base_ref':'main'}))

    def test_template_does_not_add_forge_checks_or_exclusive_permissions(self):
        payload=ruleset('main')
        self.assertEqual([r['type'] for r in payload['rules']],['pull_request'])
        self.assertFalse(payload['rules'][0]['parameters']['require_code_owner_review'])
        self.assertEqual(payload['enforcement'],'active')
        with self.assertRaises(ValueError):ruleset('main\ninvalid')


class RepositoryHTTPTests(unittest.TestCase):
    setUp=http_tests.SettingsHTTPTests.setUp
    handler=http_tests.SettingsHTTPTests.handler

    def test_repository_reads_require_trusted_host_and_check_requires_csrf(self):
        for path in ['/api/repositories','/api/repositories/ruleset?repo_id=10&revision=x','/settings/repository']:
            h=self.handler(path,Host='attacker.example');h.do_GET();h.send_error.assert_called_once_with(403)
        for path in ['/api/repositories/check','/api/repositories/enable']:
            h=self.handler(path,{'repo_id':10,'revision':'r'},Origin='http://attacker.example')
            h.do_POST();h.send_error.assert_called_once_with(403)

    def test_download_is_attachment_and_does_not_start_a_check(self):
        with patch('forge.repository_setup.RepositorySetup') as cls:
            cls.return_value.download.return_value=(ruleset('main'),'forge-review-rules-10.json')
            h=self.handler('/api/repositories/ruleset?repo_id=10&revision=r');h.do_GET()
            cls.return_value.download.assert_called_once_with(10,'r')
            cls.return_value.start_check.assert_not_called()
            self.assertIn('attachment',h._send.call_args.args[2]['Content-Disposition'])

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
