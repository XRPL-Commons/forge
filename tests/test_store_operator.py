from copy import deepcopy
import json
import unittest
import uuid

from forge.factory.config import validate
from forge.factory.store_operator import OperatorActions
from forge.factory.store_views import PipelineViews, action_revision
from forge.factory.github import GitHubUnavailable
from forge.store.models import IdempotencyConflict, TransitionError, VersionConflict
from tests import test_store_result_controller as helpers


class Remote(helpers.GitHub):
    def __init__(self):
        super().__init__()
        self.calls=[];self.after_write=None;self.comments=[];self.events=[]
    def publisher(self,role):return 'forge-controller[bot]'
    def pages(self,role,item,path):
        return deepcopy(self.events if path.endswith('/events') else self.comments)
    def call(self,role,item,method,path,payload=None):
        self.calls.append((method,path,payload))
        if method=='POST' and path.endswith('/labels'):
            self.issue_value['labels']=list(set(self.issue_value.get('labels',[]))|set(payload['labels']))
        elif method=='DELETE' and '/labels/' in path:
            from urllib.parse import unquote
            label=unquote(path.split('/labels/')[1]);self.issue_value['labels']=[v for v in self.issue_value['labels'] if v!=label]
        elif method=='PATCH' and '/issues/' in path:
            before=self.issue_value['state'];self.issue_value.update(payload)
            if before!=self.issue_value['state']:
                self.events.append(dict(id=len(self.events)+1,event='closed' if payload['state']=='closed' else 'reopened',
                                        actor=dict(id=123,login=self.publisher('controller'),type='Bot')))
        elif method=='POST' and path.endswith('/comments'):self.comments.append(dict(payload))
        else:raise AssertionError((method,path,payload))
        if self.after_write:self.after_write()
        return deepcopy(self.issue_value)


