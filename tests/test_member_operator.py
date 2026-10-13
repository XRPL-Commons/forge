"""Issue decisions preserve independent physical executions in an approved batch."""
from copy import deepcopy
import unittest

from forge.factory.config import validate
from forge.factory.store_operator import OperatorActions
from forge.factory.store_views import action_revision
from forge.store.models import TransitionError, VersionConflict
from tests import test_delivery_execution as delivery


class Remote:
    def __init__(self, count):
        self.issues={number:dict(number=number,state='open',labels=[],assignees=[]) for number in range(1,count+1)}
        self.calls=[]
        self.pull=None
    def issue(self,item):
        return deepcopy(self.issues[item['number']]),{'acceptance':['works']}
    def pr(self,item,number):
        return deepcopy(self.pull)
    def call(self,role,item,method,path,payload=None):
        self.calls.append((method,path,payload))
        issue=self.issues[item['number']]
        if method=='POST' and path.endswith('/labels'):
            issue['labels']=sorted(set(issue['labels'])|set(payload['labels']))
        elif method=='DELETE' and '/labels/' in path:
            from urllib.parse import unquote
            issue['labels'].remove(unquote(path.split('/labels/')[1]))
        elif method=='PATCH' and '/issues/' in path:issue.update(payload)
        else:raise AssertionError((method,path,payload))
        return deepcopy(issue)


