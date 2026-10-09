"""Member observations and merge authority use the same complete delivery scope."""
from copy import deepcopy
import unittest

from forge.factory.config import policy
from forge.factory.observation_controller import ObservationController
from forge.factory.merge_controller import MergeAdmission, candidate_snapshot
from forge.store.transitions import manifest_hash
from tests.test_delivery_pipeline import DeliveryPipelineTests


class GroupedLifecycleTests(unittest.TestCase):
    # Reuse the real Git/SQLite pipeline fixture, without rerunning its own tests.
    dependencies=()
    setUp=DeliveryPipelineTests.setUp
    git=DeliveryPipelineTests.git
    dispatcher=DeliveryPipelineTests.dispatcher
    command=DeliveryPipelineTests.command
    capture=DeliveryPipelineTests.capture
    child_bundle=DeliveryPipelineTests.child_bundle
    integrate=DeliveryPipelineTests.integrate

    def test_hold_fences_only_affected_child_and_resumes_from_observation(self):
        parent=self.dispatch.step('batch')['job_id']
        planning=self.jobs.job(parent)
        children=[dict(key='part-'+str(n),member_id=m['member_id'],scope='Implement',dependencies=[]) for n,m in enumerate(planning['members'])]
        self.capture(parent,outcome='delegate',delegation_plan={'children':children})
        jobs=[self.dispatch.step('batch')['job_id'],self.dispatch.step('batch')['job_id']]
        held_number=self.jobs.job(jobs[0])['issue_number']
        held=[True]
        def issue(item):
            return dict(number=item['number'],state='open',title='Issue',body='Scope',updated_at='2026-10-10T00:00:00Z',
                assignees=[],labels=['hold'] if held[0] and item['number']==held_number else []),'req-'+str(item['number'])
        self.github.issue.side_effect=issue
        observer=ObservationController(self.store,self.github,admission=lambda _:True,
            policy=lambda item:manifest_hash(policy(self.config,1,repository_name='example/repo')),clock=lambda:100)
        observer.step('batch')
        self.assertTrue(self.jobs.job(jobs[0])['fenced'])
        self.assertFalse(self.jobs.job(jobs[1])['fenced'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],2)
        held[0]=False
        observer.step('batch')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='label_hold' AND resolved_at IS NULL").fetchone()[0],0)

    def test_merge_checks_all_member_requirements_and_holds(self):
        self.verdicts.ensure.return_value={'check_id':7}
        DeliveryPipelineTests.test_two_parallel_children_resume_owner_and_publish_one_reviewed_candidate(self)
        self.config['merge_enabled']=True
        item=candidate_snapshot(self.store,'batch')
        self.assertIsNone(item['number'])
        self.assertEqual(len(item['members']),2)
        changed=[False];held=[False]
        def issue(scoped):
            return dict(number=scoped['number'],state='open',labels=['hold'] if held[0] and scoped['number']==2 else [],assignees=[]),('changed' if changed[0] and scoped['number']==2 else 'req-'+str(scoped['number']))
        self.github.issue.side_effect=issue
        self.github.target.return_value='main'
        self.github.protections.return_value=True
        self.github.gates.return_value=True
        self.github.approvals.return_value=[]
        identity=item['identity']
        self.github.pr.return_value=dict(number=identity['pull_number'],state='open',draft=False,merged=False,mergeable=True,
            head={'sha':identity['head_oid'],'repo':{'id':1}},base={'sha':identity['base_oid'],'ref':'main','repo':{'id':1}})
        admission=MergeAdmission(self.store,self.github,config=lambda:deepcopy(self.config),repository_ready=lambda _:True,clock=lambda:100)
        effect={'unit_id':'batch','candidate_id':identity['id']}
        self.assertIsNotNone(admission(effect))
        changed[0]=True
        self.assertIsNone(admission(effect))
        changed[0]=False;held[0]=True
        self.assertIsNone(admission(effect))
        held[0]=False
        from forge.factory.merge_controller import MergeController
        from forge.factory.effect_controller import EffectController
        queued=MergeController(self.store,admission=admission,clock=lambda:100).step('batch')
        self.github.merge.assert_not_called()
        def merge(_effect):
            self.github.pr.return_value.update(merged=True,state='closed',merge_commit_sha='f'*40)
        self.github.merge.side_effect=merge
        finished=EffectController(self.store,self.github,admission=admission,clock=lambda:100).step(queued['effect_id'])
        self.assertEqual(finished['status'],'confirmed')
        self.github.merge.assert_called_once()
        with self.store.read_snapshot() as db:
            self.assertEqual(tuple(db.execute("SELECT state,merge_sha FROM units WHERE id='batch'").fetchone()),('merged','f'*40))
            self.assertEqual({row[0] for row in db.execute("SELECT state FROM unit_issues WHERE unit_id='batch'")},{'done'})


if __name__=='__main__':unittest.main()
