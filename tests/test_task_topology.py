"""Independent tasks retain exact issue scope, joins and physical execution gates."""
import hashlib
import json
import tempfile
import unittest

from forge.store.db import Store
from forge.store.delivery import Delivery,DeliveryQueries
from forge.store.models import Command,InvalidDelegationPlan,TransitionError
from forge.store.transitions import Lifecycle,project_unit,manifest_hash


class TaskTopologyTests(unittest.TestCase):
    def setUp(self):
        from tests.test_delivery_execution import DeliveryExecutionTests
        fixture=DeliveryExecutionTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        self.fixture=fixture; self.store=fixture.store; self.life=fixture.life
        self.delivery=fixture.delivery; self.query=fixture.query; self.command=fixture.command
        fixture.compose(); self.members=list(fixture.members.values())

    def plan(self,tasks,final=(),internalizes=()):
        manifest=self.query.builder_manifest('batch','planning',base_oid='base',policy_hash='policy')
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='parent',input_manifest=manifest)
        rows=[{'key':key,'executor':executor,'member_ids':members,'scope':'Implement '+key,'depends_on':dependencies,'internalizes':edges}
              for key,executor,members,dependencies,edges in tasks]
        saved=self.delivery.save_plan(self.command(),parent['job_id'],execution_fence='parent',input_manifest=manifest,
            plan={'version':2,'retained_task_ids':[],'tasks':rows,'owner_final':{'member_ids':list(final),'scope':'Finish owner work','internalizes':list(internalizes)}},evidence={'verified':True})
        self.fixture.clean(parent,'parent')
        return saved

    def reserve(self,plan,key):
        identity=plan['task_ids'][key]; manifest=self.query.task_manifest(plan['plan_id'],identity)
        from forge.store.executions import Launch
        job=self.delivery.reserve_task(self.command(),plan['plan_id'],identity,slot=key,execution_fence=key,input_manifest=manifest,worktree_key=key,launch=Launch('forge/work','builder[bot]','codex',1000,{},{}))
        return job,manifest

    def accept(self,job,manifest,key):
        from forge.store.artifacts import Artifacts
        bundle=b'retained verified test bundle'; digest=hashlib.sha256(bundle).hexdigest(); commit=hashlib.sha1(key.encode()).hexdigest()
        from forge.store.executions import Executions
        Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence=key,capture={'logs':'','worktree_bundle':bundle})
        self.fixture.clean(job,key)
        result=self.delivery.accept_task_output(self.command(),job['job_id'],execution_fence=key,input_manifest=manifest,commit_oid=commit,
            evidence={'verified':True,'commit_oid':commit,'base_oid':'base','input_manifest_hash':manifest_hash(manifest),'bundle_digest':digest})
        return result

    def test_two_tasks_same_issue_and_shared_task_join_exact_outputs(self):
        a,b=self.members
        plan=self.plan([('a1','child',[a],[],[]),('a2','child',[a],[],[]),('shared','child',[a,b],['a1','a2'],[])])
        one,m1=self.reserve(plan,'a1'); two,m2=self.reserve(plan,'a2')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],2)
            self.assertEqual([row[0] for row in db.execute('SELECT member_id FROM job_members WHERE job_id=?',(one['job_id'],))],[a])
        with self.assertRaises(TransitionError): self.reserve(plan,'shared')
        o1=self.accept(one,m1,'a1');o2=self.accept(two,m2,'a2')
        shared,manifest=self.reserve(plan,'shared')
        self.assertEqual({ref['output_id'] for ref in manifest['prerequisite_outputs']},{o1['output_id'],o2['output_id']})
        self.assertEqual({pin['member_id'] for pin in manifest['members']},{a,b})
        self.accept(shared,manifest,'shared')
        tree=self.query.tree('batch'); self.assertTrue(tree['contributions'][a]['ready']);self.assertTrue(tree['contributions'][b]['ready'])
        self.assertEqual(len(tree['contributions'][a]['outputs']),3)
        integration=self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']
        self.assertEqual(integration['stage'],'integration');self.assertEqual(len(integration['input_manifest']['part_outputs']),3)

    def test_zero_delegation_and_owner_task_before_child(self):
        a,b=self.members
        plan=self.plan([('owner','owner',[a],[],[]),('child','child',[b],['owner'],[])])
        ready=self.query.assignments('batch',base_oid='base',policy_hash='policy')['items']
        self.assertEqual([item['executor'] for item in ready],['owner'])
        job,manifest=self.reserve(plan,'owner');self.accept(job,manifest,'owner')
        child,manifest=self.reserve(plan,'child');self.accept(child,manifest,'child')
        self.assertEqual(self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']['stage'],'integration')

    def test_empty_task_graph_runs_owner_final(self):
        self.plan([],self.members)
        assignment=self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']
        self.assertEqual(assignment['stage'],'integration');self.assertEqual(assignment['input_manifest']['part_outputs'],[])
        tree=self.query.tree('batch');self.assertTrue(all(row['owner_final_pending'] for row in tree['contributions'].values()))

    def test_member_fence_reaches_intersecting_task_and_task_descendants_only(self):
        a,b=self.members
        plan=self.plan([('a','child',[a],[],[]),('b','child',[b],[],[])])
        one,m1=self.reserve(plan,'a');two,m2=self.reserve(plan,'b')
        with self.store.write() as db:self.life.fence_member_in(db,self.command(),a,reason='hold')
        with self.store.read_snapshot() as db:
            jobs=dict(db.execute('SELECT id,fenced FROM jobs WHERE id IN (?,?)',(one['job_id'],two['job_id'])))
            self.assertEqual(jobs,{one['job_id']:1,two['job_id']:0})
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],2)
        self.assertTrue(self.accept(two,m2,'b')['accepted'])

    def test_owner_final_receipt_is_bound_to_exact_candidate(self):
        from forge.store.models import Candidate
        self.plan([],self.members)
        manifest=self.query.builder_manifest('batch','integration',base_oid='base',policy_hash='policy')
        job=self.delivery.reserve_builder(self.command(),stage='integration',slot='owner',execution_fence='integrate',input_manifest=manifest)
        candidate=Candidate(8,'forge/work','c'*40,'base','policy',manifest['requirements_hash'],manifest_hash(manifest))
        self.delivery.publish_candidate(self.command(),job['job_id'],execution_fence='integrate',input_manifest=manifest,candidate=candidate,
            evidence={'verified':True,'pull_number':8,'head_oid':candidate.head_oid,'base_oid':'base'})
        tree=self.query.tree('batch')
        self.assertTrue(all(row['ready'] and not row['owner_final_pending'] and row['candidate_id'] for row in tree['contributions'].values()))

    def test_changed_other_member_does_not_revoke_independent_captured_task(self):
        from forge.store.executions import Executions
        a,b=self.members
        plan=self.plan([('a','child',[a],[],[]),('b','child',[b],[],[])])
        job,manifest=self.reserve(plan,'b')
        # A real immutable revision change with the same membership generation.
        with self.store.write() as db:
            old=dict(db.execute('SELECT r.* FROM issue_revisions r JOIN unit_issues m ON m.requirements_revision_id=r.id WHERE m.id=?',(a,)).fetchone())
            db.execute('INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES(?,?,?,?,?,?,?,?)',
                       ('changed',old['issue_id'],'Changed','changed','changed','changed',old['requirements_json'],101))
            db.execute('UPDATE unit_issues SET requirements_revision_id=? WHERE id=?',('changed',a))
        running=next(task for task in self.query.tree('batch')['tasks'] if task['key']=='b')
        self.assertEqual(running['status'],'working');self.assertIsNone(running['pending_reason'])
        self.assertTrue(self.accept(job,manifest,'b')['accepted'])
        tree=self.query.tree('batch');self.assertTrue(tree['contributions'][b]['ready'])
        self.assertFalse(tree['contributions'][a]['ready'])
        self.assertEqual(self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']['stage'],'planning')

    def test_scoped_cleanup_is_independent_for_two_tasks_on_same_issue(self):
        from forge.store.executions import Executions
        a,b=self.members
        plan=self.plan([('one','child',[a,b],[],[]),('two','child',[a],[],[])])
        first,m1=self.reserve(plan,'one');second,m2=self.reserve(plan,'two')
        executions=Executions(self.store,clock=lambda:100)
        for job,key in [(first,'one'),(second,'two')]:
            executions.collect(self.command(),job['job_id'],execution_fence=key,capture={'logs':'','worktree_bundle':b'bundle'})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='cleanup_pending' AND resolved_at IS NULL").fetchone()[0],2)
        self.fixture.clean(first,'one')
        with self.store.read_snapshot() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT job_id FROM unit_gates WHERE code='cleanup_pending' AND resolved_at IS NULL")],[second['job_id']])
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],1)

    def test_joint_task_requires_explicit_internalization_and_output_join(self):
        a,b=self.members
        with self.store.write() as db: db.execute('INSERT INTO member_dependencies VALUES(?,?,?,1)',('batch',b,a))
        edge={'prerequisite_member_id':a,'member_id':b}
        with self.assertRaises(InvalidDelegationPlan): self.plan([('joint','child',[a,b],[],[])])
        # The failed store transaction did not consume the planning result.
        with self.store.read_snapshot() as db:
            job=dict(db.execute("SELECT j.id,a.input_manifest_json FROM jobs j JOIN attempts a ON a.id=j.attempt_id WHERE j.unit_id='batch' AND j.outcome IS NULL").fetchone())
        manifest=json.loads(job['input_manifest_json'])
        saved=self.delivery.save_plan(self.command(),job['id'],execution_fence='parent',input_manifest=manifest,
            plan={'version':2,'retained_task_ids':[],'tasks':[{'key':'joint','executor':'child','member_ids':[a,b],'scope':'Joint change','depends_on':[],'internalizes':[edge]}],
                  'owner_final':{'member_ids':[],'scope':'','internalizes':[]}},evidence={'verified':True})
        self.fixture.clean({'job_id':job['id']},'parent')
        child,manifest=self.reserve(saved,'joint');self.accept(child,manifest,'joint')
        self.assertEqual(self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']['stage'],'integration')

    def test_child_cannot_depend_on_owner_work_delayed_until_integration(self):
        a,b=self.members
        with self.store.write() as db: db.execute('INSERT INTO member_dependencies VALUES(?,?,?,1)',('batch',b,a))
        with self.assertRaises(InvalidDelegationPlan): self.plan([('child','child',[b],[],[])],final=[a])

    def test_task_page_keeps_full_contributions_and_rejects_changed_plan(self):
        from forge.store.models import DeliveryPlanChanged
        a,b=self.members
        self.plan([('one','child',[a],[],[]),('two','child',[a,b],[],[])])
        page=self.query.tree('batch',task_limit=1)
        self.assertFalse(page['tasks_complete']);self.assertEqual(len(page['contributions'][a]['task_ids']),2)
        next_page=self.query.tree('batch',task_after=page['task_next_cursor'],task_limit=1)
        self.assertTrue(next_page['tasks_complete'])
        self.plan([],self.members)
        with self.assertRaises(DeliveryPlanChanged): self.query.tree('batch',task_after=page['task_next_cursor'])

    def test_replanned_task_outputs_are_bound_to_new_base_and_policy(self):
        a,b=self.members
        plan=self.plan([('shared','child',[a,b],[],[])])
        job,manifest=self.reserve(plan,'shared');self.accept(job,manifest,'shared')
        changed=self.query.builder_manifest('batch','planning',base_oid='new-base',policy_hash='new-policy')
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='new',input_manifest=changed)
        self.delivery.save_plan(self.command(),parent['job_id'],execution_fence='new',input_manifest=changed,
            plan={'version':2,'retained_task_ids':[plan['task_ids']['shared']],'tasks':[],
                  'owner_final':{'member_ids':[],'scope':'','internalizes':[]}},evidence={'verified':True})
        self.fixture.clean(parent,'new')
        from forge.store.task_inputs import member_contributions_in
        with self.store.read_snapshot() as db:
            unit=dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
            contribution=member_contributions_in(db,unit,a)
            self.assertFalse(contribution['ready']);self.assertEqual(contribution['outputs'],[])
        assignment=self.query.next_assignment('batch',base_oid='new-base',policy_hash='new-policy')['assignment']
        self.assertEqual(assignment['stage'],'task');self.assertEqual(assignment['input_manifest']['base_oid'],'new-base')

    def set_choices(self,choices):
        with self.store.write() as db:
            for revision,(member,choice) in enumerate(choices.items(),1):
                pin=db.execute('SELECT requirements_revision_id FROM unit_issues WHERE id=?',(member,)).fetchone()[0]
                db.execute('INSERT INTO unit_environment(unit_id,revision,choice,requirements_revision_id,config_hash,created_at,member_id) VALUES(?,?,?,?,?,?,?)',
                    ('batch',revision,json.dumps(choice),pin,'config',100,member))

    def test_environment_conflict_is_uncharged_and_allows_one_meaningful_replan(self):
        a,b=self.members
        plan=self.plan([('shared','child',[a,b],[],[])])
        choices={a:{'tier':'standard','automatic':False,'services':[]},b:{'tier':'full','automatic':True,'services':['postgres']}}
        self.set_choices(choices)
        command=self.command();args=dict(plan_id=plan['plan_id'],task_id=plan['task_ids']['shared'],choices=choices)
        receipt=self.delivery.environment_conflict(command,**args)
        self.assertEqual(receipt,self.delivery.environment_conflict(command,**args));self.assertTrue(receipt['replan_allowed'])
        assignment=self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']
        self.assertEqual(assignment['stage'],'planning');self.assertEqual(len(assignment['input_manifest']['environment_conflicts']),1)
        self.plan([('same-conflict','child',[a,b],[],[])])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL").fetchone()[0],1)
        self.assertIsNone(self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment'])

    def test_integration_environment_conflict_requires_operator_without_execution(self):
        a,b=self.members
        plan=self.plan([],self.members)
        choices={a:{'tier':'standard','automatic':False,'services':[]},b:{'tier':'full','automatic':True,'services':['postgres']}}
        self.set_choices(choices)
        receipt=self.delivery.environment_conflict(self.command(),plan_id=plan['plan_id'],task_id=None,choices=choices)
        self.assertFalse(receipt['replan_allowed']);self.assertIsNotNone(receipt['gate_id'])
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],0)

    def test_task_publication_is_never_singleton_finish_authority(self):
        from forge.store.models import Candidate
        plan=self.plan([('shared','child',self.members,[],[])])
        job,manifest=self.reserve(plan,'shared')
        with self.assertRaises(TransitionError):
            self.life.finish_job(self.command(),job['job_id'],execution_fence='shared',input_manifest=manifest,
                candidate=Candidate(5,'forge/work','a'*40,'base','policy',manifest['requirements_hash'],manifest_hash(manifest)),
                evidence={'verified':True,'head_oid':'a'*40,'base_oid':'base','pull_number':5})

    def test_concurrent_task_reservations_share_cas_and_replay_survives_restart(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from forge.store.models import VersionConflict
        plan=self.plan([('one','child',self.members,[],[]),('two','child',self.members,[],[])])
        commands=[self.command(),self.command()]; barrier=Barrier(2)
        def attempt(index):
            key=('one','two')[index]; identity=plan['task_ids'][key]
            manifest=self.query.task_manifest(plan['plan_id'],identity)
            args=dict(slot='only-slot',execution_fence=key,input_manifest=manifest,worktree_key=key)
            barrier.wait()
            try: return index,Delivery(Store(self.store.root),clock=lambda:100).reserve_task(commands[index],plan['plan_id'],identity,**args),args
            except VersionConflict: return index,None,args
        with ThreadPoolExecutor(max_workers=2) as pool: results=list(pool.map(attempt,range(2)))
        accepted=[row for row in results if row[1] is not None];self.assertEqual(len(accepted),1)
        index,receipt,args=accepted[0]; identity=plan['task_ids'][('one','two')[index]]
        self.assertEqual(Delivery(Store(self.store.root),clock=lambda:101).reserve_task(commands[index],plan['plan_id'],identity,**args),receipt)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM job_tasks').fetchone()[0],1)

    def test_multi_member_failures_charge_one_shared_budget_per_attempt(self):
        plan=self.plan([(str(index),'child',self.members,[],[]) for index in range(4)])
        jobs=[self.reserve(plan,str(index)) for index in range(4)]
        for index,(job,manifest) in enumerate(jobs):
            command=self.command(); args=dict(execution_fence=str(index),input_manifest=manifest,evidence={'reason':'code_failure'})
            result=self.life.record_failure(command,job['job_id'],**args)
            self.assertEqual(result['charged'],index<3)
            self.assertEqual(Lifecycle(Store(self.store.root),clock=lambda:101).record_failure(command,job['job_id'],**args),result)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],3)
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='recovery_pending' AND job_id IS NOT NULL").fetchone()[0],3)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE unit_id='batch' AND fenced=0").fetchone()[0],0)

    def test_worker_wait_uses_every_exact_task_member_not_delivery_scope(self):
        a,b=self.members
        plan=self.plan([('first','child',[a],[],[]),('second','child',[b],[],[])])
        job,manifest=self.reserve(plan,'first')
        result=self.life.defer_job(self.command(),job['job_id'],execution_fence='first',input_manifest=manifest,
            evidence={'reason':'needs_database'},code='environment',details={'reason':'missing_service'})
        with self.store.read_snapshot() as db:
            self.assertEqual([row[0] for row in db.execute("SELECT member_id FROM unit_gates WHERE code='environment' AND resolved_at IS NULL")],[a])
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)
        self.assertTrue(self.reserve(plan,'second')[0]['job_id'])

    def test_sql_process_status_uses_exact_job_members(self):
        from forge.store.executions import Executions
        a,b=self.members
        plan=self.plan([('a','child',[a],[],[]),('b','child',[b],[],[])])
        job,manifest=self.reserve(plan,'a')
        with self.store.read_snapshot() as db:
            rows={row['member_id']:dict(row) for row in db.execute("SELECT * FROM issue_selection_facts WHERE unit_id='batch'")}
            self.assertEqual((rows[a]['active'],rows[b]['active']),(1,0))
            self.assertEqual(rows[b]['indexed_status'],'queued')
        Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence='a',capture={'logs':'','worktree_bundle':b'bundle'})
        with self.store.read_snapshot() as db:
            rows={row['member_id']:dict(row) for row in db.execute("SELECT * FROM issue_selection_facts WHERE unit_id='batch'")}
            self.assertEqual(rows[a]['indexed_reason_code'],'cleanup_pending')
            self.assertIsNone(rows[b]['indexed_reason_code']);self.assertEqual(rows[b]['indexed_status'],'queued')

    def test_pending_v1_planning_and_integration_keep_original_manifest_contract(self):
        from unittest.mock import patch
        from forge.store.models import Candidate
        # Recreate a pre-upgrade planning assignment, then consume its late result
        # after the normal reader has switched to v2. No manifest is rewritten.
        with patch.object(Delivery,'_builder_manifest_in',staticmethod(Delivery._builder_manifest_v1_in)):
            old=self.fixture.manifest()
            parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='old-owner',execution_fence='old-plan',input_manifest=old)
        children=[{'key':str(index),'member_id':member,'scope':'Implement member','dependencies':[]} for index,member in enumerate(self.members)]
        saved=self.delivery.save_plan(self.command(),parent['job_id'],execution_fence='old-plan',input_manifest=old,plan={'children':children},evidence={'verified':True})
        self.fixture.clean(parent,'old-plan')
        self.assertNotIn('manifest_version',old)
        self.assertTrue(all(item['input_manifest']['manifest_version']==2 for item in self.query.assignments('batch',base_oid='base',policy_hash='policy')['items']))
        # Exact v1 child captures and integration assignments can still finish.
        from forge.store.executions import Executions, Launch
        for index in range(2):
            key=str(index); fence='legacy-'+key
            manifest=self.query.child_manifest(saved['plan_id'],key)
            job=self.delivery.delegate(self.command(),saved['plan_id'],key,slot='legacy-slot-'+key,
                execution_fence=fence,input_manifest=manifest,worktree_key='legacy-tree-'+key,
                launch=Launch('forge/legacy-'+key,'builder[bot]','codex',1000,{},{}))
            bundle=b'verified historical bundle'; digest=hashlib.sha256(bundle).hexdigest(); commit=('a' if index==0 else 'b')*40
            Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence=fence,
                capture={'logs':'','worktree_bundle':bundle})
            self.fixture.clean(job,fence)
            self.delivery.accept_part(self.command(),job['job_id'],execution_fence=fence,input_manifest=manifest,commit_oid=commit,
                evidence={'verified':True,'commit_oid':commit,'base_oid':'base','input_manifest_hash':manifest_hash(manifest),'bundle_digest':digest})
        with self.store.read_snapshot() as db:
            unit=dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
            original=Delivery._builder_manifest_v1_in(db,unit,'integration','base','policy')
        with patch.object(Delivery,'_builder_manifest_in',staticmethod(Delivery._builder_manifest_v1_in)):
            integration=self.delivery.reserve_builder(self.command(),stage='integration',slot='owner',execution_fence='old-integration',input_manifest=original)
        candidate=Candidate(12,'forge/old','c'*40,'base','policy',original['requirements_hash'],manifest_hash(original))
        command=self.command();args=dict(execution_fence='old-integration',input_manifest=original,candidate=candidate,
            evidence={'verified':True,'pull_number':12,'head_oid':candidate.head_oid,'base_oid':'base'})
        receipt=self.delivery.publish_candidate(command,integration['job_id'],**args)
        self.assertTrue(receipt['accepted'])
        self.assertEqual(Delivery(Store(self.store.root),clock=lambda:101).publish_candidate(command,integration['job_id'],**args),receipt)
        with self.store.read_snapshot() as db:
            stored=db.execute('SELECT input_manifest_json FROM attempts WHERE id=?',(integration['attempt_id'],)).fetchone()[0]
            self.assertEqual(json.loads(stored),original)
            self.assertEqual(db.execute('SELECT input_manifest_hash FROM candidates WHERE id=?',(receipt['candidate_id'],)).fetchone()[0],manifest_hash(original))

    def test_overlapping_captured_deferrals_preserve_both_results_and_gate_lineage(self):
        from forge.store.executions import Executions
        a,b=self.members
        plan=self.plan([('one','child',[a,b],[],[]),('two','child',[a],[],[])])
        jobs=[self.reserve(plan,key) for key in ('one','two')]
        for (job,manifest),key in zip(jobs,('one','two')):
            Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence=key,capture={'logs':''})
            self.fixture.clean(job,key)
        receipts=[]
        for (job,manifest),key in zip(jobs,('one','two')):
            command=self.command();args=dict(execution_fence=key,input_manifest=manifest,
                evidence={'reason':'missing_'+key},code='environment',details={'reason':'missing_'+key,'required_services':['postgres' if key=='one' else 'redis']})
            result=self.life.defer_job(command,job['job_id'],**args);receipts.append((command,result))
            self.assertTrue(result['accepted'])
            self.assertEqual(Lifecycle(Store(self.store.root),clock=lambda:101).defer_job(command,job['job_id'],**args),result)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE outcome='deferred'").fetchone()[0],2)
            gates=[dict(row) for row in db.execute("SELECT * FROM unit_gates WHERE code='environment' AND resolved_at IS NULL")]
            self.assertEqual({gate['member_id'] for gate in gates},{a,b})
            joined=json.loads(next(gate for gate in gates if gate['member_id']==a)['details_json'])
            self.assertEqual(joined['reason'],'missing_one')
            self.assertEqual(joined['source'],'worker_waits')
            self.assertEqual(joined['required_services'],['postgres','redis'])
            self.assertEqual(joined['worker_waits'],[{'job_id':jobs[1][0]['job_id'],'request_id':receipts[1][0].request_id}])
            prior=db.execute('SELECT resolution FROM unit_gates WHERE id=?',(joined['previous_gate_id'],)).fetchone()[0]
            self.assertEqual(prior,'superseded')
            additional=json.loads(db.execute('SELECT payload_json FROM decisions WHERE request_id=?',(joined['worker_waits'][0]['request_id'],)).fetchone()[0])
            self.assertEqual(additional['details'],{'reason':'missing_two','required_services':['redis']})
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_v2_assignment_cannot_downgrade_its_plan_contract(self):
        manifest=self.query.builder_manifest('batch','planning',base_oid='base',policy_hash='policy')
        job=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='plan',input_manifest=manifest)
        legacy={'children':[{'key':str(i),'member_id':member,'scope':'Implement','dependencies':[]} for i,member in enumerate(self.members)]}
        with self.assertRaises(InvalidDelegationPlan):
            self.delivery.save_plan(self.command(),job['job_id'],execution_fence='plan',input_manifest=manifest,plan=legacy,evidence={'verified':True})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM delegation_plans').fetchone()[0],0)
            self.assertIsNone(db.execute('SELECT outcome FROM jobs WHERE id=?',(job['job_id'],)).fetchone()[0])

    def test_bounded_deep_plan_and_output_join_do_not_use_python_call_stack(self):
        import sys
        from forge.store.task_inputs import current_task_output_in,task_output_ref
        from forge.store.transitions import _json
        a,b=self.members
        tasks=[(str(index),'child',[a],[] if index==0 else [str(index-1)],[]) for index in range(160)]
        previous_limit=sys.getrecursionlimit()
        try:
            # The real wire plan is below64KiB. A deliberately small Python
            # stack makes the independent graph bound observable in this test.
            sys.setrecursionlimit(120)
            plan=self.plan(tasks,final=[b])
            with self.store.write() as db:
                previous=None
                for index in range(160):
                    task_id=plan['task_ids'][str(index)]
                    pins=[dict(row) for row in db.execute('SELECT member_id,requirements_revision_id FROM delivery_task_members WHERE task_id=?',(task_id,))]
                    manifest={'manifest_version':2,'members':pins,'base_oid':'base','policy_hash':'policy',
                              'prerequisite_outputs':[] if previous is None else [previous]}
                    attempt='deep-'+str(index); job='job-'+attempt; output='output-'+attempt
                    db.execute("INSERT INTO attempts(id,unit_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) VALUES(?,'batch',1,1,?,1,100)",(attempt,_json(manifest)))
                    db.execute("INSERT INTO jobs(id,unit_id,attempt_id,role,phase,outcome,fenced,cleanup,execution_kind,created_at) VALUES(?,'batch',?,'build','cleaned','success',1,1,'task',100)",(job,attempt))
                    db.execute('INSERT INTO job_tasks VALUES(?,?,?,?)',(job,plan['plan_id'],task_id,'tree-'+attempt))
                    db.execute("INSERT INTO part_outputs(id,unit_id,attempt_id,membership_generation,commit_oid,base_oid,input_manifest_hash,accepted_at,task_id) VALUES(?,'batch',?,1,?,'base',?,100,?)",
                        (output,attempt,hashlib.sha1(attempt.encode()).hexdigest(),manifest_hash(manifest),task_id))
                    previous=task_output_ref(dict(db.execute('SELECT * FROM part_outputs WHERE id=?',(output,)).fetchone()))
                unit=dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
                result=current_task_output_in(db,unit,plan['task_ids']['159'],base_oid='base',policy_hash='policy')
                self.assertEqual(result['id'],'output-deep-159')
                db.execute("INSERT INTO part_output_invalidations VALUES('output-deep-0','changed','{}',101)")
                self.assertIsNone(current_task_output_in(db,unit,plan['task_ids']['159'],base_oid='base',policy_hash='policy'))
        finally:
            sys.setrecursionlimit(previous_limit)

    def test_pending_recovery_allows_independent_task_but_no_new_owner_plan(self):
        a,b=self.members
        plan=self.plan([('failed','child',[a],[],[]),('independent','child',[b],[],[])])
        job,manifest=self.reserve(plan,'failed')
        self.life.record_failure(self.command(),job['job_id'],execution_fence='failed',input_manifest=manifest,evidence={'reason':'code_failure'})
        self.fixture.clean(job,'failed')
        assignments=self.query.assignments('batch',base_oid='base',policy_hash='policy')
        self.assertEqual([item['key'] for item in assignments['items']],['independent'])
        other,inputs=self.reserve(plan,'independent');self.accept(other,inputs,'independent')
        waiting=self.query.assignments('batch',base_oid='base',policy_hash='policy')
        self.assertEqual(waiting['items'],[]);self.assertEqual(waiting['pending_reason'],'recovery_pending')
        changed=self.query.builder_manifest('batch','planning',base_oid='base',policy_hash='policy')
        with self.assertRaises(TransitionError):
            self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='retry',input_manifest=changed)

    def test_distinct_dependency_waits_remain_until_every_declared_issue_completes(self):
        from forge.store.executions import Executions
        from forge.store.runtime_requirements import RuntimeRequirements
        from forge.factory.prerequisites import collect
        a,b=self.members
        plan=self.plan([('one','child',[a],[],[]),('two','child',[a],[],[])],final=[b])
        jobs=[self.reserve(plan,key) for key in ('one','two')]
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT r.id,r.requirements_json FROM unit_issues m JOIN issue_revisions r ON r.id=m.requirements_revision_id WHERE m.id=?',(a,)).fetchone()
            revision=row['id'];requirements=json.loads(row['requirements_json'])
        for (job,manifest),key in zip(jobs,('one','two')):
            Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence=key,capture={'logs':''})
            self.fixture.clean(job,key)
        for (job,manifest),key,number in zip(jobs,('one','two'),(10,11)):
            self.life.defer_job(self.command(),job['job_id'],execution_fence=key,input_manifest=manifest,evidence={'reason':'prerequisite'},code='dependency',
                details={'requirements':requirements,'issues':[number],'blocked':[number],'evidence':'Depends on #'+str(number)})
        class GitHub:
            completed={10}
            def prerequisite(self,reference,binding):
                number=int(reference.rsplit('#',1)[1]);done=number in self.completed
                return {'reference':reference,'kind':'issue','repository_id':1,'github_id':1000+number,'number':number,
                        'state':'closed' if done else 'open','state_reason':'completed' if done else None,'merged':False,
                        'updated_at':'2026-10-10T12:00:00Z'}
        github=GitHub();commands=RuntimeRequirements(self.store,clock=lambda:101)
        observation=collect(self.store,github,unit_id='batch',member_id=a,requirements_revision_id=revision,body='Scope')
        self.assertEqual({fact['number'] for fact in observation['facts']},{10,11})
        first=commands.dependencies(self.command(),requirements_revision_id=revision,generation=1,facts=observation['facts'])
        self.assertFalse(first['clear'])
        with self.store.read_snapshot() as db: version=db.execute("SELECT version FROM units WHERE id='batch'").fetchone()[0]
        commands.dependencies(self.command(),requirements_revision_id=revision,generation=2,facts=observation['facts'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT version FROM units WHERE id='batch'").fetchone()[0],version)
            self.assertEqual(db.execute('SELECT max(generation) FROM dependency_checks WHERE member_id=?',(a,)).fetchone()[0],2)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE member_id=? AND code='dependency' AND resolved_at IS NULL",(a,)).fetchone()[0],1)
        github.completed.add(11)
        observation=collect(self.store,github,unit_id='batch',member_id=a,requirements_revision_id=revision,body='Scope')
        second=commands.dependencies(self.command(),requirements_revision_id=revision,generation=3,facts=observation['facts'])
        self.assertTrue(second['clear'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE member_id=? AND code='dependency' AND resolved_at IS NULL",(a,)).fetchone()[0],0)
        # Fresh proof remains tied to both identities if one reopens later.
        self.assertEqual({fact['number'] for fact in collect(self.store,github,unit_id='batch',member_id=a,requirements_revision_id=revision,body='Scope')['facts']},{10,11})

    def test_overlapping_decisions_present_every_question_and_option(self):
        from forge.store.executions import Executions
        a,b=self.members
        plan=self.plan([('one','child',[a],[],[]),('two','child',[a],[],[])],final=[b])
        jobs=[self.reserve(plan,key) for key in ('one','two')]
        for (job,manifest),key in zip(jobs,('one','two')):
            Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence=key,capture={'logs':''});self.fixture.clean(job,key)
        for (job,manifest),key in zip(jobs,('one','two')):
            self.life.defer_job(self.command(),job['job_id'],execution_fence=key,input_manifest=manifest,evidence={'reason':'question'},code='decision_required',
                details={'question':'Question '+key,'options':[key+' A',key+' B'],'recommendation':'Choose '+key+' A','evidence':'Evidence '+key})
        with self.store.read_snapshot() as db:
            from forge.store.worker_waits import details_in
            value=details_in(db,db.execute("SELECT * FROM unit_gates WHERE member_id=? AND code='decision_required' AND resolved_at IS NULL",(a,)).fetchone())
            self.assertEqual(value['source'],'worker_waits')
            self.assertEqual(value['question'],'Task one: Question one\n\nTask two: Question two')
            self.assertEqual(value['options'],['Task one: one A','Task one: one B','Task two: two A','Task two: two B'])
            self.assertEqual(value['recommendation'],'Task one: Choose one A\n\nTask two: Choose two A')

    def test_large_individually_valid_decisions_are_all_visible_and_release_current_set(self):
        from forge.store.executions import Executions
        from forge.store.worker_waits import details_in
        from forge.store.models import VersionConflict
        from forge.factory.store_views import PipelineViews
        from forge.factory.config import validate
        a,b=self.members
        keys=[str(index) for index in range(4)]
        plan=self.plan([(key,'child',[a],[],[]) for key in keys],final=[b])
        jobs=[self.reserve(plan,key) for key in keys]
        for (job,manifest),key in zip(jobs,keys):
            Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence=key,capture={'logs':''});self.fixture.clean(job,key)
        stale=None
        for (job,manifest),key in zip(jobs,keys):
            report={'question':key+'Q'*3990,'recommendation':key+'R'*3990,'evidence':key+'E'*3990,
                    'options':[key+str(index)+'O'*990 for index in range(6)]}
            self.life.defer_job(self.command(),job['job_id'],execution_fence=key,input_manifest=manifest,evidence={'reason':'question'},code='decision_required',details=report)
            if stale is None: stale=self.command()
        with self.store.read_snapshot() as db:
            gate=dict(db.execute("SELECT * FROM unit_gates WHERE member_id=? AND code='decision_required' AND resolved_at IS NULL",(a,)).fetchone())
            expanded=details_in(db,gate)
            self.assertLess(len(gate['details_json'].encode()),65536)
            self.assertGreater(len(json.dumps(expanded).encode()),65536)
            self.assertEqual({wait['job_id'] for wait in expanded['waits']},{job['job_id'] for job,_ in jobs})
            self.assertEqual(len(expanded['options']),24)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE outcome='deferred'").fetchone()[0],4)
            issue=db.execute('SELECT issue_id FROM unit_issues WHERE id=?',(a,)).fetchone()[0]
        detail=PipelineViews(self.store,runtime=validate({}),clock=lambda:101).issues(key=issue)['issue']
        self.assertEqual(detail['decision_request'],expanded)
        with self.assertRaises(VersionConflict):
            self.life.resolve_gate(stale,gate['id'],release='operator',evidence={'decision':'Address every displayed task question'})
        self.life.resolve_gate(self.command(),gate['id'],release='operator',evidence={'decision':'Address every displayed task question'})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL").fetchone()[0],0)

    def test_environment_retry_checks_exact_task_scope_and_own_process_gates(self):
        from forge.store.executions import Executions
        from forge.store.runtime_requirements import RuntimeRequirements
        a,b=self.members
        plan=self.plan([('one','child',[a],[],[]),('two','child',[a],[],[])],final=[b])
        jobs=[self.reserve(plan,key) for key in ('one','two')]
        for (job,manifest),key in zip(jobs,('one','two')):
            Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence=key,capture={'logs':''})
        self.fixture.clean(jobs[0][0],'one')
        own=self.life.open_gate(self.command(),'label_hold',member_id=a,details={'reason':'hold'})
        self.life.open_gate(self.command(),'label_hold',member_id=b,details={'reason':'unrelated_hold'})
        commands=RuntimeRequirements(self.store,clock=lambda:101)
        args=dict(execution_fence='one',input_manifest=jobs[0][1],kind='services',selection={'services':['postgres']},evidence={'full_environment_available':True})
        with self.assertRaises(TransitionError):commands.environment_retry(self.command(),jobs[0][0]['job_id'],**args)
        self.life.resolve_gate(self.command(),own['gate_id'],release='observation',evidence={'verified':True,'observed_at':101})
        self.assertTrue(commands.environment_retry(self.command(),jobs[0][0]['job_id'],**args)['accepted'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT cleanup FROM jobs WHERE id=?',(jobs[1][0]['job_id'],)).fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE member_id=? AND code='label_hold' AND resolved_at IS NULL",(b,)).fetchone()[0],1)

    def test_joint_task_dependency_wait_binds_each_distinct_assigned_revision(self):
        from forge.store.executions import Executions
        a,b=self.members
        with self.store.write() as db:
            original=dict(db.execute('SELECT r.* FROM unit_issues m JOIN issue_revisions r ON r.id=m.requirements_revision_id WHERE m.id=?',(b,)).fetchone())
            db.execute('INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES(?,?,?,?,?,?,?,?)',
                ('other-revision',original['issue_id'],original['title'],original['body'],original['content_hash'],'other-requirements',json.dumps({'acceptance':['other behavior']}),100))
            db.execute("UPDATE unit_issues SET requirements_revision_id='other-revision' WHERE id=?",(b,))
            db.execute("UPDATE issues SET current_revision_id='other-revision' WHERE id=?",(original['issue_id'],))
        plan=self.plan([('shared','child',[a,b],[],[])])
        job,manifest=self.reserve(plan,'shared')
        Executions(self.store,clock=lambda:100).collect(self.command(),job['job_id'],execution_fence='shared',capture={'logs':''})
        self.fixture.clean(job,'shared')
        details={'issues':[10],'blocked':[10],'evidence':'Depends on #10'}
        with self.assertRaises(TransitionError):
            self.life.defer_job(self.command(),job['job_id'],execution_fence='shared',input_manifest=manifest,
                evidence={'reason':'prerequisite'},code='dependency',details={**details,'requirements':{'acceptance':['works']}})
        receipt=self.life.defer_job(self.command(),job['job_id'],execution_fence='shared',input_manifest=manifest,
            evidence={'reason':'prerequisite'},code='dependency',details=details)
        self.assertEqual(len(receipt['gate_ids']),2)
        with self.store.read_snapshot() as db:
            for row in db.execute("SELECT g.details_json,r.requirements_json FROM unit_gates g JOIN job_members p ON p.member_id=g.member_id JOIN issue_revisions r ON r.id=p.requirements_revision_id WHERE p.job_id=? AND g.code='dependency' AND g.resolved_at IS NULL",(job['job_id'],)):
                self.assertEqual(json.loads(row['details_json'])['requirements'],json.loads(row['requirements_json']))
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)