class MemberOperatorTests(unittest.TestCase):
    setUp=delivery.DeliveryExecutionTests.setUp
    command=delivery.DeliveryExecutionTests.command
    compose=delivery.DeliveryExecutionTests.compose
    manifest=delivery.DeliveryExecutionTests.manifest
    clean=delivery.DeliveryExecutionTests.clean
    plan=delivery.DeliveryExecutionTests.plan
    child=delivery.DeliveryExecutionTests.child
    accept=delivery.DeliveryExecutionTests.accept

    def setup_actions(self,count=2,dependencies=()):
        self.compose(count,dependencies)
        self.remote=Remote(count)
        self.actions=OperatorActions(self.store,self.remote,actor_id='github:42',config=validate({}),clock=lambda:100)
        return self.plan()
    def request(self,issue,action):
        with self.store.read_snapshot() as db: revision=action_revision(db,issue)
        return self.actions.request(issue,action,revision,'Operator decision')
    def jobs(self):
        with self.store.read_snapshot() as db:
            return {r['id']:dict(r) for r in db.execute("SELECT * FROM jobs WHERE unit_id='batch'")}

    def test_pause_one_child_preserves_independent_completion_and_budget(self):
        _,plan,_=self.setup_actions()
        first,one=self.child(plan,'0'); second,two=self.child(plan,'1')
        request=self.request('issue-0','pause')
        jobs=self.jobs()
        self.assertEqual(jobs[first['job_id']]['fenced'],1)
        self.assertEqual(jobs[second['job_id']]['fenced'],0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],2)
        self.accept(second,two,'1')
        self.assertTrue(self.actions.step(request)['applied'])
        with self.store.read_snapshot() as db:
            gates=[dict(r) for r in db.execute("SELECT * FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL")]
            self.assertEqual([g['member_id'] for g in gates],[self.members['issue-0']])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def test_pause_predecessor_fences_dependent_but_keeps_independent_child(self):
        _,plan,_=self.setup_actions(3,[{'issue_id':'issue-1','prerequisite_issue_id':'issue-0'}])
        first,one=self.child(plan,'0');self.accept(first,one,'0')
        dependent,_=self.child(plan,'1');independent,three=self.child(plan,'2')
        request=self.request('issue-0','pause')
        jobs=self.jobs()
        self.assertEqual(jobs[dependent['job_id']]['fenced'],1)
        self.assertEqual(jobs[independent['job_id']]['fenced'],0)
        self.accept(independent,three,'2')
        self.assertTrue(self.actions.step(request)['applied'])

    def test_resume_waits_only_for_affected_cleanup_and_keeps_other_pause(self):
        _,plan,_=self.setup_actions()
        first,one=self.child(plan,'0'); second,two=self.child(plan,'1')
        pause=self.request('issue-0','pause'); self.actions.step(pause)
        self.actions.stop_job(second['job_id'],'Pause other child')
        self.clean(first,'fence-0')
        resume=self.request('issue-0','resume')
        self.assertTrue(self.actions.step(resume)['applied'])
        with self.store.read_snapshot() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT member_id FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL")],[self.members['issue-1']])
            self.assertEqual(db.execute('SELECT cleanup FROM jobs WHERE id=?',(second['job_id'],)).fetchone()[0],0)

    def test_concurrent_member_intents_complete_independently(self):
        self.setup_actions()
        first=self.request('issue-0','pause'); second=self.request('issue-1','backlog')
        self.assertTrue(self.actions.step(first)['applied'])
        self.assertTrue(self.actions.step(second)['applied'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='operator_sync' AND resolved_at IS NULL").fetchone()[0],0)

    def test_child_reset_is_scoped_and_retains_cleanup_lease(self):
        _,plan,_=self.setup_actions()
        first,_=self.child(plan,'0'); second,_=self.child(plan,'1')
        self.actions.stop_job(first['job_id'],'Reset child',action='reset')
        jobs=self.jobs()
        self.assertEqual(jobs[first['job_id']]['fenced'],1)
        self.assertEqual(jobs[second['job_id']]['fenced'],0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def test_ordinary_close_cannot_close_a_grouped_delivery(self):
        self.setup_actions()
        with self.assertRaisesRegex(TransitionError,'explicit member removal'):
            self.request('issue-0','close')
        self.assertEqual(self.remote.calls,[])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_issues WHERE unit_id='batch' AND left_at IS NULL").fetchone()[0],2)

    def membership_request(self,issue,action):
        preview=self.actions.membership(issue)
        return self.actions.request(issue,action,preview['revision'],'Explicit batch decision',membership=preview['delivery']['decision'])

    def test_drop_waits_cleanup_then_releases_one_issue_without_budget_reset(self):
        _,plan,_=self.setup_actions()
        first,_=self.child(plan,'0'); second,_=self.child(plan,'1')
        request=self.membership_request('issue-0','drop')
        self.assertEqual(self.actions.step(request)['pending'],'cleanup')
        self.assertTrue(all(job['fenced'] for job in self.jobs().values()))
        self.clean(first,'fence-0');self.clean(second,'fence-1')
        result=self.actions.step(request)
        self.assertTrue(result['applied'])
        self.assertEqual(result['released']['issue_id'],'issue-0')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT budget_generation FROM units WHERE id='batch'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_issues WHERE unit_id='batch' AND left_at IS NULL").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL").fetchone()[0],0)
        self.assertEqual(self.remote.calls,[])

    def test_close_member_keeps_shared_delivery_and_does_not_close_pr(self):
        self.setup_actions()
        request=self.membership_request('issue-0','close_member')
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertEqual(self.remote.issues[1]['state'],'closed')
        self.assertEqual(self.remote.issues[2]['state'],'open')
        self.assertEqual([path for _,path,_ in self.remote.calls],['/issues/1'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='batch'").fetchone()[0],'building')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_issues WHERE issue_id='issue-0' AND left_at IS NULL").fetchone()[0],0)

    def test_dissolve_keeps_only_each_members_own_pause(self):
        self.setup_actions()
        self.actions.step(self.request('issue-0','pause'))
        request=self.membership_request('issue-1','dissolve')
        result=self.actions.step(request)
        self.assertTrue(result['applied'])
        with self.store.read_snapshot() as db:
            for member in result['released']:
                count=db.execute("SELECT COUNT(*) FROM unit_gates WHERE unit_id=? AND code='operator_pause' AND resolved_at IS NULL",(member['unit_id'],)).fetchone()[0]
                self.assertEqual(count,1 if member['issue_id']=='issue-0' else 0)

    def test_membership_requires_exact_explicit_dependent_choices(self):
        self.setup_actions(2,[{'issue_id':'issue-1','prerequisite_issue_id':'issue-0'}])
        preview=self.actions.membership('issue-0')
        decision=deepcopy(preview['delivery']['decision']);decision['dependent_decisions']={}
        with self.assertRaisesRegex(VersionConflict,'dependent decisions changed'):
            self.actions.request('issue-0','drop',preview['revision'],'Remove',membership=decision)
        self.assertEqual(self.remote.calls,[])

    def test_confirmed_external_closure_removes_member_without_another_remote_write(self):
        self.setup_actions()
        self.remote.issues[1].update(state='closed',state_reason='completed')
        with self.store.write() as db:
            db.execute("UPDATE issues SET github_state='closed',state_reason='completed' WHERE id='issue-0'")
        request=self.membership_request('issue-0','close_member')
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertEqual(self.remote.calls,[])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT state_reason FROM issues WHERE id='issue-0'").fetchone()[0],'completed')

    def test_close_last_survivor_settles_only_after_explicit_membership_decision(self):
        self.setup_actions()
        self.actions.step(self.membership_request('issue-0','close_member'))
        self.assertTrue(self.actions.step(self.membership_request('issue-1','close_member'))['applied'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='batch'").fetchone()[0],'closed')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_issues WHERE unit_id='batch' AND left_at IS NULL").fetchone()[0],0)

    def test_membership_intent_blocks_concurrent_member_decisions_until_cleanup(self):
        _,plan,_=self.setup_actions()
        self.child(plan,'0')
        self.membership_request('issue-0','drop')
        with self.assertRaisesRegex(TransitionError,'pending membership'):
            self.request('issue-1','pause')

    def test_explicit_delivery_resume_preserves_member_pause_and_shared_budget(self):
        self.setup_actions()
        self.actions.step(self.request('issue-0','pause'))
        self.life.pause(self.command())
        request=self.membership_request('issue-1','resume_delivery')
        self.assertTrue(self.actions.step(request)['resumed'])
        with self.store.read_snapshot() as db:
            gates=[dict(row) for row in db.execute("SELECT * FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL")]
            self.assertEqual([gate['member_id'] for gate in gates],[self.members['issue-0']])
            self.assertEqual(db.execute("SELECT budget_generation FROM units WHERE id='batch'").fetchone()[0],1)
        self.assertIn('hold',self.remote.issues[1]['labels'])

    def test_delivery_resume_without_unit_pause_has_no_effects(self):
        self.setup_actions()
        with self.assertRaisesRegex(TransitionError,'no unit-wide operator pause'):
            self.membership_request('issue-0','resume_delivery')
        self.assertEqual(self.remote.calls,[])

    def test_group_reload_retains_exact_continuation_and_another_members_environment_hold(self):
        from forge.store.models import Candidate
        from forge.store.executions import Launch
        from forge.store.transitions import manifest_hash,project_unit
        from forge.store.delivery_inputs import previous_publication_in
        from forge.factory.ownership import publication_context,owned_publication_matches
        _,plan,_=self.setup_actions()
        for key in ('0','1'):
            child,manifest=self.child(plan,key);self.accept(child,manifest,key)
        manifest=self.manifest('integration')
        owner=self.delivery.reserve_builder(self.command(),stage='integration',slot='owner',execution_fence='integration',
            input_manifest=manifest,launch=Launch('forge/batch','builder[bot]','codex',200,{},{}))
        candidate=Candidate(7,'forge/batch','c'*40,'base','policy',manifest['requirements_hash'],manifest_hash(manifest))
        self.delivery.publish_candidate(self.command(),owner['job_id'],execution_fence='integration',input_manifest=manifest,
            candidate=candidate,evidence={'verified':True,'pull_number':7,'head_oid':candidate.head_oid,'base_oid':'base'})
        self.clean(owner,'integration')
        self.life.open_gate(self.command(),'environment',member_id=self.members['issue-1'],details={'reason':'database'})
        self.life.open_gate(self.command(),'external_change',details={'reason':'candidate_head_changed'})
        self.remote.pull={'number':7,'state':'open','draft':False,'merged':False,
            'user':{'id':42,'login':'builder[bot]','type':'Bot'},
            'head':{'sha':'d'*40,'ref':'forge/batch','repo':{'id':1}},
            'base':{'sha':'b'*40,'ref':'main','repo':{'id':1}}}
        self.assertTrue(self.actions.step(self.request('issue-0','recheck'))['applied'])
        with self.store.read_snapshot() as db:
            previous=previous_publication_in(db,'batch')
            self.assertEqual(previous['source_job_id'],owner['job_id'])
            self.assertEqual(previous['head_oid'],'d'*40)
            self.assertIsNone(project_unit(db,'batch')['current_candidate_id'])
            self.assertEqual(project_unit(db,'batch')['reason_code'],'environment')
            self.assertEqual([r[0] for r in db.execute("SELECT member_id FROM unit_gates WHERE code='environment' AND resolved_at IS NULL")],[self.members['issue-1']])
        self.assertEqual(self.manifest('planning')['previous_publication'],previous)
        context=publication_context(self.store,'batch',{})
        self.assertTrue(owned_publication_matches(self.remote.pull,context))
        changed=deepcopy(self.remote.pull);changed['user']['id']=99
        self.assertFalse(owned_publication_matches(changed,context))
        self.assertEqual(self.query.tree('batch')['charged_failures'],0)

    def test_member_closure_verifies_candidate_and_keeps_shared_pr_open(self):
        from forge.store.models import Candidate
        from forge.store.transitions import manifest_hash
        _,plan,_=self.setup_actions()
        for key in ('0','1'):
            child,manifest=self.child(plan,key);self.accept(child,manifest,key)
        manifest=self.manifest('integration')
        owner=self.delivery.reserve_builder(self.command(),stage='integration',slot='owner',execution_fence='integration',input_manifest=manifest)
        candidate=Candidate(7,'forge/batch','c'*40,'base','policy',manifest['requirements_hash'],manifest_hash(manifest))
        self.delivery.publish_candidate(self.command(),owner['job_id'],execution_fence='integration',input_manifest=manifest,candidate=candidate,evidence={'verified':True,'pull_number':7,'head_oid':candidate.head_oid,'base_oid':'base'})
        self.clean(owner,'integration')
        self.remote.pull={'number':7,'state':'open','merged':False,'head':{'sha':'c'*40},'base':{'sha':'base'}}
        request=self.membership_request('issue-0','close_member')
        self.assertTrue(self.actions.step(request)['applied'])
        self.assertEqual(self.remote.pull['state'],'open')
        self.assertEqual([path for _,path,_ in self.remote.calls],['/issues/1'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidates WHERE unit_id='batch'").fetchone()[0],1)
            self.assertIsNone(db.execute("SELECT current_candidate_id FROM units WHERE id='batch'").fetchone()[0])

    def test_dissolve_consolidates_unit_and_member_pause_without_widening_other_intent(self):
        self.setup_actions()
        self.actions.step(self.request('issue-0','pause'))
        self.life.pause(self.command())
        result=self.actions.step(self.membership_request('issue-1','dissolve'))
        self.assertTrue(result['applied'])
        with self.store.read_snapshot() as db:
            for item in result['released']:
                gate=db.execute("SELECT details_json FROM unit_gates WHERE unit_id=? AND code='operator_pause' AND resolved_at IS NULL",(item['unit_id'],)).fetchone()
                import json
                self.assertEqual(len(json.loads(gate[0])['source_gate_ids']),2 if item['issue_id']=='issue-0' else 1)

    def test_dissolve_preserves_approved_graph_as_external_prerequisites(self):
        self.setup_actions(3,[{'issue_id':'issue-1','prerequisite_issue_id':'issue-0'},{'issue_id':'issue-2','prerequisite_issue_id':'issue-1'}])
        result=self.actions.step(self.membership_request('issue-0','dissolve'))
        self.assertTrue(result['applied'])
        with self.store.read_snapshot() as db:
            edges={(row['issue_id'],row['prerequisite_issue_id']) for row in db.execute('SELECT * FROM issue_dependencies')}
            self.assertEqual(edges,{('issue-1','issue-0'),('issue-2','issue-1')})
            for item in result['released']:
                self.assertEqual(db.execute('SELECT budget_generation FROM units WHERE id=?',(item['unit_id'],)).fetchone()[0],1)

    def task_plan(self, tasks):
        from tests.test_task_topology import TaskTopologyTests
        self.fixture=self
        return TaskTopologyTests.plan(self,tasks)

    def task(self, plan, key):
        from tests.test_task_topology import TaskTopologyTests
        return TaskTopologyTests.reserve(self,plan,key)

    def task_output(self, job, manifest, key):
        from tests.test_task_topology import TaskTopologyTests
        return TaskTopologyTests.accept(self,job,manifest,key)

    def test_stop_joint_task_pauses_exact_members_and_preserves_independent_task(self):
        self.setup_actions(3)
        a,b,c=self.members.values()
        plan=self.task_plan([('joint','owner',[a,b],[],[]),('independent','child',[c],[],[])])
        joint,_=self.task(plan,'joint'); independent,_=self.task(plan,'independent')
        self.actions.stop_job(joint['job_id'],'Pause joint task')
        jobs=self.jobs()
        self.assertEqual(jobs[joint['job_id']]['fenced'],1)
        self.assertEqual(jobs[independent['job_id']]['fenced'],0)
        with self.store.read_snapshot() as db:
            self.assertEqual({row[0] for row in db.execute("SELECT member_id FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL")},{a,b})
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_resume_waits_for_task_only_descendant_cleanup_not_independent_worker(self):
        self.setup_actions(3)
        a,b,c=self.members.values()
        plan=self.task_plan([('source','child',[a],[],[]),('dependent','child',[b],['source'],[]),('independent','child',[c],[],[])])
        source,inputs=self.task(plan,'source'); self.task_output(source,inputs,'source')
        dependent,_=self.task(plan,'dependent'); independent,_=self.task(plan,'independent')
        self.actions.step(self.request('issue-0','pause'))
        with self.assertRaisesRegex(TransitionError,'not available'):
            self.request('issue-0','resume')
        self.assertEqual(self.jobs()[dependent['job_id']]['fenced'],1)
        self.assertEqual(self.jobs()[independent['job_id']]['fenced'],0)
        self.clean(dependent,'dependent')
        resume=self.request('issue-0','resume')
        self.assertTrue(self.actions.step(resume)['applied'])
        self.assertEqual(self.jobs()[independent['job_id']]['cleanup'],0)

    def test_stop_planning_run_retains_whole_delivery_pause(self):
        self.compose(2)
        self.remote=Remote(2)
        self.actions=OperatorActions(self.store,self.remote,actor_id='github:42',config=validate({}),clock=lambda:100)
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='owner',input_manifest=self.manifest())
        self.actions.stop_job(parent['job_id'],'Pause delivery planning')
        with self.store.read_snapshot() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT member_id FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL")],[None])
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?',(parent['job_id'],)).fetchone()[0],1)

    def test_triage_effect_waits_exact_task_descendants_then_ignores_independent_cleanup(self):
        from forge.store.repository_planning import RepositoryPlanning
        from forge.factory.triage_effects import TriageEffects
        self.setup_actions(3)
        a,b,c=self.members.values()
        plan=self.task_plan([('source','child',[a],[],[]),('dependent','child',[b],['source'],[]),('independent','child',[c],[],[])])
        source,inputs=self.task(plan,'source'); self.task_output(source,inputs,'source')
        dependent,_=self.task(plan,'dependent'); independent,_=self.task(plan,'independent')
        planning=RepositoryPlanning(self.store,clock=lambda:100)
        planning.begin(1,proposal_id='triage',base_branch='main',request_id='begin-triage',actor_id='planner')
        rows=[dict(action='park' if n==0 else 'keep',members=['issue-'+str(n)],reason='Operator should pause this scope' if n==0 else 'Continue independently',rule_quote=None,parameters={'add':[]} if n==0 else {}) for n in range(3)]
        planning.publish('triage',expected_version=1,rows=rows,rationale='Review pause request',request_id='publish-triage',actor_id='planner')
        rows=planning.proposal('triage')['rows']
        accepted=planning.accept('triage',expected_version=2,accept_rows=[row['id'] for row in rows],reject_rows=[],request_id='accept-triage',actor_id='operator',config=validate({}))
        effect=accepted['effects'][0]
        effects=TriageEffects(self.store,self.remote,config=validate({}),clock=lambda:100)
        self.assertEqual(effects.step(effect)['reason'],'cleanup')
        self.assertEqual(self.remote.calls,[])
        self.assertEqual(self.jobs()[dependent['job_id']]['fenced'],1)
        self.assertEqual(self.jobs()[independent['job_id']]['fenced'],0)
        self.clean(dependent,'dependent')
        self.assertEqual(effects.step(effect)['status'],'confirmed')
        self.assertTrue(self.remote.calls)
        self.assertEqual(self.jobs()[independent['job_id']]['cleanup'],0)