class OperatorTests(unittest.TestCase):
    command=helpers.ResultControllerTests.command
    view=helpers.ResultControllerTests.view
    reserve=helpers.ResultControllerTests.reserve
    def setUp(self):
        helpers.ResultControllerTests.setUp(self)
        self.github=Remote();self.now=100
        self.actions=OperatorActions(self.store,self.github,actor_id='github:1609016',config=validate({}),clock=lambda:self.now)
    def revision(self):
        with self.store.read_snapshot() as db:return action_revision(db,'i')
    def request(self,action,reason='Operator reason',**kwargs):
        return self.actions.request('i',action,self.revision(),reason,**kwargs)
    def status(self,identifier):
        with self.store.read_snapshot() as db:return dict(db.execute('SELECT * FROM issue_action_requests WHERE id=?',(identifier,)).fetchone())
    def test_pause_fences_before_return_then_readback_resumes_without_losing_independent_gate(self):
        job=self.reserve()
        self.life.open_gate(self.command(),'environment',details={'reason':'database'})
        request=self.request('pause')
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?',(job,)).fetchone()[0],1)
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertIn('hold',self.github.issue_value['labels'])
        self.assertEqual(self.status(request)['status'],'applied')
        self.assertEqual(self.view()['current_blocker']['code'],'operator_pause')
    def test_network_failure_after_remote_write_is_reconciled_on_restart(self):
        request=self.request('pause')
        self.github.after_write=lambda:(_ for _ in ()).throw(GitHubUnavailable('lost response'))
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.github.after_write=None
        restarted=OperatorActions(self.store,self.github,config=validate({}),clock=lambda:100)
        self.assertTrue(restarted.step(request)['applied'])
        self.assertEqual(len(self.github.calls),1)
    def test_idempotency_binds_actor_payload_and_revision(self):
        identifier=str(uuid.uuid4());revision=self.revision()
        self.actions.request('i','pause',revision,'Pause',request_id=identifier)
        self.assertEqual(self.actions.request('i','pause',revision,'Pause',request_id=identifier),identifier)
        self.actions.actor_id='github:other'
        with self.assertRaises(IdempotencyConflict):self.actions.request('i','pause',revision,'Pause',request_id=identifier)
    def test_stale_revision_has_no_side_effects(self):
        revision=self.revision();self.life.pause(self.command())
        with self.assertRaises(VersionConflict):self.actions.request('i','pause',revision,'Pause')
        self.assertEqual(self.github.calls,[])
    def test_fresh_remote_requirements_change_rejects_and_preserves_hold(self):
        request=self.request('pause');self.github.requirements='changed'
        self.assertTrue(self.actions.step(request)['rejected'])
        self.assertEqual(self.github.calls,[])
        self.assertEqual(self.view()['current_blocker']['code'],'operator_pause')
    def test_rejected_recheck_does_not_create_a_dead_end_or_clear_independent_hold(self):
        self.life.open_gate(self.command(),'environment',details={'reason':'database'})
        request=self.request('recheck');self.github.requirements='changed'
        self.assertTrue(self.actions.step(request)['rejected'])
        with self.store.read_snapshot() as db:
            codes=[r[0] for r in db.execute("SELECT code FROM unit_gates WHERE resolved_at IS NULL")]
            self.assertEqual(codes,['environment'])
            self.assertIsNotNone(db.execute("SELECT 1 FROM operator_observation_requests WHERE issue_id='i'").fetchone())
        self.assertEqual(self.github.calls,[])
        # A fresh canonical observation changes the revision; retry uses that
        # revision and the new requirements, never the rejected request's pins.
        old_revision=self.revision()
        self.life.observe_issue(self.command(),'i',state='open',title='Issue',body='Body',requirements='changed',
            remote_updated_at='2026-10-13T12:00:00Z',generation=1)
        self.assertNotEqual(self.revision(),old_revision)
        with self.assertRaises(VersionConflict):self.actions.request('i','recheck',old_revision,'Old decision')
        self.assertTrue(self.actions.step(self.request('recheck'))['applied'])
        self.assertEqual(self.view()['reason_code'],'environment')
        self.assertEqual(self.view()['charged_failures'],0)

    def candidate_recheck(self):
        self.controller.github=self.github
        job=self.reserve()
        helpers.ResultControllerTests.publish(self,job)
        helpers.ResultControllerTests.capture(self,job,helpers.ResultControllerTests.report(self,job))
        self.assertTrue(self.controller.step(job)['accepted'])
        selected=self.view()['current_candidate_id']
        self.life.open_gate(self.command(),'external_change',details={'reason':'candidate_head_changed'})
        return job,selected

    def test_reload_changed_head_invalidates_candidate_and_retains_exact_owned_publication(self):
        from forge.store.delivery_inputs import previous_publication_in
        job,selected=self.candidate_recheck()
        self.life.open_gate(self.command(),'environment',details={'reason':'database'})
        self.github.pr_value['head']['sha']='c'*40
        request=self.request('recheck')
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.view()['state'],'building')
        self.assertEqual(self.view()['reason_code'],'environment')
        self.assertEqual(self.view()['charged_failures'],0)
        with self.store.read_snapshot() as db:
            publication=previous_publication_in(db,'u')
            self.assertEqual(publication['source_job_id'],job)
            self.assertEqual(publication['head_oid'],'c'*40)
            self.assertEqual(publication['author'],self.github.pr_value['user'])
            self.assertIsNotNone(db.execute('SELECT id FROM candidates WHERE id=?',(selected,)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='external_change' AND resolved_at IS NULL").fetchone()[0],0)
        self.assertEqual(self.github.calls,[])
        self.assertEqual(self.actions.step(request)['status'],'applied')

    def test_reload_does_not_adopt_a_different_author_branch_or_repository(self):
        self.candidate_recheck()
        original=deepcopy(self.github.pr_value)
        for part in ('author','branch','repository'):
            with self.subTest(part=part):
                self.github.pr_value=deepcopy(original)
                self.github.pr_value['head']['sha']='c'*40
                if part=='author':self.github.pr_value['user']['login']='someone'
                elif part=='branch':self.github.pr_value['head']['ref']='another-branch'
                else:self.github.pr_value['head']['repo']['id']=2
                self.assertTrue(self.actions.step(self.request('recheck'))['rejected'])
                self.assertIsNotNone(self.view()['current_candidate_id'])
                self.assertEqual(self.view()['reason_code'],'external_change')
        self.assertEqual(self.github.calls,[])

    def test_unavailable_candidate_recheck_explains_the_required_remote_repair(self):
        self.candidate_recheck();original=deepcopy(self.github.pr_value)
        for field,value,message in (('draft',True,'ready for review'),('state','closed','Reopen'),('merged',True,'reconciled')):
            with self.subTest(field=field):
                self.github.pr_value=deepcopy(original);self.github.pr_value[field]=value
                request=self.request('recheck')
                self.assertTrue(self.actions.step(request)['rejected'])
                self.assertIn(message,self.status(request)['message'])
                self.assertEqual(self.view()['reason_code'],'external_change')

    def test_resume_clears_only_intent_and_verified_hold(self):
        self.actions.step(self.request('pause'))
        self.life.open_gate(self.command(),'environment',details={'reason':'database'})
        request=self.request('resume')
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertNotIn('hold',self.github.issue_value['labels'])
        self.assertEqual(self.view()['current_blocker']['code'],'environment')
    def test_fresh_remote_human_owner_blocks_resume(self):
        self.actions.step(self.request('pause'));request=self.request('resume')
        self.github.issue_value['assignees']=[{'login':'someone','type':'User'}]
        self.assertTrue(self.actions.step(request)['rejected'])
        self.assertIn('hold',self.github.issue_value['labels'])
    def test_close_observed_issue_terminal_and_no_budget_charge(self):
        request=self.request('close')
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertEqual(self.view()['state'],'closed')
        self.assertEqual(self.view()['close_code'],'github_not_planned')
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)
        # The operator's reason is the closing comment on GitHub, as the dialog promises.
        self.assertEqual(self.github.comments,[{'body':'Closed from Forge as not planned: Operator reason'}])
    def test_close_comment_is_posted_once_across_a_lost_response(self):
        request=self.request('close')
        self.github.after_write=lambda:(_ for _ in ()).throw(GitHubUnavailable('lost response'))
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.github.after_write=None
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertEqual(len(self.github.comments),1)
    def test_reopen_undoes_an_applied_close_and_asks_for_a_fresh_look(self):
        request=self.request('close')
        with self.assertRaises(TransitionError):self.actions.reopen('i',request)
        self.actions.step(request)
        undo=self.actions.reopen('i',request)
        self.assertEqual(self.github.issue_value['state'],'closed')
        self.assertEqual(self.status(undo)['status'],'pending')
        self.assertTrue(self.actions.step(undo)['applied'])
        self.assertEqual(self.github.issue_value['state'],'open')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT actor_id FROM operator_observation_requests WHERE issue_id=?',('i',)).fetchone()[0],'github:1609016')
        self.github.calls.clear()
        self.assertEqual(self.actions.reopen('i',request),undo)
        self.actions.step(undo)
        self.assertEqual(self.github.calls,[])
        with self.store.read_snapshot() as db:
            states=[json.loads(row[0])['status'] for row in db.execute("SELECT payload_json FROM events WHERE json_extract(payload_json,'$.operator_request')=? ORDER BY sequence",(undo,))]
        self.assertEqual(states,['pending','applied'])

    def test_close_and_undo_recover_lost_write_responses_from_exact_event_lineage(self):
        request=self.request('close')
        def lose_patch():
            if self.github.calls[-1][0]=='PATCH':raise GitHubUnavailable('lost reply')
        self.github.after_write=lose_patch
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.github.after_write=None
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertEqual(len(self.github.events),1)
        undo=self.actions.reopen('i',request)
        self.github.after_write=lose_patch
        self.assertEqual(self.actions.step(undo)['pending'],'github_observation')
        self.github.after_write=None
        self.assertTrue(self.actions.step(undo)['applied'])
        self.assertEqual([event['event'] for event in self.github.events],['closed','reopened'])

    def test_undo_never_reopens_a_later_unrelated_closure(self):
        request=self.request('close');self.actions.step(request)
        undo=self.actions.reopen('i',request)
        self.github.events.extend([dict(id=2,event='reopened',actor=dict(id=99,login='operator',type='User')),
                                   dict(id=3,event='closed',actor=dict(id=99,login='operator',type='User'))])
        self.github.calls.clear()
        self.assertTrue(self.actions.step(undo)['rejected'])
        self.assertEqual(self.github.calls,[])
        self.assertEqual(self.github.issue_value['state'],'closed')

    def test_undo_generation_and_actor_remain_pinned(self):
        request=self.request('close');self.actions.step(request);undo=self.actions.reopen('i',request)
        self.actions.actor_id='github:other'
        with self.assertRaises(IdempotencyConflict):self.actions.reopen('i',request)
        with self.store.write() as db:db.execute("UPDATE issues SET version=version+1 WHERE id='i'")
        self.github.calls.clear()
        self.assertTrue(self.actions.step(undo)['rejected'])
        self.assertEqual(self.github.calls,[])

    def test_close_effect_requires_controller_event_and_missing_event_stays_pending(self):
        request=self.request('close')
        original=self.github.pages
        self.github.pages=lambda role,item,path: [] if path.endswith('/events') else original(role,item,path)
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.github.pages=original
        self.github.events[-1]['actor']={'id':99,'login':'other','type':'User'}
        self.assertTrue(self.actions.step(request)['rejected'])

    def test_older_close_effect_without_lineage_is_rejected_without_adopting_remote_closure(self):
        request=self.request('close');row=self.actions._request(request)
        self.actions._intent(row,'close-issue',{'number':1,'state_reason':'not_planned'})
        self.github.issue_value.update(state='closed',state_reason='not_planned')
        self.assertTrue(self.actions.step(request)['rejected'])
        self.assertEqual(self.github.calls,[])

    def test_reopen_only_undoes_a_close(self):
        request=self.request('pause');self.actions.step(request)
        with self.assertRaises(TransitionError):self.actions.reopen('i',request)
    def test_imported_pending_request_never_executes_without_actor_authority(self):
        with self.store.write() as db:
            db.execute("INSERT INTO issue_action_requests(id,issue_id,action,expected_revision,status,reason,created_at) VALUES('old','i','pause','old','pending','old',1)")
        self.assertEqual(self.actions.cycle()['items'],[])
        self.assertEqual(self.github.calls,[])
    def test_observation_version_bump_does_not_discard_operator_intent(self):
        request=self.request('pause')
        with self.store.write() as db:db.execute("UPDATE units SET version=version+1 WHERE id='u'")
        self.assertTrue(self.actions.step(request)['applied'])
    def test_public_projection_is_bounded_metadata_and_detail_is_explicit(self):
        self.reserve()
        views=PipelineViews(self.store,runtime=validate({}),clock=lambda:100)
        page=views.issues()
        self.assertEqual(page['total'],1)
        self.assertNotIn('body',page['issues'][0])
        self.assertEqual(views.issues(key='i')['issue']['body'],'Body')
        self.assertNotIn('_jobs',page['issues'][0])

    def test_environment_override_and_request_completion_share_transaction(self):
        self.life.open_gate(self.command(),'environment',details={'reason':'database'})
        request=self.request('environment',environment={'mode':'standard','services':[]})
        before=self.view()['fencing_generation']
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertEqual(self.view()['fencing_generation'],before)
        with self.store.read_snapshot() as db:
            self.assertEqual(json.loads(db.execute("SELECT override_json FROM issue_environment_observations WHERE issue_id='i'").fetchone()[0]),{'mode':'standard','services':[]})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='operator_sync' AND resolved_at IS NULL").fetchone()[0],0)
        applied_version=self.view()['version']
        self.actions.step(request)
        self.assertEqual(self.view()['version'],applied_version)
        self.assertEqual(self.view()['fencing_generation'],before)

    def test_stop_job_immediately_fences_and_is_attributed_without_github_write(self):
        job=self.reserve();identifier=self.actions.stop_job(job,'Stop this run')
        self.assertEqual(self.view()['current_blocker']['code'],'operator_pause')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT actor_id FROM decisions WHERE request_id=?',(identifier,)).fetchone()[0],'github:1609016')
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?',(job,)).fetchone()[0],1)
        self.assertEqual(self.github.calls,[])

    def test_factory_sections_and_counts_do_not_read_artifacts(self):
        from unittest.mock import patch
        job=self.reserve()
        views=PipelineViews(self.store,runtime=validate({}),clock=lambda:100)
        with patch('forge.store.artifacts.Artifacts.read',side_effect=AssertionError('Metadata read touched artifacts')):
            board=views.factory(section='board')
            self.assertEqual(board['issues'][0]['key'],'i')
            self.assertEqual(board['jobs'][0]['id'],job)
            self.assertEqual(board['summary']['runner_count'],0)
            self.assertEqual(views.factory(section='runners')['jobs'],[])
            self.assertEqual(views.factory(section='completed',limit=10)['issues'],[])

    def test_status_filter_count_matches_derived_requirements_gate(self):
        with self.store.write() as db:
            original=db.execute("SELECT * FROM issue_revisions WHERE issue_id='i'").fetchone()
            db.execute('INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES(?,?,?,?,?,?,?,?)',
                ('new','i','Changed','New body','new-content','new-hash','"new"',100))
            db.execute("UPDATE issues SET current_revision_id='new',version=version+1 WHERE id='i'")
        views=PipelineViews(self.store,runtime=validate({}),clock=lambda:100)
        page=views.issues(state='attention')
        self.assertEqual(page['total'],1)
        self.assertEqual(page['issues'][0]['reason_code'],'membership_decision')
        self.assertEqual(views.issues(state='queued')['total'],0)

    def test_human_verification_http_adapter_binds_actor_issue_and_original_revision(self):
        from forge.factory.verification_controller import VerificationController
        from unittest.mock import Mock
        for name in ('capture','report','publish','build'):
            setattr(self,name,getattr(helpers.ResultControllerTests,name).__get__(self))
        self.controller.github=self.github
        self.build()
        gate=self.life.open_gate(self.command(),'verification_human',candidate_id=self.view()['current_candidate_id'],details={'what':'Run manual check','why':'Needs operator'})['gate_id']
        self.github.approvals=Mock(return_value=[])
        self.actions.verification=VerificationController(self.store,self.github,profiles=lambda repo:[],policy=lambda item:self.policy,clock=lambda:100)
        revision=self.revision();identifier=str(uuid.uuid4())
        receipt=self.actions.verify('i',revision,gate,'I ran the current candidate and it passed',request_id=identifier)
        self.assertIn('evidence_id',receipt)
        self.assertEqual(self.actions.verify('i',revision,gate,'I ran the current candidate and it passed',request_id=identifier),receipt)
        with self.assertRaises(IdempotencyConflict):self.actions.verify('i','different',gate,'I ran the current candidate and it passed',request_id=identifier)

    def test_typed_reviewer_exemption_requires_same_candidate_and_issue_assignment(self):
        from forge.factory.ownership import ownership
        issue={'number':1,'assignees':[{'login':'reviewer','type':'User'}],'labels':[]}
        item={'number':1,'unit_state':'merging','candidate':{'key':'candidate','pr':2},
            'reviewers_requested':{'candidate_id':'candidate','logins':['reviewer'],'issue_assignment':True}}
        self.assertIsNone(ownership(issue,item,'owner/repo'))
        item['reviewers_requested']['issue_assignment']=False
        self.assertIsNotNone(ownership(issue,item,'owner/repo'))
        item['reviewers_requested'].update(issue_assignment=True,candidate_id='old')
        self.assertIsNotNone(ownership(issue,item,'owner/repo'))

    def test_new_delivery_pins_observed_default_branch_before_admission(self):
        from unittest.mock import Mock
        from forge.store.queries import Queries
        self.life.close_unit(self.command(),close_code='operator_closed',evidence={'approved':True})
        self.github.target=Mock(return_value='develop')
        request=self.request('queue')
        unit=Queries(self.store).issue('i')['lifecycle']['unit_id']
        self.assertEqual(Queries(self.store).unit(unit)['base_branch'],'develop')
        self.assertEqual(self.github.calls,[])
        self.assertTrue(self.actions.step(request)['applied'])
        self.github.target.assert_called_once()

    def test_truncated_import_withholds_actions_until_full_observation(self):
        with self.store.write() as db:
            db.execute("UPDATE revision_presentation SET body_truncated=1 WHERE revision_id=(SELECT current_revision_id FROM issues WHERE id='i')")
        views=PipelineViews(self.store,runtime=validate({}),clock=lambda:100)
        for row in (views.issues()['issues'][0],views.issues(key='i')['issue']):
            self.assertTrue(row['body_truncated'])
            self.assertFalse(row['fresh'])
            self.assertFalse(row['closable'])
            self.assertEqual(row['actions'],[])
        with self.assertRaises(TransitionError):self.request('pause')
        self.assertEqual(self.github.calls,[])

    def test_runtime_callback_is_loaded_once_for_each_bounded_page(self):
        from unittest.mock import Mock
        for number in range(2,51):
            self.life.admit_issue(request_id='display-'+str(number),actor_id='test',unit_id='unit-'+str(number),issue_id='issue-'+str(number),repository_id=1,repository_name='owner/repo',issue_number=number,title='Issue',body='Body',requirements='requirements',observed_at=100)
        runtime=Mock(return_value=validate({}))
        views=PipelineViews(self.store,runtime=runtime,clock=lambda:100)
        for call in (lambda:views.issues(),lambda:views.factory(section='board'),lambda:views.issues(key='i')):
            runtime.reset_mock();call();runtime.assert_called_once_with()

    def test_public_projection_uses_one_connection_for_config_repositories_and_jobs(self):
        from unittest.mock import patch
        from forge.store.queries import Queries
        job = self.reserve()
        snapshots = []
        def runtime():
            with self.store.read_snapshot() as db:
                snapshots.append(db)
                db.execute('SELECT 1').fetchone()
            return validate({})
        def repositories(config):
            with self.store.read_snapshot() as db:
                snapshots.append(db)
                return [dict(id=r['id'], name=r['full_name'], enabled=bool(r['enabled']))
                        for r in db.execute('SELECT * FROM repositories')]
        views = PipelineViews(self.store, runtime=runtime, repositories=repositories, clock=lambda:100)
        for name, call, has_jobs in (
                ('board', lambda:views.factory(section='board'), True),
                ('detail', lambda:views.issues(key='i'), True),
                ('list', lambda:views.issues(), False)):
            snapshots.clear()
            with self.subTest(view=name), \
                    patch.object(self.store, '_connect_path', wraps=self.store._connect_path) as connect, \
                    patch.object(Queries, '_issue_contexts', wraps=Queries._issue_contexts) as issue_read:
                result = call()
                self.assertEqual(connect.call_count, 1)
                # Batched lists and singleton detail share the canonical read
                # in the configuration/repository snapshot.
                self.assertEqual(issue_read.call_count, 1)
                self.assertEqual(issue_read.call_args.args[1], ['i'])
                self.assertEqual(len(snapshots), 2)
                self.assertIs(snapshots[0], snapshots[1])
                self.assertIs(issue_read.call_args.args[0], snapshots[0])
                row = result['issue'] if name == 'detail' else result['issues'][0]
                self.assertEqual((row['key'], row['active']), ('i', job))
                if has_jobs:
                    self.assertEqual([item['id'] for item in result['jobs']], [job])


class OperatorEffectObservationReplayTests(unittest.TestCase):
    """Normal observation may consume our own write before its response is reconciled."""
    def setUp(self):
        from tempfile import TemporaryDirectory
        from forge.store.db import Store
        from forge.store.executions import Executions
        self.directory=TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.store=Store.create(self.directory.name);self.github=Remote();self.config=validate({})
        self.github.issue_value.update(title='Issue',updated_at='2026-10-10T00:00:01Z')
        admitted=Executions(self.store,clock=lambda:100).admit_issue(request_id='admit',actor_id='operator',unit_id='u',issue_id='1:1',
            repository_id=1,repository_name='owner/repo',issue_number=1,title='Issue',body='Body',requirements='requirements',observed_at=90)
        self.member=admitted['member_id'];self.actions=self.service()

    def service(self):
        return OperatorActions(self.store,self.github,actor_id='github:42',config=self.config,clock=lambda:100)

    def close(self):
        with self.store.read_snapshot() as db:revision=action_revision(db,'1:1')
        return self.actions.request('1:1','close',revision,'No longer needed')

    def observe_closed(self):
        from forge.factory.observation_controller import ObservationController
        result=ObservationController(self.store,self.github,admission=lambda _:True,clock=lambda:100).observe_member('u',self.member,'1:1')
        self.assertEqual(result['close_code'],'github_not_planned')

    def observe_reopened(self):
        from forge.factory.store_inventory import StoreInventory
        StoreInventory(self.store,None,self.github,config=self.config,clock=lambda:100).record(
            {'id':1,'name':'owner/repo'},deepcopy(self.github.issue_value),'requirements')

    def snapshot(self):
        with self.store.read_snapshot() as db:
            return dict(db.execute("SELECT * FROM units WHERE id='u'").fetchone()),dict(db.execute("SELECT * FROM issues WHERE id='1:1'").fetchone())

    def lose_response(self):
        if self.github.calls[-1][0]=='PATCH':
            if self.github.issue_value['state']=='open':
                self.github.issue_value.update(state_reason='reopened',updated_at='2026-10-10T00:00:02Z')
            raise GitHubUnavailable('lost PATCH reply')

    def test_observer_settles_our_close_before_reconstructed_service_acknowledges_it(self):
        request=self.close();self.github.after_write=self.lose_response
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.github.after_write=None;self.observe_closed();before=self.snapshot()
        self.github.calls.clear()
        restarted=self.service()
        self.assertTrue(restarted.step(request)['applied'])
        self.assertEqual(self.github.calls,[])
        self.assertEqual(self.snapshot(),before)
        with self.store.read_snapshot() as db:
            from forge.factory.store_operator import action_state_in
            state=action_state_in(db,request)
            self.assertEqual((state['status'],state['reversible']),('applied',True))
        self.assertEqual(restarted.step(request)['status'],'applied')
        self.assertEqual(self.github.calls,[])

    def test_inventory_observes_our_reopen_before_reconstructed_service_acknowledges_it(self):
        request=self.close();self.actions.step(request);undo=self.actions.reopen('1:1',request)
        self.github.after_write=self.lose_response
        self.assertEqual(self.actions.step(undo)['pending'],'github_observation')
        self.github.after_write=None;self.observe_reopened();before=self.snapshot()
        self.github.calls.clear()
        restarted=self.service()
        self.assertTrue(restarted.step(undo)['applied'])
        self.assertEqual(self.github.calls,[])
        self.assertEqual(self.snapshot(),before)
        with self.store.read_snapshot() as db:
            from forge.factory.store_operator import action_state_in
            state=action_state_in(db,request)
            self.assertEqual(state['undo']['status'],'applied')
            actor=db.execute('SELECT actor_id FROM operator_action_authority WHERE request_id=?',(undo,)).fetchone()[0]
            self.assertEqual(actor,'github:42')
        self.assertEqual(restarted.step(undo)['status'],'applied')

    def test_existing_effect_does_not_authorize_new_write_after_observed_fact_change(self):
        request=self.close();self.actions.step(request);undo=self.actions.reopen('1:1',request)
        row=self.actions._request(undo)
        self.actions._intent(row,'reopen-issue',{'closure_event':row['plan']['closure_event'],'number':1})
        self.github.issue_value.update(title='A changed issue',updated_at='2026-10-10T00:00:02Z')
        self.observe_reopened()  # Inventory sees changed facts, but issue remains closed.
        self.github.calls.clear()
        self.assertTrue(self.service().step(undo)['rejected'])
        self.assertEqual(self.github.calls,[])
        self.assertEqual(self.github.issue_value['state'],'closed')

    def test_observation_does_not_let_undo_claim_an_unrelated_reopen(self):
        request=self.close();self.actions.step(request);undo=self.actions.reopen('1:1',request)
        row=self.actions._request(undo)
        self.actions._intent(row,'reopen-issue',{'closure_event':row['plan']['closure_event'],'number':1})
        self.github.events.append(dict(id=2,event='reopened',actor=dict(id=99,login='another-operator',type='User')))
        self.github.issue_value.update(state='open',state_reason='reopened',updated_at='2026-10-10T00:00:02Z')
        self.observe_reopened();self.github.calls.clear()
        self.assertTrue(self.service().step(undo)['rejected'])
        self.assertEqual(self.github.calls,[])

    def test_close_receipt_preserves_new_facts_then_observation_settles_original_sync(self):
        from forge.store.models import Command
        from forge.store.transitions import Lifecycle
        request=self.close();self.github.after_write=self.lose_response
        self.assertEqual(self.actions.step(request)['pending'],'github_observation')
        self.github.after_write=None
        self.github.requirements='changed requirements'
        with self.store.read_snapshot() as db:version=db.execute("SELECT version FROM units WHERE id='u'").fetchone()[0]
        Lifecycle(self.store,clock=lambda:100).observe_issue(Command('observe-facts','supervisor:observer','u',version),'1:1',
            state='closed',state_reason='not_planned',title='Issue',body='Body',requirements=self.github.requirements,
            remote_updated_at='2026-10-10T00:00:02Z',generation=1)
        before=self.snapshot();self.github.calls.clear()
        self.assertTrue(self.service().step(request)['applied'])
        self.assertEqual(self.snapshot(),before)
        self.assertEqual(self.github.calls,[])
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM operator_observation_requests WHERE issue_id='1:1'").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='operator_sync' AND resolved_at IS NULL").fetchone()[0],1)
        self.github.issue_value['updated_at']='2026-10-10T00:00:02Z'
        self.observe_closed()
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='operator_sync' AND resolved_at IS NULL").fetchone()[0],0)
            self.assertIsNone(db.execute("SELECT 1 FROM operator_observation_requests WHERE issue_id='1:1'").fetchone())
            self.assertEqual(db.execute("SELECT status FROM issue_action_requests WHERE id=?",(request,)).fetchone()[0],'applied')
