import json
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.factory.config import DEFAULT
from forge.factory.contracts import assignment_note, strategist_note, triage_note
from forge.factory.discussions import note as discussion_note
from forge.factory.harness import schema
from forge.mirror.policy import Repository, MirrorError
from forge.role_qualification import ROLES, schema_check, QualificationError
from forge.role_qualification_fixtures import Fixtures
from forge.store.mirror import MirrorStore
from forge_client.role_qualification import execute, HOST


class QualificationPolicyTests(unittest.TestCase):
    def setUp(self):
        from copy import deepcopy
        from forge.role_qualification import QualificationSettings
        self.config={'connections':{'controller':{'app_id':30,'installation_id':20}},'repositories':[]}
        self.installed=Mock()
        self.installed.load.side_effect=lambda:deepcopy(self.config)
        self.repository=Repository(1,'acme/project',10,'Organization',20,actual_access=True)
        self.policy=QualificationSettings(self.installed,self.repository)

    def test_disposable_selection_keeps_live_restrictions_and_never_restores_removed_config(self):
        from forge.factory.source_preparation import denied_repositories
        self.assertEqual(self.policy.load()['repositories'],[{'id':1,'name':'acme/project','enabled':False}])
        self.assertEqual(self.config['repositories'],[])
        deny={'repository_id':2,'canonical_name':'vendor/private'}
        self.config['repositories']=[dict(id=1,name='acme/project',enabled=False,mirror_denied_repositories=[deny])]
        self.assertEqual(denied_repositories(self.policy,1),[deny])
        self.config['connections']={}
        self.assertEqual(self.policy.load()['connections'],{})
        self.config['repositories']=[]
        with self.assertRaisesRegex(MirrorError,'repository_unavailable'):
            self.policy.load()

    def test_existing_deny_blocks_source_preparation_without_enabling_repository(self):
        from forge.role_qualification import CanarySource
        from forge.worker_qualification import Fixture
        from tests.test_source_preparation import Access
        deny={'repository_id':1,'canonical_name':'acme/project'}
        self.config['repositories']=[dict(id=1,name='acme/project',enabled=False,mirror_denied_repositories=[deny])]
        with tempfile.TemporaryDirectory(prefix='qr-',dir='/tmp') as temp:
            root=Path(temp)
            git=Fixture(root)
            fixtures=Fixtures(root/'fixture',self.repository,dict(number=10,title='Issue',body='Body'),
                              'requirements',git.oid,dict(DEFAULT),180)
            job=fixtures.reserve('build')
            source=CanarySource(fixtures.store.root,fixtures.store,settings=self.policy,
                                access=Access(),git_store=git.cache)
            with self.assertRaisesRegex(MirrorError,'project_denied'):
                source.prepare(job,assignment_note(job))
            self.config['repositories'][0]['mirror_denied_repositories']=[]
            prepared=source.prepare(job,assignment_note(job))
            from forge.mirror_daemon import MirrorDaemon
            from forge.mirror.service import Broker
            daemon=MirrorDaemon(fixtures.store.root,fixtures.store,settings=self.policy,
                                access=Access(),git_store=git.cache)
            broker=Broker(prepared.session_id,source.mirror,git.cache,resolve=daemon.resolve,
                          live_authority=daemon.live_authority,live_policy=daemon.policy)
            self.config['repositories'][0]['mirror_denied_repositories']=[deny]
            status,_,_=broker.handle(dict(method='GET',path='/github/acme/project.git/info/refs?service=git-upload-pack',
                                          protocol='',content_type='',body_length=0),b'')
            self.assertEqual(status,403)
            receipt=source.mirror.receipts(job['id'])[-1]['details']
            self.assertEqual(receipt['reason'],'project_denied')
            self.assertEqual(receipt['request_kind'],'advertisement')
        self.assertFalse(self.config['repositories'][0]['enabled'])
        self.assertEqual(self.config['repositories'][0]['mirror_denied_repositories'],[deny])

    def test_repository_name_reuse_cannot_supply_another_project_policy(self):
        self.config['repositories']=[dict(id=2,name='acme/project',enabled=False)]
        with self.assertRaisesRegex(MirrorError,'repository_unavailable'):
            self.policy.load()


