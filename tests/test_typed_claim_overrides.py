"""Operator answers acknowledge exact member claims without releasing siblings."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from forge.factory.store_operator import OperatorActions
from forge.factory.store_views import PipelineViews, action_revision
from forge.store.runtime_requirements import RuntimeRequirements
from forge.store.models import TransitionError
from forge.factory.prerequisites import collect
from tests import test_delivery_pipeline as pipeline
from tests.test_typed_build_results import report, WAIT, DECISION, LARGE


class TypedClaimOverrideTests(unittest.TestCase):
    def setUp(self):
        self.f=pipeline.DeliveryPipelineTests('runTest');self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f
        self.job=f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.members={pin['issue_number']:pin for pin in self.job['members']}
        self.fact=dict(reference='example/repo#10',repository_id=1,github_id=110,number=10,kind='issue',
            state='open',state_reason=None,merged=False,updated_at='2026-10-10T12:00:00Z')
        f.github.prerequisite.side_effect=lambda ref,binding:dict(self.fact)
        self.remote={n:dict(number=n,state='open',labels=[],assignees=[]) for n in (1,2)}
        f.github.issue.side_effect=lambda item:(deepcopy(self.remote[item['number']]),'req-'+str(item['number']))
        def call(role,item,method,path,payload=None):
            if method=='POST' and path.endswith('/labels'):
                self.remote[item['number']]['labels']=sorted(set(self.remote[item['number']]['labels'])|set(payload['labels']))
            else:raise AssertionError((method,path))
            return deepcopy(self.remote[item['number']])
        f.github.call.side_effect=call
        self.actions=OperatorActions(f.store,f.github,actor_id='github:42',config=f.config,clock=lambda:100)
        self.views=PipelineViews(f.store,runtime=f.config,clock=lambda:100)

    def capture(self,claims):
        f=self.f;job=self.job
        f.executions.collect(f.command(),job['id'],execution_fence=job['execution_fence'],capture={'result':report(job,claims),'logs':''})
        f.life.verify_cleanup(f.command(),job['id'],execution_fence=job['execution_fence'],evidence={'process_stopped':True,'resources_released':True})
        self.assertTrue(f.results.step(job['id'])['accepted'])

    def request(self,number,action):
        key='issue-'+str(number)
        with self.f.store.read_snapshot() as db:revision=action_revision(db,key)
        identifier=self.actions.request(key,action,revision,'Acknowledged for this issue')
        self.assertTrue(self.actions.step(identifier)['applied'])
        return identifier

    def detail(self,number):
        return self.views.issues(key='issue-'+str(number))['issue']

    def gates(self,code):
        with self.f.store.read_snapshot() as db:
            return [dict(row) for row in db.execute('SELECT * FROM unit_gates WHERE code=? AND resolved_at IS NULL',(code,))]

    def test_start_anyway_is_visible_and_dismisses_only_selected_pin(self):
        self.capture([{**WAIT,'issue_number':10}])
        old=self.gates('dependency')[0]
        self.assertIn('start_anyway',self.detail(1)['actions'])
        self.assertEqual(self.detail(1)['prerequisites'][0]['reference'],'example/repo#10')
        self.request(1,'start_anyway')
        remaining=self.gates('dependency')
        self.assertEqual(len(remaining),1)
        self.assertNotEqual(remaining[0]['id'],old['id'])
        self.assertEqual(json.loads(remaining[0]['details_json'])['requirements_by_member'],
            {self.members[2]['member_id']:self.members[2]['requirements_revision_id']})
        self.assertNotIn('start_anyway',self.detail(1)['actions'])
        self.assertIn('start_anyway',self.detail(2)['actions'])
        for number,expected in [(1,[]),(2,[self.fact])]:
            pin=self.members[number]
            observed=collect(self.f.store,self.f.github,unit_id='batch',member_id=pin['member_id'],requirements_revision_id=pin['requirements_revision_id'],body='Scope')
            self.assertEqual(observed['facts'],expected)
        self.request(2,'start_anyway')
        self.assertEqual(self.gates('dependency'),[])

    def test_explicit_issue_body_dependency_and_independent_gate_survive(self):
        self.capture([{**WAIT,'issue_number':10}])
        member=self.members[1]
        independent=self.f.life.open_gate(self.f.command(),'dependency',member_id=member['member_id'],details={'source':'runtime_dependencies','blocked':['explicit']})['gate_id']
        self.request(1,'start_anyway')
        self.assertIn(independent,[gate['id'] for gate in self.gates('dependency')])
        observed=collect(self.f.store,self.f.github,unit_id='batch',member_id=member['member_id'],requirements_revision_id=member['requirements_revision_id'],body='Depends on: #10')
        self.assertEqual(observed['facts'],[self.fact])

    def test_stale_member_revision_cannot_acknowledge_a_prior_claim(self):
        self.capture([{**WAIT,'issue_number':10}])
        member=self.members[1]
        self.f.life.observe_issue(self.f.command(),'issue-1',state='open',title='Changed',body='Changed',requirements='new',remote_updated_at='2026-10-14T12:00:00Z',generation=1)
        from forge.store.claim_overrides import dependencies_in
        with self.f.store.read_snapshot() as db:
            self.assertEqual(dependencies_in(db,'batch',member['member_id'],member['requirements_revision_id']),[])
        self.assertNotIn('start_anyway',self.detail(1)['actions'])
        with self.assertRaises(TransitionError):
            with self.f.store.read_snapshot() as db:revision=action_revision(db,'issue-1')
            self.actions.request('issue-1','start_anyway',revision,'Stale answer')
        self.assertEqual(len(self.gates('dependency')),1)

    def test_cosmetic_issue_edit_keeps_exact_requirement_claim_actionable(self):
        self.capture([{**WAIT,'issue_number':10}])
        self.f.life.observe_issue(self.f.command(),'issue-1',state='open',title='Clearer title',body='Reworded',requirements='req-1',remote_updated_at='2026-10-14T12:00:00Z',generation=1)
        self.assertIn('start_anyway',self.detail(1)['actions'])
        self.request(1,'start_anyway')
        self.assertEqual(len(self.gates('dependency')),1)

    def test_retry_shows_and_records_both_claims_without_acknowledging_sibling(self):
        self.capture([DECISION,LARGE])
        value=self.detail(1)
        self.assertIn('retry',value['actions'])
        self.assertEqual(value['decision_request']['question'],DECISION['question'])
        self.assertIn(DECISION['evidence'],value['decision_request']['evidence'])
        self.assertIn(LARGE['reason'],value['decision_request']['evidence'])
        self.assertIn('this issue only',value['decision_request']['acknowledgment_scope'])
        request=self.request(1,'retry')
        gates=self.gates('decision_required');self.assertEqual(len(gates),1)
        self.assertEqual(json.loads(gates[0]['details_json'])['requirements_by_member'],
            {self.members[2]['member_id']:self.members[2]['requirements_revision_id']})
        self.assertIsNone(self.detail(1)['decision_request'])
        self.assertIsNotNone(self.detail(2)['decision_request'])
        with self.f.store.read_snapshot() as db:
            rows=[json.loads(row[0]) for row in db.execute("SELECT payload_json FROM events WHERE kind='gate_resolved'")]
            receipt=next(row['evidence'] for row in rows if row['evidence'].get('request_id')==request and row['evidence'].get('claim_types'))
        self.assertEqual(receipt['claim_types'],['needs_decision','too_large'])
        self.assertEqual(receipt['acknowledged_claims'],[DECISION,LARGE])
        self.assertEqual(receipt['acknowledged_member_id'],self.members[1]['member_id'])

    def test_another_members_unaccepted_requirements_change_keeps_its_claim(self):
        self.capture([{**WAIT,'issue_number':10}])
        self.f.life.observe_issue(self.f.command(),'issue-2',state='open',title='Changed',body='Changed',requirements='new',remote_updated_at='2026-10-14T12:00:00Z',generation=1)
        self.request(1,'start_anyway')
        gate=self.gates('dependency')[0]
        self.assertEqual(json.loads(gate['details_json'])['requirements_by_member'],
            {self.members[2]['member_id']:self.members[2]['requirements_revision_id']})

    def test_legacy_same_requirements_on_another_member_are_not_cleared(self):
        self.capture([DECISION])
        first,second=self.members[1],self.members[2]
        gate=self.f.life.open_gate(self.f.command(),'dependency',member_id=second['member_id'],details={'requirements':'req-1','issues':[10]})['gate_id']
        RuntimeRequirements(self.f.store,clock=lambda:100).clear_declared_dependencies(self.f.command(),requirements_revision_id=first['requirements_revision_id'],evidence={'reason':'Only first'})
        self.assertIn(gate,[g['id'] for g in self.gates('dependency')])
