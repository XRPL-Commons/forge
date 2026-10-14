"""Planner assignments use their selected provider through production wiring."""
import io
import json
import tarfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from forge.factory.config import validate
from forge.factory.runner import ContainerRunner
from forge.factory.sqlite_runtime import components
from forge.store.transitions import Lifecycle, manifest_hash
from tests import test_sqlite_runtime_support as fixtures


class PlannerProviderSelectionTests(unittest.TestCase):
    setUp=fixtures.RuntimeFixture.setUp
    prepare=fixtures.RuntimeFixture.prepare

    def planner(self,runner,config):
        github=Mock()
        github.token.return_value='read-only-github-test-token'
        return components(self.root,config=config,runner=runner,github=github,
            settings=self.settings,clock=lambda:self.now)['planning'],github

    def reserve(self,planner,worker):
        Lifecycle(self.database,clock=lambda:self.now).admit_issue(request_id='admit',actor_id='operator',
            unit_id='unit',issue_id='10:1',repository_id=10,repository_name='example/project',issue_number=1,
            title='Work',body='Details',requirements='digest',observed_at=self.now)
        request=planner.plans.queue(10,request_id='plan',actor_id='operator',base_branch='main')
        context=planner.plans.context(request['proposal_id'],head='a'*40)
        job=dict(id='11111111-1111-4111-8111-111111111111',proposal_id=request['proposal_id'],slot='repository-triage',role='triage',
            repo_id=10,repo='example/project',head='a'*40,deadline=self.now+1800,fenced=False,cleanup=False,
            runner_config=worker,context_hash=manifest_hash(context))
        planner.plans.reserve(request['proposal_id'],job,context)
        return job

    def test_setup_supplied_default_runner_cannot_override_the_selected_claude_assignment(self):
        self.prepare()
        default=validate({'enabled':True,'image':'sha256:'+'a'*64,'provider':'codex',
            'provider_environment':['OPENAI_API_KEY'],'seccomp_profile_env':None})
        selected={**default,'provider':'claude','model':'claude-test-model',
            'provider_environment':['CLAUDE_CODE_OAUTH_TOKEN'],'provider_auth_file_env':None}
        environment={'OPENAI_API_KEY':'unused-codex-test-key','CLAUDE_CODE_OAUTH_TOKEN':'selected-claude-test-token'}
        base=ContainerRunner(default,environ=environment)
        planner,github=self.planner(base,default)
        job=self.reserve(planner,selected)
        job=planner.plans.job(job['id'],context=True)
        commands=[];payloads=[];notes=[]
        source=base.source_preparation
        source.mirror=Mock()
        source.mirror.prepared.return_value={'source_input':{},'execution_fence':job['id'],
                                            'publication_authorization':{},'revoked_at':None}
        source.mirror.live_authority.return_value=True
        def freeze(job,note,**kwargs):
            notes.append(note)
            return SimpleNamespace(input_directory='/fixture/input',socket_directory='/fixture/socket')
        source.prepare=Mock(side_effect=freeze)
        def call(runner,args,**kwargs):
            commands.append((runner.config,args))
            if args[:3]==['cp','-a','-']:
                with tarfile.open(fileobj=io.BytesIO(kwargs['input'])) as archive:
                    payloads.append({name:json.load(archive.extractfile(name))
                        for name in ('credentials.json',)})
            return Mock(stdout=b'')
        created={'id':'container','status':'created','running':False}
        with patch.object(ContainerRunner,'call',autospec=True,side_effect=call), \
                patch('forge.factory.worker_boundary.WorkerBoundary.prepare',return_value=SimpleNamespace(worker_args=())):
            worker=planner.runner(planner.plans.job(job['id'])['runner_config'])
            self.assertIsNot(worker,base)
            self.assertIs(worker.environ,environment)
            # Exercise the real creation command and provider isolation policy.
            with patch.object(ContainerRunner,'inspect',side_effect=[None,created]):
                self.assertEqual(worker._create(job),'container')
            with patch.object(ContainerRunner,'inspect',return_value=created), \
                    patch.object(ContainerRunner,'capacity',return_value={'allowed':True}):
                self.assertEqual(planner.drive(job),{'phase':'running'})
        creation=next(args for _,args in commands if args[0]=='create')
        self.assertIn('systempaths=unconfined',creation)
        self.assertIn('apparmor=forge-runner-private-proc',creation)
        self.assertTrue(all(config['provider']=='claude' for config,_ in commands))
        self.assertTrue(any(args[0]=='start' for _,args in commands))
        self.assertEqual(len(payloads),1)
        assignment=notes[-1];credentials=payloads[0]['credentials.json']
        self.assertEqual(assignment['provider'],'claude')
        self.assertEqual(assignment['model'],'claude-test-model')
        self.assertEqual(assignment['harness_argv'],selected['harness_argv'])
        self.assertEqual(assignment['role'],'triage')
        captured=planner.plans.job(job['id'],context=True)['context']
        self.assertEqual(assignment['context']['note_sources_schema'],captured['note_sources_schema'])
        self.assertEqual(manifest_hash(assignment['context']),job['context_hash'])
        self.assertEqual(credentials,{'CLAUDE_CODE_OAUTH_TOKEN':'selected-claude-test-token'})
        github.token.assert_not_called()
        self.assertEqual(base.config['provider'],'codex')

    def test_injected_non_container_adapter_remains_the_planner_engine(self):
        self.prepare()
        config=validate({'enabled':True,'image':'sha256:'+'a'*64})
        adapter=Mock()
        adapter.inspect.return_value={'id':'fake','status':'created','running':False}
        planner,github=self.planner(adapter,config)
        job=self.reserve(planner,config)
        self.assertIs(planner.runner(config),adapter)
        self.assertEqual(planner.drive(job),{'phase':'running'})
        adapter.prepare.assert_called_once()
        adapter.start.assert_called_once()
        self.assertEqual(adapter.prepare.call_args.args[0]['runner_config'],config)
        self.assertEqual(len(adapter.prepare.call_args.args),1)
        github.token.assert_not_called()
