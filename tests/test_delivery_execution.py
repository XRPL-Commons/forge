"""Composition, physical leases and logical builder ownership share one authority."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
import tempfile
import hashlib
import json
import unittest

from forge.store.db import Store
from forge.store.delivery import Delivery, DeliveryQueries
from forge.store.models import Command, Candidate, TransitionError, VersionConflict, InvalidDelegationPlan
from forge.store.transitions import Lifecycle, manifest_hash, project_unit


class DeliveryExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory(); self.addCleanup(self.temporary.cleanup)
        self.store=Store.create(self.temporary.name)
        self.life=Lifecycle(self.store,clock=lambda:100.0)
        self.delivery=Delivery(self.store,clock=lambda:100.0)
        self.query=DeliveryQueries(self.store)
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
            children=[dict(key=str(i),member_id=member,scope='Implement member',dependencies=dependencies_in(db,unit,member))
                      for i,member in enumerate(self.members.values())]
        saved=self.delivery.save_plan(self.command(),parent['job_id'],execution_fence='parent-fence',input_manifest=manifest,
                                      plan={'children':children},evidence={'verified':True})
        self.clean(parent,'parent-fence')
        return parent,saved,children

    def child(self,plan,key):
        manifest=self.query.child_manifest(plan['plan_id'],key)
        child=self.delivery.delegate(self.command(),plan['plan_id'],key,slot='child-'+key,execution_fence='fence-'+key,
            input_manifest=manifest,worktree_key='worktree-'+key)
        return child,manifest

    def accept(self,child,manifest,key):
        commit=('a' if key=='0' else 'b')*40
        from forge.store.artifacts import Artifacts
        bundle=b'synthetic retained bundle'
        digest=hashlib.sha256(bundle).hexdigest()
        Artifacts(self.store).put(child['job_id'],'worktree_bundle',bundle)
        Artifacts(self.store).put(child['job_id'],'worker_capture',json.dumps({'bundle':{'digest':digest,'size':len(bundle),'chunks':1}}).encode())
        accepted=self.delivery.accept_part(self.command(),child['job_id'],execution_fence='fence-'+key,input_manifest=manifest,
            commit_oid=commit,evidence={'verified':True,'commit_oid':commit,'base_oid':'base','input_manifest_hash':manifest_hash(manifest),'bundle_digest':digest})
        self.clean(child,'fence-'+key)
        return accepted

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
        with self.assertRaises(TransitionError): self.query.child_manifest(plan['plan_id'],'1')
        runnable=self.query.assignments('batch',base_oid='base',policy_hash='policy')['items']
        self.assertEqual({item['key'] for item in runnable},{'0','2'})
        first,manifest=self.child(plan,'0'); accepted=self.accept(first,manifest,'0')
        next_manifest=self.query.child_manifest(plan['plan_id'],'1')
        self.assertEqual(next_manifest['prerequisite_outputs'][0]['output_id'],accepted['output_id'])
        wrong={**next_manifest,'prerequisite_outputs':[]}
        with self.assertRaises(TransitionError):
            self.delivery.delegate(self.command(),plan['plan_id'],'1',slot='wrong',execution_fence='wrong',input_manifest=wrong,worktree_key='wrong')

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
            manifest=self.query.child_manifest(plan['plan_id'],str(index))
            barrier.wait()
            try:
                return self.delivery.delegate(command,plan['plan_id'],str(index),slot='same-slot',execution_fence='race-fence-'+str(index),
                    input_manifest=manifest,worktree_key='race-'+str(index))
            except VersionConflict:
                return None
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(reserve,range(2)))
        self.assertEqual(sum(result is not None for result in results),1)
        with self.store.read_snapshot() as db: self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],1)

    def test_cancelled_child_result_cannot_advance_or_release_its_lease(self):
        self.compose(); parent,plan,children=self.plan(); child,manifest=self.child(plan,'0')
        self.delivery.cancel_builder(self.command(),evidence={'reason':'operator_cancelled'})
        result=self.delivery.accept_part(self.command(),child['job_id'],execution_fence='fence-0',input_manifest=manifest,
            commit_oid='a'*40,evidence={'verified':True,'commit_oid':'a'*40,'base_oid':'base','input_manifest_hash':manifest_hash(manifest)})
        self.assertFalse(result['accepted'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM part_outputs').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],1)
        self.clean(child,'fence-0')

    def test_plan_commit_replay_and_restart_do_not_duplicate_children(self):
        self.compose(); parent,plan,children=self.plan()
        restarted=Delivery(self.store,clock=lambda:101.0)
        manifest=self.query.child_manifest(plan['plan_id'],'0')
        command=self.command()
        args=dict(slot='child-0',execution_fence='fence-0',input_manifest=manifest,worktree_key='worktree-0')
        first=restarted.delegate(command,plan['plan_id'],'0',**args)
        self.assertEqual(restarted.delegate(command,plan['plan_id'],'0',**args),first)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_delegations').fetchone()[0],1)
        with self.assertRaises(TransitionError):
            restarted.delegate(self.command(),plan['plan_id'],'0',**{**args,'slot':'other','execution_fence':'other','worktree_key':'other'})

    def test_failed_transaction_leaves_no_plan_or_result_and_can_retry(self):
        self.compose(); manifest=self.manifest()
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='parent',input_manifest=manifest)
        command=self.command()
        children=[dict(key=str(i),member_id=member,scope='Implement',dependencies=[]) for i,member in enumerate(self.members.values())]
        from unittest.mock import patch
        with patch.object(self.delivery,'_save',side_effect=RuntimeError('simulated commit interruption')):
            with self.assertRaises(RuntimeError):
                self.delivery.save_plan(command,parent['job_id'],execution_fence='parent',input_manifest=manifest,plan={'children':children},evidence={'verified':True})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM delegation_plans').fetchone()[0],0)
            self.assertIsNone(db.execute('SELECT outcome FROM jobs WHERE id=?',(parent['job_id'],)).fetchone()[0])
        self.assertTrue(self.delivery.save_plan(command,parent['job_id'],execution_fence='parent',input_manifest=manifest,plan={'children':children},evidence={'verified':True})['accepted'])

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
        self.assertEqual([output['member_id'] for output in tree['outputs']],[independent])
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
                self.delivery.save_plan(command,parent['job_id'],execution_fence='parent',input_manifest=manifest,
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
        assignments=self.query.assignments('batch',base_oid='base',policy_hash='policy',limit=1,allowed_members={last['member_id']})
        self.assertEqual([item['member_id'] for item in assignments['items']],[last['member_id']])