class RoleFixturesTests(unittest.TestCase):
    def test_seven_roles_hold_real_exact_sql_authority_and_revoke(self):
        with tempfile.TemporaryDirectory() as temp:
            for index, role in enumerate(ROLES):
                with self.subTest(role=role):
                    repository=Repository(1,'acme/project',2,'User',None,'main',True)
                    fixtures=Fixtures(Path(temp)/str(index),repository,
                        dict(number=10,title='Actual issue context',body='Read-only context'),
                        'requirements', 'a'*40,dict(DEFAULT),180)
                    job=fixtures.reserve(role)
                    mirror=MirrorStore(fixtures.store)
                    authority=mirror.authority(job['id'])
                    self.assertIsNotNone(authority)
                    note=(triage_note(job) if role=='triage' else discussion_note(job) if role=='discussion'
                          else strategist_note(job) if role=='strategist' else assignment_note(job))
                    self.assertEqual(schema(note)['properties']['role']['enum'],[role])
                    fixtures.fence(job)
                    with self.assertRaises(MirrorError):mirror.authority(job['id'])


    def test_all_roles_freeze_inputs_and_fetch_through_owned_broker(self):
        import threading
        from forge.worker_qualification import Fixture
        from forge.role_qualification import CanarySource, QualificationSettings
        from forge.mirror_daemon import MirrorDaemon
        from forge.mirror.frontend import Frontend
        from forge.mirror.git_store import run_git
        from tests.test_source_preparation import Access
        from forge.settings import Settings
        from forge.store.fresh import initialize
        from tests.test_settings import configured
        with tempfile.TemporaryDirectory(prefix='qr-',dir='/tmp') as temp:
            root=Path(temp)
            git=Fixture(root)
            access=Access()
            (root/'installation').mkdir(mode=0o700)
            initialize(root/'installation')
            installed_settings=Settings(root/'installation',scope='active')
            installed_settings.save_config(configured())
            self.assertEqual(installed_settings.scope, 'setup')
            self.assertEqual(installed_settings.load()['repositories'], [])
            original_config=installed_settings.config_snapshot()
            settings=QualificationSettings(installed_settings,access.resolve('acme/project'))
            # The local cache fixture has repository id 1 and a tracked README.
            for index,role in enumerate(ROLES):
                with self.subTest(role=role):
                    fixtures=Fixtures(root/str(index),access.resolve('acme/project'),
                        dict(number=10,title='Issue',body='Body'),'requirements',git.oid,dict(DEFAULT),180)
                    job=fixtures.reserve(role)
                    source=CanarySource(fixtures.store.root,fixtures.store,settings=settings,
                                        access=access,git_store=git.cache)
                    note=(triage_note(job) if role=='triage' else discussion_note(job) if role=='discussion'
                          else strategist_note(job) if role=='strategist' else assignment_note(job))
                    prepared=source.prepare(job,note)
                    frozen=json.loads((prepared.input_directory/'assignment.json').read_text())
                    self.assertTrue(frozen['qualification']['fixture'])
                    self.assertIn('QUALIFICATION_READ',frozen['instructions'])
                    daemon=MirrorDaemon(fixtures.store.root,fixtures.store,settings=settings,
                                        access=access,git_store=git.cache)
                    try:
                        daemon.step()
                        with Frontend(('127.0.0.1',0),str(prepared.socket_directory/'session.sock')) as frontend:
                            thread=threading.Thread(target=frontend.serve_forever,daemon=True);thread.start()
                            try:
                                url=f'http://127.0.0.1:{frontend.server_port}/github/acme/project.git'
                                checkout=root/('clone'+str(index))
                                run_git(['clone',url,str(checkout)])
                                self.assertEqual(run_git(['-C',str(checkout),'rev-parse','HEAD']).decode().strip(),git.oid)
                                receipts=source.mirror.receipts(job['id'])
                                self.assertTrue(any(row['details'].get('request_kind')=='upload' and
                                    row['details'].get('outcome')=='served' for row in receipts))
                                if role=='build':
                                    from forge.role_qualification import validate_capture
                                    from forge.publication_artifacts import PublicationClaim
                                    from forge.factory.source_inputs import digest
                                    blob=run_git(['-C',str(checkout),'hash-object','README.md']).decode().strip()
                                    (checkout/'forge-qualification.txt').write_text('README.md '+blob+'\n')
                                    run_git(['-C',str(checkout),'add','forge-qualification.txt'])
                                    run_git(['-C',str(checkout),'-c','user.name=Fixture','-c','user.email=fixture@example.test',
                                             '-c','commit.gpgsign=false','commit','-m','qualification'])
                                    head=run_git(['-C',str(checkout),'rev-parse','HEAD']).decode().strip()
                                    bundle_path=root/'output.bundle'
                                    run_git(['-C',str(checkout),'bundle','create',str(bundle_path),'HEAD','^'+git.oid])
                                    bundle=bundle_path.read_bytes()
                                    job['source_input']=prepared.source_input
                                    source.config=dict(DEFAULT)
                                    value=dict(version=1,attempt=job['id'],role='build',outcome='pass',blocker=None,
                                        summary='QUALIFICATION_READ README.md '+blob,headline='Read source',plain='Read source',
                                        operator_summary='Read source',issues=[],evidence=None,decision=None,verification=None,
                                        required_services=[],output_commit=head,pr_title='Qualification',pr_body='Local fixture')
                                    receipt=dict(local_head_oid=head,source_digest=digest(prepared.source_input),
                                        job_id=job['id'],bundle_size=len(bundle),bundle_digest=hashlib.sha256(bundle).hexdigest())
                                    capture=dict(result=value,execution=dict(running=False,exit_code=0),
                                        execution_envelope=dict(harness_status='ok'),worktree_bundle=bundle,export_receipt=receipt)
                                    self.assertEqual(validate_capture(capture,job,frozen,source)['status'],'qualified')
                                    capture['worktree_bundle']=b'not a Git bundle'
                                    with self.assertRaises(PublicationClaim):validate_capture(capture,job,frozen,source)
                                fixtures.fence(job)
                                with self.assertRaises(MirrorError):run_git(['-C',str(checkout),'fetch','origin'])
                            finally:
                                frontend.shutdown();thread.join()
                    finally:daemon.close()
            self.assertEqual(installed_settings.config_snapshot(),original_config)


    def test_idle_probe_refuses_enabled_or_unfinished_work_without_writes(self):
        from forge.role_qualification import idle
        from forge.store.configuration import Configuration
        from forge.store.db import Store
        with tempfile.TemporaryDirectory() as temp:
            store=Store.create(Path(temp)/'store')
            config=Configuration(store)
            config.replace('setup/runtime.json',dict(DEFAULT),expected_revision=0)
            before=idle(store)
            self.assertEqual(before,[('setup/runtime.json',1)])
            config.replace('setup/runtime.json',{**DEFAULT,'enabled':True},expected_revision=1)
            with self.assertRaisesRegex(QualificationError,'automation'):idle(store)
            self.assertTrue(config.get('setup/runtime.json')['settings']['enabled'])


    def test_failed_launch_fences_before_teardown_and_preserves_cleanup_failure(self):
        from forge.role_qualification import qualify
        from forge.store.db import Store
        from forge.store.configuration import Configuration
        from forge.worker_qualification import Fixture
        from tests.test_source_preparation import Access
        from contextlib import ExitStack
        import shutil
        with tempfile.TemporaryDirectory(prefix='qr-',dir='/tmp') as temp:
            root=Path(temp)
            store=Store.create(root/'live')
            config={**DEFAULT,'image':'sha256:'+'a'*64,'provider_environment':[]}
            Configuration(store).replace('setup/runtime.json',config,expected_revision=0)
            git=Fixture(root)
            access=Access()
            for broken_cleanup in (False,True):
                runner=Mock()
                runner.create.side_effect=QualificationError('fixture_launch_failure')
                def reset(job):
                    self.assertTrue(job['fenced'])
                    if broken_cleanup:raise RuntimeError('fixture_cleanup_failure')
                runner.reset.side_effect=reset
                with ExitStack() as stack:
                    for target,value in (
                        ('forge.store.runtime.selected',store),
                        ('forge.role_qualification.configuration',config),
                        ('forge.role_qualification.idle_logins',None),
                        ('forge.mirror_access.MirrorAccess',access),
                        ('forge.mirror.git_store.GitStore',git.cache),
                        ('forge.factory.github.GitHub.issue_inputs',(dict(number=10,title='Issue',body='Body'),'requirements',[])),
                        ('forge.credentials.Credentials.environment',{}),
                        ('forge.role_qualification.CanaryRunner',runner)):
                        stack.enter_context(patch(target,return_value=value))
                    settings=stack.enter_context(patch('forge.settings.Settings'))
                    settings.return_value.load.return_value={'connections':{},'repositories':[]}
                    daemon=stack.enter_context(patch('forge.mirror_daemon.MirrorDaemon'))
                    report=qualify(root/'live','acme/project',10,apply=True)
                self.assertEqual(report['status'],'failed')
                self.assertEqual(report['cleanup'],'unconfirmed' if broken_cleanup else 'confirmed')
                runner.reset.assert_called_once()
                daemon.return_value.close.assert_called_once()
                self.assertEqual(report['automation_after'],'disabled')
                if broken_cleanup:
                    shutil.rmtree(report['retained_evidence_directory'])


    def test_login_cleanup_marker_does_not_mean_active_login(self):
        from forge.role_qualification import idle_logins
        runner=Mock()
        with patch('forge.role_qualification.ContainerRunner',return_value=runner):
            runner.call.return_value.stdout=b''
            idle_logins('/tmp/qualification-installation',dict(DEFAULT))
            command=runner.call.call_args.args[0]
            self.assertIn('--filter',command)
            self.assertTrue(any(item.startswith('label=forge.login-root=') for item in command))
            runner.call.return_value.stdout=b'active-container\n'
            with self.assertRaisesRegex(QualificationError,'login'):idle_logins('/tmp/qualification-installation',dict(DEFAULT))

    def test_generated_schema_checks_role_identity_types_and_closed_fields(self):
        spec={'type':'object','properties':{'role':{'type':'string','enum':['review']},
              'flag':{'anyOf':[{'type':'null'},{'type':'boolean'}]}},'required':['role','flag'],'additionalProperties':False}
        schema_check({'role':'review','flag':None},spec)
        for value in ({'role':'build','flag':None},{'role':'review','flag':1},
                      {'role':'review'},{'role':'review','flag':False,'extra':1}):
            with self.assertRaises(QualificationError):schema_check(value,spec)

    def test_cli_plan_uses_qualified_runtime_and_never_silently_applies(self):
        installation=Mock()
        installation.journal.read.return_value={'host_key':'pinned'}
        installation.host.rpc.return_value={'result':{'status':'planned','roles':list(ROLES)}}
        args=SimpleNamespace(ssh='anvil',repo='acme/project',issue=10,apply=False)
        result=execute(args,installation=installation)
        script,payload=installation.host.rpc.call_args.args
        self.assertFalse(payload['apply'])
        self.assertIn("'forge.launch','forge.role_qualification'",script)
        self.assertEqual(installation.host.fingerprint,'pinned')
        self.assertIn('seven',result['next_action'])

    def test_cli_refuses_qualified_without_cleanup(self):
        installation=Mock()
        installation.journal.read.return_value={}
        installation.host.rpc.return_value={'result':{'status':'qualified','cleanup':'unconfirmed'}}
        with self.assertRaises(Exception):
            execute(SimpleNamespace(ssh='anvil',repo='acme/project',issue=10,apply=True),installation=installation)
