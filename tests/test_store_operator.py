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
        self.calls=[];self.after_write=None;self.comments=[]
    def pages(self,role,item,path):
        return deepcopy(self.comments)
    def call(self,role,item,method,path,payload=None):
        self.calls.append((method,path,payload))
        if method=='POST' and path.endswith('/labels'):
            self.issue_value['labels']=list(set(self.issue_value.get('labels',[]))|set(payload['labels']))
        elif method=='DELETE' and '/labels/' in path:
            from urllib.parse import unquote
            label=unquote(path.split('/labels/')[1]);self.issue_value['labels']=[v for v in self.issue_value['labels'] if v!=label]
        elif method=='PATCH' and '/issues/' in path:self.issue_value.update(payload)
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
        with self.assertRaises(TransitionError):self.actions.reopen('i',request)  # Still closing: nothing to undo yet.
        self.actions.step(request)
        self.assertEqual(self.actions.reopen('i',request),{'request_id':request,'reopened':True})
        self.assertEqual(self.github.issue_value['state'],'open')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT actor_id FROM operator_observation_requests WHERE issue_id=?',('i',)).fetchone()[0],'github:1609016')
        self.github.calls.clear()
        self.actions.reopen('i',request)  # Already open: no second write.
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
                    patch.object(Queries, '_issue_context', wraps=Queries._issue_context) as issue_read:
                result = call()
                self.assertEqual(connect.call_count, 1)
                # Both the narrow Activity path and rich issue views use this
                # canonical read in the configuration/repository snapshot.
                self.assertEqual(issue_read.call_count, 1)
                self.assertEqual(len(snapshots), 2)
                self.assertIs(snapshots[0], snapshots[1])
                self.assertIs(issue_read.call_args.args[0], snapshots[0])
                row = result['issue'] if name == 'detail' else result['issues'][0]
                self.assertEqual((row['key'], row['active']), ('i', job))
                if has_jobs:
                    self.assertEqual([item['id'] for item in result['jobs']], [job])
