"""Composition, physical leases and logical builder ownership share one authority."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import tempfile
import hashlib
import json
import unittest

from forge.store.db import Store
from forge.store.delivery import Delivery, DeliveryQueries, _unit
from forge.store.executions import Executions, Launch
from forge.store.models import Command, Candidate, TransitionError, VersionConflict, InvalidDelegationPlan
from forge.store.transitions import Lifecycle, manifest_hash, project_unit


class ManifestQueries(DeliveryQueries):
    """Test-only readers of the manifests and next assignment the controllers build."""

    def builder_manifest(self,unit_id,stage,*,base_oid,policy_hash):
        with self.store.read_snapshot() as db:
            return Delivery._builder_manifest_in(db,_unit(db,unit_id),stage,base_oid,policy_hash)

    def task_manifest(self,plan_id,task_id):
        from forge.store.task_inputs import task_manifest_in,current_plan_in
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT b.unit_id FROM delegation_plans p JOIN unit_builders b ON b.id=p.builder_id WHERE p.id=?',(plan_id,)).fetchone()
            if row is None: raise KeyError(plan_id)
            unit=_unit(db,row[0]); plan=current_plan_in(db,unit['id'])
            if not plan or plan['id']!=plan_id: raise TransitionError('task plan has changed')
            return task_manifest_in(db,unit,plan,task_id)

    def next_assignment(self,unit_id,*,base_oid,policy_hash,allowed_members=None):
        result = self.assignments(unit_id,base_oid=base_oid,policy_hash=policy_hash,limit=1,allowed_members=allowed_members)
        return {key:value for key,value in result.items() if key!='items'} | {'assignment':result['items'][0] if result['items'] else None}


def task_plan(tasks, retained=()):
    return {'version': 2, 'retained_task_ids': list(retained), 'tasks': tasks,
            'owner_final': {'member_ids': [], 'scope': '', 'internalizes': []}}


class DeliveryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.store=Store.create(self.temporary.name)
        self.life=Lifecycle(self.store,clock=lambda:100.0)
        self.delivery=Delivery(self.store,clock=lambda:100.0)
        self.query=ManifestQueries(self.store)
        self.sequence=0

    def command(self,unit='batch'):
        self.sequence+=1
        with self.store.read_snapshot() as db: version=project_unit(db,unit)['version']
        return Command('request-'+str(self.sequence),'operator',unit,version)

    def compose(self,count=2,dependencies=()):
        pins=[]
        for number in range(count):
            admitted=self.life.admit_issue(request_id='admit-'+str(number),actor_id='operator',unit_id='source-'+str(number),
                issue_id='issue-'+str(number),repository_id=1,repository_name='example/repo',issue_number=number+1,
                title='Issue',body='Scope',requirements={'acceptance':['works']},observed_at=90)
            pins.append(dict(issue_id='issue-'+str(number),issue_version=1,requirements_revision_id=admitted['revision_id'],
                             unit_id='source-'+str(number),unit_version=1,member_id=admitted['member_id']))
        proposal=dict(repository_id=1,base_branch='main',members=pins,dependencies=list(dependencies),existing_prs=[],rationale='Related change')
        approval={'approved':True,'composition_hash':self.delivery.composition_hash(**proposal)}
        command=Command('compose','operator','batch',1)
        result=self.delivery.compose(command,**proposal,approval=approval)
        self.assertEqual(result,self.delivery.compose(command,**proposal,approval=approval))
        self.members={member['issue_id']:member['member_id'] for member in result['members']}
        return result

    def manifest(self,stage='planning'):
        return self.query.builder_manifest('batch',stage,base_oid='base',policy_hash='policy')

    def clean(self,job,fence):
        return self.life.verify_cleanup(self.command(),job['job_id'],execution_fence=fence,
             evidence={'process_stopped':True,'resources_released':True})

    def plan(self):
        manifest=self.manifest()
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='parent-fence',input_manifest=manifest)
        with self.store.read_snapshot() as db:
            from forge.store.delivery_inputs import dependencies_in
            unit=dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
            keys={member:str(i) for i,member in enumerate(self.members.values())}
            children=[dict(key=keys[member],executor='child',member_ids=[member],scope='Implement member',
                depends_on=[keys[predecessor] for predecessor in dependencies_in(db,unit,member)],internalizes=[])
                      for member in self.members.values()]
        saved=self.delivery.save_task_plan(self.command(),parent['job_id'],execution_fence='parent-fence',input_manifest=manifest,
                                      plan=task_plan(children),evidence={'verified':True})
        self.clean(parent,'parent-fence')
        return parent,saved,children

    def child(self,plan,key):
        manifest=self.query.task_manifest(plan['plan_id'],plan['task_ids'][key])
        child=self.delivery.reserve_task(self.command(),plan['plan_id'],plan['task_ids'][key],slot='child-'+key,execution_fence='fence-'+key,
            input_manifest=manifest,worktree_key='worktree-'+key,launch=Launch('forge/task-'+key,'builder[bot]','codex',1000,{},{}))
        return child,manifest

    def accept(self,child,manifest,key):
        commit=('a' if key=='0' else 'b')*40
        bundle=b'synthetic retained bundle'
        digest=hashlib.sha256(bundle).hexdigest()
        Executions(self.store,clock=lambda:100).collect(self.command(),child['job_id'],execution_fence='fence-'+key,
            capture={'logs':'','worktree_bundle':bundle})
        self.clean(child,'fence-'+key)
        accepted=self.delivery.accept_task_output(self.command(),child['job_id'],execution_fence='fence-'+key,input_manifest=manifest,
            commit_oid=commit,evidence={'verified':True,'commit_oid':commit,'base_oid':'base','input_manifest_hash':manifest_hash(manifest),'bundle_digest':digest})
        return accepted

    def test_integration_publication_checks_each_member_hold_in_build_phase(self):
        from forge.store.publications import Publications
        self.compose(); parent,plan,children=self.plan()
        for key in ('0','1'):
            child,manifest=self.child(plan,key);self.accept(child,manifest,key)
        manifest=self.manifest('integration')
        integrated=self.delivery.reserve_builder(self.command(),stage='integration',slot='owner',
            execution_fence='integration-fence',input_manifest=manifest,
            launch=Launch('forge/batch','builder[bot]','codex',1000,{},{}))
        Executions(self.store,clock=lambda:100).collect(self.command(),integrated['job_id'],
            execution_fence='integration-fence',capture={'result':None,'logs':''})
        self.clean(integrated,'integration-fence')
        row=dict(unit_id='batch',job_id=integrated['job_id'],execution_fence='integration-fence',
            input_manifest_json=json.dumps(manifest))
        commands=Publications(self.store,clock=lambda:100)
        with self.store.read_snapshot() as db:
            self.assertEqual(commands._ready(db,row)[1]['state'],'building')
        self.life.open_gate(self.command(),'environment',details={'capability':'database'},
            member_id=self.members['issue-1'])
        with self.store.read_snapshot() as db:
            with self.assertRaisesRegex(TransitionError,'independent gate'):
                commands._ready(db,row)

    def test_owner_job_wait_is_not_offered_as_a_rejected_builder_reservation(self):
        # Production 2026-10-15: a Forge problem on the owning planning job left
        # the selector offering planning; reserve_builder rejected it each cycle.
        self.compose()
        manifest=self.manifest()
        owner=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='owner-fence',input_manifest=manifest,
            launch=Launch('forge/batch','builder[bot]','codex',1000,{},{}))
        Executions(self.store,clock=lambda:100).collect(self.command(),owner['job_id'],execution_fence='owner-fence',
            capture={'result':None,'logs':''})
        self.clean(owner,'owner-fence')
        command=self.command()
        def change(db,unit):
            # The shape ExecutionProblems.defer leaves: a deferred owner job and
            # its exact memberless forge_problem wait.
            job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(owner['job_id'],)).fetchone())
            self.life._result(db,command,job,{'reason':'forge_problem'},'deferred')
            self.life._open_gate(db,command,'forge_problem',{'reason':'fixture'},job_id=owner['job_id'])
            return {}
        self.life._apply(command,'fixture_gate',{},change)
        selected=self.query.next_assignment('batch',base_oid='base',policy_hash='policy')
        self.assertIsNone(selected['assignment'])
        self.assertEqual(selected['pending_reason'],'forge_problem')
        with self.assertRaisesRegex(TransitionError,'owning builder'):
            self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='next-fence',input_manifest=manifest)

    def test_parallel_parts_resume_same_builder_then_one_exact_review_and_audit(self):
        self.compose(); parent,plan,children=self.plan()
        first,one=self.child(plan,'0'); second,two=self.child(plan,'1')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_members WHERE job_id=?',(parent['job_id'],)).fetchone()[0],2)
        self.accept(first,one,'0'); self.accept(second,two,'1')
        manifest=self.manifest('integration')
        self.assertEqual(self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']['stage'],'integration')
        integrated=self.delivery.reserve_builder(self.command(),stage='integration',slot='owner',execution_fence='integration-fence',input_manifest=manifest)
        self.assertEqual(integrated['builder_id'],parent['builder_id'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='batch'").fetchone()[0], 'building')
            self.assertEqual(db.execute('SELECT stage FROM builder_assignments WHERE job_id=?',(integrated['job_id'],)).fetchone()[0], 'integration')
        candidate=Candidate(7,'forge/batch','c'*40,'base','policy',manifest['requirements_hash'],manifest_hash(manifest))
        self.delivery.publish_candidate(self.command(),integrated['job_id'],execution_fence='integration-fence',input_manifest=manifest,candidate=candidate,
            evidence={'verified':True,'pull_number':7,'head_oid':candidate.head_oid,'base_oid':'base'})
        self.clean(integrated,'integration-fence')
        for role in ('review','audit'):
            with self.store.read_snapshot() as db:
                identity=dict(db.execute("SELECT c.* FROM candidates c JOIN units u ON u.current_candidate_id=c.id WHERE u.id='batch'").fetchone())
            exact={key:identity[key] for key in ('head_oid','base_oid','policy_hash','requirements_hash','input_manifest_hash')}
            exact['candidate_id']=identity['id']
            job=self.life.reserve_job(self.command(),role,slot='verdict',execution_fence=role+'-fence',input_manifest=exact,candidate_id=identity['id'])
            self.life.finish_job(self.command(),job['job_id'],execution_fence=role+'-fence',input_manifest=exact,evidence={'candidate_id':identity['id'],'verdict':'pass'})
            self.clean(job,role+'-fence')
        with self.store.read_snapshot() as db:
            self.assertEqual(project_unit(db,'batch')['state'],'merging')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidate_evidence WHERE kind IN ('review','audit')").fetchone()[0],2)

    def test_dependent_part_pins_exact_predecessor_and_unrelated_part_can_run(self):
        self.compose(3,[{'issue_id':'issue-1','prerequisite_issue_id':'issue-0'}]); parent,plan,children=self.plan()
        with self.assertRaises(TransitionError): self.query.task_manifest(plan['plan_id'],plan['task_ids']['1'])
        runnable=self.query.assignments('batch',base_oid='base',policy_hash='policy')['items']
        self.assertEqual({item['key'] for item in runnable},{'0','2'})
        first,manifest=self.child(plan,'0'); accepted=self.accept(first,manifest,'0')
        next_manifest=self.query.task_manifest(plan['plan_id'],plan['task_ids']['1'])
        self.assertEqual(next_manifest['prerequisite_outputs'][0]['output_id'],accepted['output_id'])
        wrong={**next_manifest,'prerequisite_outputs':[]}
        with self.assertRaises(TransitionError):
            self.delivery.reserve_task(self.command(),plan['plan_id'],plan['task_ids']['1'],slot='wrong',execution_fence='wrong',input_manifest=wrong,worktree_key='wrong')

    def test_third_failure_fences_tree_without_fourth_or_parent_propagation_charge(self):
        self.compose(4); parent,plan,children=self.plan()
        assignments=[self.child(plan,str(i)) for i in range(4)]
        for index,(job,manifest) in enumerate(assignments):
            result=self.life.record_failure(self.command(),job['job_id'],execution_fence='fence-'+str(index),input_manifest=manifest,evidence={'reason':'code_failure'})
            self.assertEqual(result['charged'],index<3)
        tree=self.query.tree('batch')
        self.assertEqual(tree['charged_failures'],3)
        self.assertTrue(all(job['fenced'] for job in tree['jobs']))
        self.delivery.cancel_builder(self.command(),evidence={'reason':'child_failure'})
        self.assertEqual(self.query.tree('batch')['charged_failures'],3)
        with self.store.read_snapshot() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],4)

    def test_competing_reservations_use_one_atomic_version_and_slot(self):
        self.compose(); parent,plan,children=self.plan()
        current=self.command(); barrier=Barrier(2)
        def reserve(index):
            command=replace(current,request_id='race-'+str(index))
            manifest=self.query.task_manifest(plan['plan_id'],plan['task_ids'][str(index)])
            barrier.wait()
            try:
                return self.delivery.reserve_task(command,plan['plan_id'],plan['task_ids'][str(index)],slot='same-slot',execution_fence='race-fence-'+str(index),
                    input_manifest=manifest,worktree_key='race-'+str(index))
            except VersionConflict:
                return None
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(reserve,range(2)))
        self.assertEqual(sum(result is not None for result in results),1)
        with self.store.read_snapshot() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],1)

    def test_cancelled_child_result_cannot_advance_or_release_its_lease(self):
        self.compose(); parent,plan,children=self.plan(); child,manifest=self.child(plan,'0')
        self.delivery.cancel_builder(self.command(),evidence={'reason':'operator_cancelled'})
        result=self.delivery.accept_task_output(self.command(),child['job_id'],execution_fence='fence-0',input_manifest=manifest,
            commit_oid='a'*40,evidence={'verified':True,'commit_oid':'a'*40,'base_oid':'base','input_manifest_hash':manifest_hash(manifest)})
        self.assertFalse(result['accepted'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM part_outputs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],1)
        self.clean(child,'fence-0')

    def test_plan_commit_replay_and_restart_do_not_duplicate_children(self):
        self.compose(); parent,plan,children=self.plan()
        restarted=Delivery(self.store,clock=lambda:101.0)
        manifest=self.query.task_manifest(plan['plan_id'],plan['task_ids']['0'])
        command=self.command()
        args=dict(slot='child-0',execution_fence='fence-0',input_manifest=manifest,worktree_key='worktree-0')
        first=restarted.reserve_task(command,plan['plan_id'],plan['task_ids']['0'],**args)
        self.assertEqual(restarted.reserve_task(command,plan['plan_id'],plan['task_ids']['0'],**args),first)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_tasks').fetchone()[0],1)
        with self.assertRaises(TransitionError):
            restarted.reserve_task(self.command(),plan['plan_id'],plan['task_ids']['0'],**{**args,'slot':'other','execution_fence':'other','worktree_key':'other'})

    def test_failed_transaction_leaves_no_plan_or_result_and_can_retry(self):
        self.compose(); manifest=self.manifest()
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='parent',input_manifest=manifest)
        command=self.command()
        children=[dict(key=str(i),executor='child',member_ids=[member],scope='Implement',depends_on=[],internalizes=[]) for i,member in enumerate(self.members.values())]
        from unittest.mock import patch
        with patch.object(self.delivery,'_save',side_effect=RuntimeError('simulated commit interruption')):
            with self.assertRaises(RuntimeError):
                self.delivery.save_task_plan(command,parent['job_id'],execution_fence='parent',input_manifest=manifest,plan=task_plan(children),evidence={'verified':True})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delegation_plans').fetchone()[0],0)
            self.assertIsNone(db.execute('SELECT outcome FROM jobs WHERE id=?',(parent['job_id'],)).fetchone()[0])
        self.assertTrue(self.delivery.save_task_plan(command,parent['job_id'],execution_fence='parent',input_manifest=manifest,plan=task_plan(children),evidence={'verified':True})['accepted'])

    def test_drop_prerequisite_invalidates_descendants_and_preserves_unrelated_outputs(self):
        self.compose(3,[{'issue_id':'issue-1','prerequisite_issue_id':'issue-0'}]); parent,plan,children=self.plan()
        for key in ('0','1','2'):
            child,manifest=self.child(plan,key); self.accept(child,manifest,key)
        removed=self.members['issue-0']; dependent=self.members['issue-1']; independent=self.members['issue-2']
        with self.assertRaises(TransitionError):
            self.delivery.drop_member(self.command(),removed,membership_generation=1,dependent_decisions={},evidence={'approved':True})
        result=self.delivery.drop_member(self.command(),removed,membership_generation=1,
            dependent_decisions={dependent:'wait_external'},evidence={'approved':True})
        tree=self.query.tree('batch')
        self.assertEqual(tree['outputs'],[])
        reusable=self.manifest()['available_tasks']
        self.assertEqual([pin['member_id'] for task in reusable if task['output'] for pin in task['members']],[independent])
        self.assertIsNotNone(result['released']['unit_id'])
        self.assertEqual(next(member for member in tree['members'] if member['id']==dependent)['current_blocker']['code'],'dependency')

    def test_member_fence_leaves_independent_child_authority_and_capacity_owned(self):
        self.compose(); parent,plan,children=self.plan()
        one,first=self.child(plan,'0'); two,second=self.child(plan,'1')
        command=self.command()
        with self.store.write() as db:
            self.life.fence_member_in(db,command,self.members['issue-0'],reason='member_environment_changed')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?',(one['job_id'],)).fetchone()[0],1)
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?',(two['job_id'],)).fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],2)
        self.assertTrue(self.accept(two,second,'1')['accepted'])

    def test_invalid_plan_is_typed_and_replayed_without_consuming_the_assignment(self):
        self.compose(); manifest=self.manifest()
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='parent',input_manifest=manifest)
        command=self.command()
        for _ in range(2):
            with self.assertRaises(InvalidDelegationPlan):
                self.delivery.save_task_plan(command,parent['job_id'],execution_fence='parent',input_manifest=manifest,
                    plan={'children':[]},evidence={'verified':True})
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT outcome FROM jobs WHERE id=?',(parent['job_id'],)).fetchone()[0])
        charged=self.life.record_failure(self.command(),parent['job_id'],execution_fence='parent',input_manifest=manifest,
            evidence={'reason':'invalid_delegation_plan'})
        self.assertTrue(charged['charged'])
        self.assertEqual(self.query.tree('batch')['charged_failures'],1)

    def test_existing_pr_composition_requires_exact_explicit_publication_choice(self):
        pins=[]; receipts=[]
        for index in range(2):
            unit='existing-'+str(index); issue='existing-issue-'+str(index)
            admitted=self.life.admit_issue(request_id='existing-admit-'+str(index),actor_id='operator',unit_id=unit,
                issue_id=issue,repository_id=1,repository_name='example/repo',issue_number=index+1,title='Existing',body='',
                requirements={'acceptance':['works']},observed_at=90)
            with self.store.read_snapshot() as db:
                req=db.execute('SELECT requirements_hash FROM issue_revisions WHERE id=?',(admitted['revision_id'],)).fetchone()[0]
            candidate=Candidate(index+10,'forge/existing-'+str(index),str(index)*40,'base','policy',req,'original')
            chosen=self.life.adopt_candidate(self.command(unit),candidate,evidence={'approved':True,'verified':True,
                **{key:getattr(candidate,key) for key in ('pull_number','head_oid','base_oid','policy_hash','input_manifest_hash')}})
            pins.append(dict(issue_id=issue,issue_version=1,requirements_revision_id=admitted['revision_id'],
                unit_id=unit,unit_version=chosen['version'],member_id=admitted['member_id']))
            receipts.append(dict(unit_id=unit,candidate_id=chosen['candidate_id'],verified=True,merged=False,
                pull_number=candidate.pull_number,head_oid=candidate.head_oid,observed_at=100,author={'id':10+index,'type':'User','login':'author-'+str(index)}))
        proposal=dict(repository_id=1,base_branch='main',members=pins,existing_prs=receipts,rationale='One change')
        approval={'approved':True,'composition_hash':self.delivery.composition_hash(**proposal)}
        command=Command('missing-target','operator','batch',1)
        for _ in range(2):
            with self.assertRaisesRegex(TransitionError,'explicit publication target'):
                self.delivery.compose(command,**proposal,approval=approval)
        target={'kind':'existing','ownership_transfer':True,**{key:receipts[0][key] for key in ('candidate_id','pull_number','head_oid','unit_id','author')}}
        proposal['publication_target']=target
        approval={'approved':True,'composition_hash':self.delivery.composition_hash(**proposal)}
        self.delivery.compose(Command('approved-target','operator','batch',1),**proposal,approval=approval)
        manifest=self.manifest()
        self.assertEqual(manifest['publication_target'],target)
        self.assertEqual(len(manifest['existing_candidates']),2)
        self.assertEqual([row['author'] for row in manifest['existing_candidates']], [row['author'] for row in receipts])
        from forge.factory.ownership import publication_context, approved_source_matches, publication_transfer_matches, ownership
        context = publication_context(self.store, 'batch', {'number': 2, 'repo_id': 1})
        source = manifest['existing_candidates'][1]
        pr = {'number': source['pull_number'], 'state': 'open', 'merged': False, 'body': 'Fixes #2',
              'user': source['author'], 'author_association':'COLLABORATOR', 'closing_issues':[{'repository_id':1,'number':2}], 'head': {'sha': source['head_oid'], 'ref': source['branch'], 'repo': {'id': 1}},
              'base': {'ref': 'main', 'repo': {'id': 1}}}
        self.assertTrue(approved_source_matches(pr, context))
        self.assertFalse(publication_transfer_matches(pr, context))
        self.assertIsNone(ownership({'number': 2}, context, 'example/repo', [pr]))
        changed = {**pr, 'head': {**pr['head'], 'sha': 'f' * 40}}
        self.assertIsNotNone(ownership({'number': 2}, context, 'example/repo', [changed]))
        changed = {**pr, 'user': {**pr['user'], 'id': 99}}
        self.assertIsNotNone(ownership({'number': 2}, context, 'example/repo', [changed]))
        selected=replace(candidate,pull_number=target['pull_number'],branch='forge/existing-0')
        self.delivery._publication_target_in(manifest,selected)
        with self.assertRaises(TransitionError):self.delivery._publication_target_in(manifest,candidate)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM external_effects').fetchone()[0],0)

    def test_execution_tree_history_has_stable_bounded_cursor(self):
        self.compose(); parent,plan,children=self.plan(); self.child(plan,'0'); self.child(plan,'1')
        first=self.query.tree('batch',limit=2)
        second=self.query.tree('batch',after=first['next_cursor'],limit=2)
        self.assertEqual(len(first['jobs']),2); self.assertEqual(len(second['jobs']),1)
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len({job['id'] for job in first['jobs']+second['jobs']}),3)

    def test_drop_to_one_preserves_owning_builder_and_publication_authority(self):
        self.compose(); parent,plan,children=self.plan()
        for key in ('0','1'):
            child,manifest=self.child(plan,key); self.accept(child,manifest,key)
        self.delivery.drop_member(self.command(),self.members['issue-0'],membership_generation=1,
            dependent_decisions={},evidence={'approved':True})
        assignment=self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']
        self.assertEqual(assignment['stage'],'planning')
        retained=[task['id'] for task in assignment['input_manifest']['available_tasks']]
        self.assertEqual(retained,[plan['task_ids']['1']])
        replanning=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='owner-replan',input_manifest=assignment['input_manifest'])
        self.delivery.save_task_plan(self.command(),replanning['job_id'],execution_fence='owner-replan',input_manifest=assignment['input_manifest'],
            plan=task_plan([],retained),evidence={'verified':True})
        self.clean(replanning,'owner-replan')
        assignment=self.query.next_assignment('batch',base_oid='base',policy_hash='policy')['assignment']
        self.assertEqual(assignment['stage'],'integration')
        owner=self.delivery.reserve_builder(self.command(),stage='integration',slot='owner',execution_fence='owner-resumed',
            input_manifest=assignment['input_manifest'])
        self.assertEqual(owner['builder_id'],parent['builder_id'])
        with self.store.read_snapshot() as db:
            self.assertTrue(project_unit(db,'batch')['delivery_approved'])
            self.assertEqual(len(project_unit(db,'batch')['members']),1)

    def test_large_composition_filters_fresh_eligibility_before_result_limit(self):
        result=self.compose(101)
        self.assertEqual(len(result['members']),101)
        parent,plan,children=self.plan()
        last=children[-1]
        assignments=self.query.assignments('batch',base_oid='base',policy_hash='policy',limit=1,allowed_members=set(last['member_ids']))
        self.assertEqual([item['member_ids'] for item in assignments['items']],[last['member_ids']])

    def test_replan_on_new_base_selects_predecessor_before_stale_output_dependents(self):
        self.compose(3,[{'issue_id':'issue-1','prerequisite_issue_id':'issue-0'}])
        _,plan,children=self.plan()
        first,manifest=self.child(plan,'0');self.accept(first,manifest,'0')
        fresh=self.query.builder_manifest('batch','planning',base_oid='new-base',policy_hash='policy')
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='new-parent',input_manifest=fresh)
        self.delivery.save_task_plan(self.command(),parent['job_id'],execution_fence='new-parent',input_manifest=fresh,
                                plan=task_plan(children),evidence={'verified':True})
        self.clean(parent,'new-parent')
        choices=self.query.assignments('batch',base_oid='new-base',policy_hash='policy')['items']
        self.assertEqual({member for choice in choices for member in choice['member_ids']},{self.members['issue-0'],self.members['issue-2']})
        self.assertTrue(all(choice['input_manifest']['base_oid']=='new-base' for choice in choices))
