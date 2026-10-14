"""CLI setup uses the real selected Onboarding lifecycle and normal checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.installation_apps import RoleAppHandoff
from forge.installation_setup import InstallationSetup
from forge_client.contracts import SetupError
from forge_client.factory_setup import FactorySetup, execute
from forge_client.install import Installation
from forge_client.metrics import first_merge_timing
from forge_client.workflow import Journal
from tests import test_sqlite_onboarding as sql_setup


class SetupStageTests(unittest.TestCase):
    def setUp(self):
        sql_setup.SqliteSetupTests.setUp(self)
        sql_setup.SqliteSetupTests.connected(self)
        config = self.setup.runtime(); config['image'] = 'sha256:'+'a'*64
        self.setup.save_runtime(config, active=False)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.local = Journal('test', 'anvil', root=Path(temp.name).resolve())
        self.local.save({'version': 1, 'ssh': 'anvil', 'intent': {'id':'a'*32, 'name':'Anvil','revision':'b'*40},
                         'started_at':100, 'origin':'https://forge.example', 'milestones': {}})
        self.github = Mock(); self.github.identity.return_value = {'id':123,'login':'operator'}
        auth = Mock(); auth.config.return_value = {'origin':'https://forge.example','operators':[123]}
        handoff = RoleAppHandoff(self.root, setup=self.setup, auth=auth)
        self.server = InstallationSetup(self.root, setup=self.setup, handoff=handoff)
        self.host = Mock()
        self.host.rpc.side_effect = lambda program, value, **kw: {'ok':True,'result':self.server.call(value['setup'])}
        self.workflow = Installation('anvil', host=self.host, storage=self.local, clock=lambda:1000)
        self.factory = FactorySetup(self.workflow, github=self.github)

    def test_check_and_explicit_start_use_real_setup_probe_lifecycle(self):
        self.assertFalse(self.factory.call('status')['can_start'])
        with self.assertRaises(SetupError): self.factory.check(start=True, apply=True)
        checking = self.factory.check(apply=True)
        self.assertEqual(checking['status'], 'checking')
        service, engine, resets = sql_setup.SqliteSetupTests.service(self)
        for _ in range(30):
            if not self.setup.read()['operation']: break
            service.tick()
        self.assertTrue(self.factory.call('status')['can_start'])
        self.assertEqual(self.setup.read()['phase'], 'setup')
        self.factory.check(start=True, apply=True)
        service.tick()
        self.assertEqual(self.factory.call('status')['phase'], 'active')
        self.assertEqual(self.local.read()['milestones']['factory_started'], 1000)
        self.assertFalse(engine)
        with self.assertRaises(SetupError): self.factory.apps(apply=True)

    def test_initial_repository_uses_real_preparation_and_readiness_without_start(self):
        from forge.repository_setup import RepositorySetup
        self.installations[:] = [{'id':100+i,'app_id':i,'account':{'login':'example'}} for i in range(1,5)]
        local = self.github
        local.repository.return_value = {'id':10,'name':'example/project','branch':'main','admin':True}
        local.rulesets.return_value = []
        remote = Mock()
        remote.return_value.target.return_value = 'main'
        remote.return_value.pages.return_value = []
        remote.return_value.protection_report.return_value = {'branch':'main','available':True,
            'requirements':[{'id':'review','label':'Required review','passed':True}], 'bypass':{'status':'ready'}}
        with patch('forge.installation_setup.RepositorySetup',
                   side_effect=lambda settings: RepositorySetup(settings, github_class=remote)):
            result = self.factory.prepare_repository('example/project', apply=True)
        self.assertEqual(result['status'],'repository_ready',result)
        self.assertEqual(self.setup.read()['phase'],'setup')
        self.assertEqual(self.local.read()['first_repository'],{'id':10,'name':'example/project'})
        local.create_baseline.assert_not_called()
        local.grant_repository.assert_not_called()

    def test_secret_is_not_saved_in_client_journal_and_json_browser_flow_is_handoff(self):
        secret = 'synthetic-provider-credential-123456789'
        self.factory.account('codex', kind='api_key', secret=secret, apply=True)
        self.assertNotIn(secret, self.local.path.read_text())
        self.host.rpc.reset_mock()
        result = self.factory.account('codex', apply=True, interactive=False)
        self.assertEqual(result['status'], 'account_login_required')
        self.assertEqual([call.args[1]['setup']['action'] for call in self.host.rpc.call_args_list], ['status'])

    def test_changed_setup_generation_is_not_silently_adopted_on_resume(self):
        self.factory.call('status')
        with self.setup.store.transaction() as data: data['generation'] = 'new-generation'
        resumed = FactorySetup(self.workflow, github=self.github)
        with self.assertRaises(SetupError): resumed.call('status')
        self.assertNotEqual(self.local.read()['factory_binding']['generation'], 'new-generation')

    def test_setup_requests_reject_unknown_actions_and_extra_secret_fields(self):
        request = {'version':1,'setup_id':'a'*32,'actor_id':123,'generation':self.setup.read()['generation'],
                   'action':'status','parameters':{}}
        for bad in ({**request,'action':'shell'}, {**request,'parameters':{'github_token':'not-allowed'}}):
            with self.assertRaises(SetupError): self.server.call(bad)


class PilotTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.journal = Journal('pilot','anvil',root=Path(temp.name).resolve())
        self.target = {'id':10,'name':'example/project'}
        self.journal.save({'version':1,'ssh':'anvil','intent':{'id':'a'*32,'name':'Anvil','revision':'b'*40},
            'started_at':100,'origin':'https://forge.example','milestones':{}, 'factory_actor':{'id':7,'login':'operator'},
            'first_repository':self.target})
        self.workflow = Installation('anvil',host=Mock(),storage=self.journal,clock=lambda:1000)
        self.github = Mock()
        self.factory = FactorySetup(self.workflow,github=self.github)
        self.proof = {'status':'waiting','repository':self.target,'number':4,'candidate':None,'evidence':None,
                      'attempts':{'total':4,'charged_failures':1}}
        def call(action, **kw):
            if action == 'status': return {'phase':'active','ready_label':'forge:ready'}
            self.assertEqual(kw['started_at'],100)
            return deepcopy(self.proof)
        self.factory.call = call

    def test_retry_preserves_start_and_issue_until_independent_exact_merge(self):
        self.assertEqual(self.factory.pilot(number=4,apply=True)['status'],'first_merge_pending')
        self.assertEqual(self.journal.read()['started_at'],100)
        self.assertNotIn('first_merge',self.journal.read())
        with self.assertRaises(SetupError): self.factory.pilot(number=5)
        self.proof.update(status='merged',candidate={'id':'candidate','pull_number':9,'head_sha':'a'*40,'base_sha':'b'*40,'merge_sha':'c'*40},
                          evidence={'merge_effect_id':'effect','confirmed_at':900,'role_jobs':{'build':'b','review':'r','audit':'a'}})
        self.github.merged_pull.return_value = {'merged':True,'head_sha':'x'*40,'merge_sha':'c'*40,'merged_at':'1970-01-01T00:15:00Z'}
        with self.assertRaises(SetupError): self.factory.pilot()
        self.assertNotIn('first_merge',self.journal.read())
        self.github.merged_pull.return_value['head_sha'] = 'a'*40
        result = self.factory.pilot()
        self.assertEqual(result['status'],'first_merge_complete')
        self.assertEqual(result['timing']['setup_to_first_merge_seconds'],800)
        self.assertEqual(result['attempts']['charged_failures'],1)
        self.assertEqual(self.journal.read()['milestones']['first_merge'],900)

    def test_invalid_timer_order_is_reported_instead_of_zero_duration(self):
        with self.assertRaises(SetupError) as error:
            first_merge_timing({'started_at':100},90)
        self.assertEqual(error.exception.code,'timing_unavailable')


class WizardResumeTests(unittest.TestCase):
    def app_wizard(self, views, answers, handoffs):
        from forge_client.factory_setup import _wizard
        factory = Mock()
        factory.state = {'factory_owner':{'login':'example'},'factory_actor':{'login':'operator'}}
        factory.call.side_effect = views
        factory.apps.side_effect = handoffs
        ask, tell, browser = Mock(side_effect=answers), Mock(), Mock()
        with patch('forge_client.factory_setup.FactorySetup',return_value=factory):
            result = _wizard(Mock(ssh='anvil'),ask=ask,tell=tell,open_browser=browser)
        return factory, result, tell, browser

    def app_view(self, **roles):
        return {'phase':'setup','roles':{
            role: roles.get(role, {'registered':True,'status':'ok'})
            for role in ('build','review','audit','controller')}, 'harnesses':[]}

    def test_incomplete_role_stays_selected_after_check_and_unknown_choice(self):
        view = self.app_view(build={'registered':False,'status':'missing'},
                             review={'registered':False,'status':'missing'})
        factory, result, tell, browser = self.app_wizard(
            [view, view, view], ['check','yes','save'], [{'url':'https://forge.example/build'}])
        factory.apps.assert_called_once_with(role='build',owner='example',apply=True)
        self.assertEqual([call.args[0] for call in factory.call.call_args_list], ['status','refresh','refresh'])
        factory.account.assert_not_called()
        factory.prepare_repository.assert_not_called()
        self.assertEqual(result,view)
        self.assertIn('forge setup continue --ssh anvil', ' '.join(call.args[0] for call in tell.call_args_list))

    def test_passkey_or_expired_handoff_reopens_then_verifies_before_next_role(self):
        view = self.app_view(build={'registered':False,'status':'missing'},
                             review={'registered':False,'status':'missing'})
        refreshed = self.app_view(review={'registered':False,'status':'missing'})
        factory, result, tell, browser = self.app_wizard(
            [view, view, view, refreshed], ['check','reopen','check','save'],
            [{'url':'https://forge.example/old'}, {'url':'https://forge.example/renewed'},
             {'url':'https://forge.example/review'}])
        self.assertEqual([call.kwargs['role'] for call in factory.apps.call_args_list], ['build','build','review'])
        self.assertEqual([call.args[0] for call in browser.call_args_list],
                         ['https://forge.example/old','https://forge.example/renewed','https://forge.example/review'])
        self.assertEqual(result,refreshed)
        self.assertIn('Do not fill in the blank form manually.', ' '.join(call.args[0] for call in tell.call_args_list))
        factory.account.assert_not_called()

    def test_registered_but_uninstalled_resume_reopens_existing_installation(self):
        view = self.app_view(build={'registered':True,'status':'missing'})
        link = 'https://github.com/apps/existing-build/installations/new'
        factory, result, tell, browser = self.app_wizard(
            [view, view, view], ['check','reopen','save'], [{'install_url':link}, {'install_url':link}])
        self.assertEqual([call.args[0] for call in browser.call_args_list], [link,link])
        self.assertEqual([call.kwargs['role'] for call in factory.apps.call_args_list], ['build','build'])
        output = ' '.join(call.args[0] for call in tell.call_args_list)
        self.assertIn('Install this existing App', output)
        self.assertNotIn('approve App registration', output)
        factory.prepare_repository.assert_not_called()
        self.assertEqual(result,view)

    def test_ready_apps_are_not_registered_again_and_operator_start_is_explicit(self):
        from forge_client.factory_setup import _wizard
        factory = Mock()
        factory.state = {'factory_owner':{'login':'example'},'factory_actor':{'login':'operator'},
                         'first_repository':{'name':'example/project'}}
        view = {'phase':'setup','roles':{r:{'registered':True,'status':'ok'} for r in ('build','review','audit','controller')},
                'harnesses':[{'connected':True}], 'can_start':True}
        factory.call.return_value = view
        factory.prepare_repository.side_effect = [{'status':'planned'},{'status':'repository_ready'}]
        factory.check.return_value = view
        with patch('forge_client.factory_setup.FactorySetup',return_value=factory):
            result = _wizard(Mock(),ask=Mock(side_effect=['y','y','n']),tell=Mock(),open_browser=Mock())
        factory.apps.assert_not_called()
        factory.account.assert_not_called()
        factory.check.assert_called_once_with(apply=True,wait=600)
        factory.pilot.assert_not_called()
        self.assertEqual(result['phase'],'setup')

    def test_json_continue_is_inspection_and_does_not_open_browser(self):
        from forge_client.cli import execute as execute_cli, parser
        args = parser().parse_args(['setup','continue','--ssh','anvil','--json'])
        with patch('forge_client.factory_setup.execute',return_value={'status':'setup_required'}) as execute_stage, \
             patch('forge_client.factory_setup.wizard',side_effect=AssertionError('interactive wizard')):
            result = execute_cli(args)
        self.assertEqual(result['status'],'setup_required')
        self.assertEqual(execute_stage.call_args.args[0].setup_command,'inspect')


class SetupHostTransportTests(unittest.TestCase):
    def test_root_transport_runs_qualified_service_account_and_keeps_secret_on_stdin(self):
        import os
        from forge_client import _factory_host as host
        secret = 'synthetic-provider-secret-for-private-stdin'
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            (home / 'source/forge').mkdir(parents=True)
            for name in ('launch.py', 'installation_setup.py'):
                (home / 'source/forge' / name).touch()
            request = {'version':1,'intent':{'id':'a'*32},'setup':{'setup_id':'a'*32,'action':'account',
                       'parameters':{'provider':'codex','kind':'api_key','secret':secret}}}
            account = SimpleNamespace(pw_uid=os.getuid(),pw_dir=str(home))
            with patch.object(host,'HOME',home), patch.object(host,'ROOT',home/'runtime'), \
                 patch.object(host,'SOURCE',home/'source'), patch.object(host,'validate_intent'), \
                 patch.object(host,'facts'), patch.object(host,'read',return_value={'intent':request['intent'],'step':'complete'}), \
                 patch.object(host.pwd,'getpwnam',return_value=account), \
                 patch.object(host.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=b'{"ok":true,"result":{}}')) as run:
                self.assertTrue(host.dispatch(request)['ok'])
            argv = run.call_args.args[0]
            self.assertEqual(argv[:4],['runuser','-u','forge','--'])
            self.assertIn('forge.launch',argv)
            self.assertIn('forge.installation_setup',argv)
            self.assertNotIn(secret,str(argv))
            self.assertNotIn(secret,str(run.call_args.kwargs['env']))
            self.assertEqual(json.loads(run.call_args.kwargs['input'])['parameters']['secret'],secret)

    def test_old_server_reports_maintenance_before_setup_process(self):
        import os
        from forge_client import _factory_host as host
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            request = {'version':1,'intent':{'id':'a'*32},'setup':{'setup_id':'a'*32,'action':'inspect'}}
            account = SimpleNamespace(pw_uid=os.getuid(),pw_dir=str(home))
            with patch.object(host,'HOME',home), patch.object(host,'SOURCE',home/'source'), \
                 patch.object(host,'validate_intent'), patch.object(host,'facts'), \
                 patch.object(host,'read',return_value={'intent':request['intent'],'step':'complete'}), \
                 patch.object(host.pwd,'getpwnam',return_value=account), \
                 patch.object(host.subprocess,'run') as run:
                with self.assertRaisesRegex(host.InstallError, '^server_maintenance_required$'):
                    host.dispatch(request)
                run.assert_not_called()

    def test_wrong_installation_or_setup_id_cannot_reach_service_account(self):
        from forge_client import _factory_host as host
        request = {'version':1,'intent':{'id':'a'*32},'setup':{'setup_id':'b'*32}}
        with patch.object(host,'validate_intent'), patch.object(host,'facts'), \
             patch.object(host,'read',return_value={'intent':request['intent'],'step':'complete'}), \
             patch.object(host.subprocess,'run') as run:
            with self.assertRaises(host.InstallError): host.dispatch(request)
        run.assert_not_called()
