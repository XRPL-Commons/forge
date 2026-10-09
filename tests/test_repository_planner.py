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

    def test_missing_result_preserves_bounded_logs_outside_immutable_capture_until_retention(self):
        import hashlib
        import sqlite3
        from forge.store.artifacts import Artifacts
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        self.runner.inspect.return_value={'status':'exited','running':False}
        logs='prefix'*20000+'scrubbed worker diagnostic'
        self.runner.capture.return_value={'result':None,'logs':logs,'missing':['valid result artifact']}
        result=self.controller.drive(self.job)
        self.assertEqual(result['error'],'triage_result_missing')
        capture=self.plans.job('triage')['capture']
        self.assertNotIn('logs',capture)
        with self.store.read_snapshot() as db:
            saved=dict(db.execute('SELECT * FROM auxiliary_logs').fetchone())
            digest=db.execute('SELECT capture_digest FROM auxiliary_runtime').fetchone()[0]
        self.assertLessEqual(len(saved['content']),65536)
        self.assertTrue(saved['content'].endswith(b'scrubbed worker diagnostic'))
        self.assertEqual(saved['digest'],hashlib.sha256(saved['content']).hexdigest())
        retention=Artifacts(self.store)
        self.assertEqual(retention.prune_raw_logs(now=self.now+30*86400-1),0)
        with self.store.write() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE auxiliary_logs SET content=x'00'")
        self.assertEqual(retention.prune_raw_logs(now=self.now+30*86400),1)
        self.assertEqual(retention.prune_raw_logs(now=self.now+30*86400),0)
        with self.store.read_snapshot() as db:
            expired=dict(db.execute('SELECT * FROM auxiliary_logs').fetchone())
            self.assertIsNone(expired['content'])
            self.assertEqual(expired['digest'],saved['digest'])
            self.assertEqual(db.execute('SELECT capture_digest FROM auxiliary_runtime').fetchone()[0],digest)
        self.assertEqual(self.plans.job('triage')['capture'],capture)

    def test_uncleaned_logs_remain_available_and_invalid_identity_is_not_missing(self):
        from forge.store.artifacts import Artifacts
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        capture={'result':{'attempt':'other','role':'triage','outcome':'pass','rows':[]},'logs':'diagnostic'}
        self.plans.progress('triage',capture=capture,fenced=True)
        self.assertEqual(Artifacts(self.store).prune_raw_logs(now=self.now+31*86400),0)
        job=self.plans.progress('triage',cleanup=True)
        self.assertEqual(self.controller._finish(job)['error'],'triage_result_invalid')
        self.assertEqual(Artifacts(self.store).prune_raw_logs(now=self.now+31*86400),1)

    def test_valid_unsuccessful_worker_report_is_not_invalid_output(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        job=self.plans.progress('triage',capture={'result':{
            'attempt':'triage','role':'triage','outcome':'unable','rows':[]}},fenced=True,cleanup=True)
        self.assertEqual(self.controller._finish(job)['error'],'triage_worker_unsuccessful')
        self.assertEqual(self.plans.proposal(self.request['proposal_id'])['status'],'draft')

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

    def _observe(self, *, body='Details'):
        from forge.store.models import Command
        with self.store.read_snapshot() as db:
            version=db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0]
            generation=db.execute("SELECT collection_generation+1 FROM issues WHERE id='1:1'").fetchone()[0]
        Lifecycle(self.store,clock=lambda:self.now).observe_issue(Command('observe-'+str(self.now),'observer','unit',version),'1:1',
            state='open',title='Work',body=body,requirements='digest',remote_updated_at='2026-10-10T00:00:00Z',generation=generation)

    def _capture(self, *, notes=None, handoff=None):
        result={'attempt':'triage','role':'triage','outcome':'pass','summary':'Keep work','rows':[{
            'action':'keep','members':['1:1'],'reason':'Still useful','rule_quote':None,'parameters':{}}],
            'notes':notes or [],'handoff':handoff}
        self.plans.progress('triage',capture={'result':result},fenced=True,cleanup=True)
        return self.plans.job('triage')

    def _source(self):
        return {'kind':'issue_revision','identity':self.context['issues'][0]['requirements_revision_id'],
            'description':'Captured requirement','url':'https://github.com/example/project/issues/1'}

    def test_relevant_event_wake_survives_restart_without_poll_or_self_event_loop(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        self.controller._finish(self._capture())
        self.now=400
        self._observe()
        self.assertEqual(self.plans.due(),[])  # a newer observation of identical facts is not work
        self.now=450
        self._observe(body='Changed scope')
        self.assertEqual(self.plans.due(),[])  # coalesce bursts for thirty seconds
        self.now=481
        restarted=RepositoryPlanning(self.store,clock=lambda:self.now)
        self.assertEqual([(r['repository_id'],r['reason']) for r in restarted.due()],[(1,'repository_event')])
        request=restarted.queue(1,request_id='second',actor_id='supervisor:repository_event-triage',base_branch='main')
        context=restarted.context(request['proposal_id'])
        job={**self.job,'id':'second-worker','proposal_id':request['proposal_id'],'deadline':1000,'context_hash':manifest_hash(context)}
        restarted.reserve(request['proposal_id'],job,context)
        self.now=800
        self.assertEqual(restarted.due(),[])

    def test_event_after_context_capture_is_not_consumed_by_later_reservation(self):
        # An operator context edit does not alter issue launch pins. Its event
        # must remain pending when the earlier immutable context is reserved.
        self.now=110
        self.plans.note(1,note_id='operator-note',expected_revision=0,kind='convention',content='New convention',
            sources=[self._source()],request_id='operator-note',actor_id='github:7')
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        self.now=411
        self.assertEqual(self.plans.due()[0]['reason'],'repository_event')
        self.plans.configure(1,request_id='disable',actor_id='operator',expected_revision=1,rules='',proposals_enabled=False)
        self.assertEqual(self.plans.due(),[])
        self.controller.config['enabled']=False
        self.runner.inspect.return_value={'status':'running','running':True}
        self.controller.cycle(launch=False)  # already owned work is still observed
        self.runner.create.assert_not_called()

    def test_new_issue_between_input_snapshot_and_context_still_wakes_next_pass(self):
        self.now=110
        Lifecycle(self.store,clock=lambda:self.now).admit_issue(request_id='discovered',actor_id='operator',unit_id='new',issue_id='1:2',
            repository_id=1,repository_name='example/project',issue_number=2,title='New work',body='',requirements='new',observed_at=110)
        context=self.plans.context(self.request['proposal_id'])
        self.assertEqual([i['issue_id'] for i in context['issues']],['1:1'])
        self.job['context_hash']=manifest_hash(context)
        self.plans.reserve(self.request['proposal_id'],self.job,context)
        self.now=411
        self.assertEqual(self.plans.due()[0]['reason'],'repository_event')

    def test_worker_consolidates_its_own_note_and_updates_same_handoff(self):
        from forge.store.transitions import _id
        handoff_id=_id('1','planner-handoff')
        for key in ('lesson',handoff_id):
            self.plans.note(1,note_id=key,expected_revision=0,kind='lesson' if key=='lesson' else 'next_action',content='Before',
                sources=[self._source()],request_id=key,actor_id='supervisor:triage')
        self.context=self.plans.context(self.request['proposal_id']);self.job['context_hash']=manifest_hash(self.context)
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        job=self._capture(notes=[{'note_id':'lesson','kind':'lesson','content':'Consolidated','sources':[self._source()]}],
            handoff={'content':'Next investigation','sources':[self._source()]})
        self.assertEqual(self.controller._finish(job)['phase'],'complete')
        notes=self.plans.repository(1)['notes']
        self.assertEqual({n['note_id'] for n in notes},{'lesson',handoff_id})
        self.assertTrue(all(n['revision']==2 for n in notes))

    def test_event_wakes_only_its_repository_and_disabled_runtime_queues_nothing(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        self.controller._finish(self._capture())
        self.now=400
        Lifecycle(self.store,clock=lambda:self.now).admit_issue(request_id='other-repo',actor_id='operator',unit_id='other',issue_id='2:1',
            repository_id=2,repository_name='example/other',issue_number=1,title='Other',body='',requirements='other',observed_at=400)
        self.assertEqual([r['repository_id'] for r in self.plans.due()],[2])
        self.controller.config['enabled']=False
        self.assertEqual(self.controller.cycle()['items'],[])
        self.github.target.assert_not_called()
        self.controller.config['enabled']=True;self.settings.paused.return_value=True
        self.assertEqual(self.controller.cycle()['items'],[])
        self.github.target.assert_not_called()

    def test_captured_notes_and_handoff_are_sourced_atomic_and_replayable(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        note={'note_id':None,'kind':'lesson','content':'Preserve the existing requirement','sources':[self._source()]}
        job=self._capture(notes=[note],handoff={'content':'Check the remaining integration question','sources':[self._source()]})
        self.controller.plans=self.plans
        with patch.object(self.plans,'settle_run',side_effect=OSError('lost reply')):
            with self.assertRaises(OSError):self.controller._finish(job)
        self.assertEqual(self.controller._finish(job)['phase'],'complete')
        notes=self.plans.repository(1)['notes']
        self.assertEqual({n['kind'] for n in notes},{'lesson','next_action'})
        self.assertTrue(all(n['actor_id']=='supervisor:triage' and n['revision']==1 for n in notes))
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM repository_note_revisions').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
        self.now=500
        self.assertEqual(self.plans.due(),[])  # generated context never schedules itself
        self._observe(body='Changed requirement')
        self.assertTrue(all(n['needs_refresh'] for n in self.plans.repository(1)['notes']))
        self.assertEqual(self.plans.context(self.request['proposal_id'])['notes'],[])

    def test_operator_correction_wins_without_discarding_proposal(self):
        self.plans.note(1,note_id='lesson',expected_revision=0,kind='lesson',content='Initial',sources=[self._source()],
            request_id='initial',actor_id='supervisor:triage')
        self.context=self.plans.context(self.request['proposal_id']);self.job['context_hash']=manifest_hash(self.context)
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        self.plans.note(1,note_id='lesson',expected_revision=1,kind='lesson',content='Operator correction',sources=[self._source()],
            request_id='correct',actor_id='github:7')
        job=self._capture(notes=[{'note_id':'lesson','kind':'lesson','content':'Stale model update','sources':[self._source()]}])
        self.assertEqual(self.controller._finish(job)['phase'],'complete')
        self.assertEqual(self.plans.repository(1)['notes'][0]['content'],'Operator correction')
        with self.store.read_snapshot() as db:
            import json
            receipt=json.loads(db.execute("SELECT result_json FROM repository_planning_requests WHERE request_id='triage:publish'").fetchone()[0])
        self.assertEqual(receipt['notes'],[{'note_id':'lesson','status':'note_changed'}])

    def test_uncaptured_note_source_rejects_whole_proposal_without_partial_notes(self):
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        source={**self._source(),'identity':'invented-revision'}
        job=self._capture(notes=[{'note_id':None,'kind':'lesson','content':'Unproven','sources':[source]}])
        self.assertEqual(self.controller._finish(job)['error'],'triage_proposal_invalid')
        self.assertEqual(self.plans.repository(1)['notes'],[])
        self.assertEqual(self.plans.proposal(self.request['proposal_id'])['status'],'draft')
        self.assertEqual(self.plans.proposal(self.request['proposal_id'])['rows'],[])
