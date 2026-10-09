import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.store.db import Store
from forge.store.models import TransitionError
from forge.store.repository_planning import RepositoryPlanning
from forge.store.transitions import Lifecycle, manifest_hash
from forge.factory.repository_planner import RepositoryPlanner
from forge.factory.harness import schema


class RepositoryPlannerTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store.create(temporary.name);self.now=100
        Lifecycle(self.store,clock=lambda:self.now).admit_issue(request_id='admit',actor_id='operator',unit_id='unit',issue_id='1:1',
            repository_id=1,repository_name='example/project',issue_number=1,title='Work',body='Details',requirements='digest',observed_at=90)
        self.plans=RepositoryPlanning(self.store,clock=lambda:self.now)
        self.request=self.plans.queue(1,request_id='request',actor_id='operator',base_branch='main')
        self.context=self.plans.context(self.request['proposal_id'])
        self.job={'id':'triage','proposal_id':self.request['proposal_id'],'slot':'repository-triage','role':'triage','repo_id':1,'repo':'example/project',
            'head':'a'*40,'deadline':500,'fenced':False,'cleanup':False,'runner_config':{},'context_hash':manifest_hash(self.context)}
        self.runner=Mock();self.github=Mock();self.settings=Mock();self.settings.paused.return_value=False
        self.controller=RepositoryPlanner(self.store,self.github,config={'enabled':True},settings=self.settings,observations=Mock(),
            runner=lambda config:self.runner,clock=lambda:self.now)

    def test_readonly_capture_requires_cleanup_and_then_only_publishes_proposal(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        self.runner.inspect.return_value={'status':'created','running':False}
        self.github.token.return_value='private-token'
        self.controller.drive(self.job)
        self.github.token.assert_called_once_with('recovery',1,worker=True)
        prepared=self.runner.prepare.call_args.args[0]
        self.assertEqual(prepared['context'],self.context)
        self.runner.inspect.return_value={'status':'exited','running':False}
        result={'attempt':'triage','role':'triage','outcome':'pass','summary':'Keep work','rows':[{
            'action':'keep','members':['1:1'],'reason':'Still useful','rule_quote':None,'parameters':{}}]}
        self.runner.capture.return_value={'result':result,'logs':'finished'}
        def cleaned(job):
            with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT status FROM triage_proposals').fetchone()[0],'draft')
        self.runner.reset.side_effect=cleaned
        self.assertEqual(self.controller.drive(self.job)['phase'],'complete')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT status FROM triage_proposals').fetchone()[0],'proposed')
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM auxiliary_leases').fetchone()[0],0)
        self.github.call.assert_not_called()

    def test_lost_publication_response_resumes_cleaned_capture_without_another_worker(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        capture={'result':{'attempt':'triage','role':'triage','outcome':'pass','summary':'Keep work','rows':[{
            'action':'keep','members':['1:1'],'reason':'Still useful','rule_quote':None,'parameters':{}}]}}
        self.plans.progress('triage',capture=capture,fenced=True,cleanup=True)
        with patch.object(self.plans,'settle_run',side_effect=OSError('lost response')):
            self.controller.plans=self.plans
            with self.assertRaises(OSError):self.controller._finish(self.plans.job('triage'))
        self.assertEqual(self.controller.cycle()['items'][0]['phase'],'complete')
        self.runner.create.assert_not_called()
        self.assertEqual(self.plans.proposal(self.request['proposal_id'])['version'],2)

    def test_timeout_tears_down_before_failed_receipt_and_does_not_charge_work(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context);self.now=600
        self.assertEqual(self.controller.drive(self.job)['phase'],'failed')
        self.runner.reset.assert_called_once()
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT status FROM repository_triage_runs').fetchone()[0],'failed')

    def test_context_is_immutable_and_role_schema_has_explicit_publication_target(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        with self.assertRaises(TransitionError):self.plans.progress('triage',context_hash='different')
        contract=schema({'id':'triage','role':'triage'})
        choices=contract['properties']['rows']['items']['properties']['parameters']['anyOf']
        group=next(row for row in choices if 'existing_prs' in row['properties'])
        self.assertIn('publication_target',group['required'])

    def test_launch_refreshes_entire_open_backlog_with_native_observation_and_inventory(self):
        from forge.factory.observation_controller import ObservationController
        config={'enabled':True,'ready_label':'ready','hold_labels':['hold'],'availability_retry_seconds':30,
                'provider_pool':[{'id':'account','provider':'codex','model':None,'provider_environment':[],'provider_auth_file_env':None}],
                'provider_ready':['account']}
        self.settings.admission.return_value={'allowed':True}
        self.github.pages.return_value=[{'number':1},{'number':2},{'number':3,'pull_request':{}}]
        self.github.pulls.return_value=[]
        def issue(item):
            number=item['number']
            return {'number':number,'state':'open','title':'Work '+str(number),'body':'Details','labels':[],
                'assignees':[],'updated_at':'2026-10-10T00:00:00Z'},'digest' if number==1 else 'digest2'
        self.github.issue.side_effect=issue
        self.github.call.return_value={'commit':{'sha':'a'*40}}
        self.runner.inspect.side_effect=[None,{'status':'created','running':False}]
        self.runner.create.return_value='owned-container'
        observer=ObservationController(self.store,self.github,admission=lambda _:True,clock=lambda:self.now)
        controller=RepositoryPlanner(self.store,self.github,config=config,settings=self.settings,observations=observer,
            runner=lambda _:self.runner,clock=lambda:self.now)
        result=controller._start(self.plans.runs()[0])
        self.assertEqual(result['phase'],'running')
        prepared=self.runner.prepare.call_args.args[0]
        self.assertEqual({row['issue_id'] for row in prepared['context']['issues']},{'1:1','1:2'})
        self.assertEqual(self.github.pages.call_args.args[2],'/issues?state=open')
        self.github.token.assert_called_once_with('recovery',1,worker=True)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM units').fetchone()[0],1)  # discovery did not enqueue #2
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
