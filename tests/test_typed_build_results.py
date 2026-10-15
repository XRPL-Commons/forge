"""Typed worker claims retain exact authority across capture and observation."""
from copy import deepcopy
import json
import unittest
from unittest.mock import Mock

from forge.factory.build_results import schema, validate
from forge.factory.contracts import result
from forge.factory.github import GitHubUnavailable
from forge.factory.ownership import linked_pulls, ownership
from forge.factory.prerequisites import references
from forge.factory.environments import choose
from forge.store.runtime_requirements import steward_environment_in
from tests import test_store_runtime_requirements as runtime
from tests import test_store_result_controller as results


def report(job, claims=(), **updates):
    return dict(version=2,attempt=job['id'],role='build',outcome='cannot_complete' if claims else 'done',
        summary='Captured findings',headline='Work needs a choice',plain='The implementation needs a decision.',
        operator_summary='The implementation needs a decision.',claims=list(claims),verification=None,
        delegation_plan=None,output_commit=None,pr_title=None,pr_body=None,**updates)


WAIT={'type':'wait_on_issue','repository_id':1,'issue_number':2,'reason':'The interface is implemented there'}
DECISION={'type':'needs_decision','question':'Which interface?','options':['Stable','Experimental'],
          'recommendation':'Stable','evidence':'Both versions are supported by the client'}
LARGE={'type':'too_large','reason':'The requested rewrite spans independently owned services'}
ENV={'type':'environment','environment_id':'full','services':['postgres'],'reason':'services','evidence':'The integration check requires postgres'}


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.job={'id':'one','role':'build','repo_id':1,'issue_number':1,'runner_config':{'environment_tiers':{'standard':{},'full':{}}}}

    def test_typed_claims_validate_without_requirements_quotes_or_summary_keywords(self):
        value=report(self.job,[WAIT,DECISION,LARGE,ENV]);value['summary']='GH013 workflows signed commits'
        checked=result(value,self.job)
        self.assertEqual(checked['claims'],value['claims'])
        self.assertEqual(checked['outcome'],'cannot_complete')
        for change in ({'outcome':'done'},{'output_commit':'a'*40},{'verification':{'what':'X','why':'Y'}},
                       {'claims':[WAIT,WAIT]},{'claims':[{**WAIT,'issue_number':1}]},
                       {'claims':[{**ENV,'environment_id':'unknown'}]},
                       {'claims':[{**ENV,'reason':'I need a database'}]}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate({**value,**change},self.job)

    def test_planning_done_is_plan_only_and_schema_has_only_two_outcomes(self):
        job={**self.job,'builder_stage':'planning'}
        value=report(job);value['delegation_plan']={'version':2,'tasks':[],'retained_task_ids':[],'owner_final':{'member_ids':['m'],'scope':'Implement'}}
        self.assertEqual(validate(value,job)['outcome'],'done')
        self.assertEqual(schema(job)['properties']['outcome']['enum'],['done','cannot_complete'])
        with self.assertRaises(ValueError):validate({**value,'output_commit':'a'*40},job)
        value=report(self.job);value['output_commit']='a'*40
        with self.assertRaises(ValueError):validate(value,self.job)

    def test_fresh_build_cannot_downgrade_to_legacy_result(self):
        legacy={'version':1,'attempt':'one','role':'build','outcome':'pass','summary':'Done'}
        self.assertEqual(result(legacy,self.job)['outcome'],'pass')
        with self.assertRaises(ValueError):result(legacy,{**self.job,'build_result_version':2})

    def test_dependency_override_is_exact_and_ignores_incidental_prose(self):
        self.assertEqual(references('Depends on #2\n#3 must be merged first\nSTART #4\nNeeds #5','owner/repo'),[])
        self.assertEqual(references('Depends on: #2, Other/Repo#3\n```\nDepends on: #4\n```\n> Depends on: #5','owner/repo'),['other/repo#3','owner/repo#2'])
        self.assertEqual(references('Depends on: #2 and #3','owner/repo'),[])


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.item={'repo':'owner/repo','repo_id':1,'number':2}
        self.pr={'id':123,'number':4,'state':'open','author_association':'MEMBER','body':'Fixes #999',
                 'user':{'id':7,'login':'maintainer','type':'User'}}
        self.github=Mock()

    def page(self,refs=(),more=False,cursor=None):
        return {'repository':{'databaseId':1,'pullRequest':{'databaseId':123,'number':4,'authorAssociation':'MEMBER',
            'closingIssuesReferences':{'nodes':[{'number':n,'repository':{'databaseId':r}} for r,n in refs],
                'pageInfo':{'hasNextPage':more,'endCursor':cursor}}}}}

    def test_complete_typed_links_take_ownership_and_prose_does_not(self):
        self.github.graphql.side_effect=[self.page([(9,2)],True,'page2'),self.page([(1,2)])]
        rows=linked_pulls(self.github,self.item,[self.pr])
        self.assertEqual(self.github.graphql.call_count,2)
        self.assertIsNotNone(ownership({'number':2},self.item,'owner/repo',rows))
        self.assertIsNone(ownership({'number':999},self.item,'owner/repo',rows))
        self.assertEqual(self.github.graphql.call_args.args[3]['cursor'],'page2')

    def test_noncollaborator_or_bot_never_claims_work_and_unknown_links_raise(self):
        for pr in ({**self.pr,'author_association':'CONTRIBUTOR'},{**self.pr,'user':{'login':'build[bot]','type':'Bot'}}):
            self.assertIsNone(ownership({'number':2},self.item,'owner/repo',linked_pulls(self.github,self.item,[pr])))
        self.github.graphql.assert_not_called()
        with self.assertRaises(GitHubUnavailable):ownership({'number':2},self.item,'owner/repo',[self.pr])
        for response in ({},self.page([(1,2)],True,None),{'repository':{**self.page()['repository'],'databaseId':9}}):
            self.github.graphql.return_value=response
            with self.subTest(response=response),self.assertRaises(GitHubUnavailable):linked_pulls(self.github,self.item,[self.pr])


class ActiveClaimTests(unittest.TestCase):
    setUp=results.ResultControllerTests.setUp
    command=results.ResultControllerTests.command
    reserve=results.ResultControllerTests.reserve
    capture=results.ResultControllerTests.capture
    view=results.ResultControllerTests.view
    charges=results.ResultControllerTests.charges

    def fact(self,state='open',**updates):
        return dict(reference='owner/repo#2',repository_id=1,github_id=102,number=2,kind='issue',
            state=state,state_reason='completed' if state=='closed' else None,merged=False,updated_at='2026-10-08T01:00:00Z',**updates)

    def test_all_independent_claims_survive_capture_and_dependency_completion(self):
        job=self.reserve();self.github.prerequisite=Mock(return_value=self.fact())
        self.capture(job,report(self.queries.job(job),[WAIT,DECISION,LARGE]))
        accepted=self.controller.step(job)
        self.assertTrue(accepted['accepted']);self.assertFalse(accepted['charged'])
        with self.store.read_snapshot() as db:
            gates=[dict(r) for r in db.execute('SELECT * FROM unit_gates WHERE resolved_at IS NULL')]
            self.assertEqual({g['code'] for g in gates},{'decision_required','dependency'})
            decision=next(g for g in gates if g['code']=='decision_required')
            self.assertEqual(json.loads(decision['details_json'])['claims'],[DECISION,LARGE])
            self.assertEqual({g['job_id'] for g in gates},{job})
        from forge.store.runtime_requirements import RuntimeRequirements
        RuntimeRequirements(self.store,clock=lambda:100).dependencies(self.command(),requirements_revision_id=self.manifest['requirements_revision_id'],generation=1,facts=[self.fact('closed')])
        with self.store.read_snapshot() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT code FROM unit_gates WHERE resolved_at IS NULL')],['decision_required'])
        self.assertEqual(self.controller.step(job)['outcome'],'deferred');self.assertEqual(self.charges(),0)

    def test_done_still_requires_independent_review_and_audit(self):
        job=self.reserve()
        results.ResultControllerTests.publish(self,job)
        self.capture(job,report(self.queries.job(job)))
        self.assertTrue(self.controller.step(job)['accepted'])
        self.assertEqual(self.view()['state'],'reviewing')
        for role,state in [('review','auditing'),('audit','merging')]:
            identity=self.reserve(role)
            self.capture(identity,results.ResultControllerTests.report(self,identity))
            self.assertTrue(self.controller.step(identity)['accepted'])
            self.assertEqual(self.view()['state'],state)
        self.assertEqual([role for _,role in self.verdicts.calls],['pass','pass'])

    def test_remote_closed_or_wrong_identity_cannot_create_worker_wait(self):
        for fact,expected in [(self.fact('closed'),'failure'),({**self.fact(),'repository_id':3},'unknown')]:
            self.setUp();job=self.reserve();self.github.prerequisite=Mock(return_value=fact)
            self.capture(job,report(self.queries.job(job),[WAIT]))
            if expected=='unknown':
                with self.assertRaises(GitHubUnavailable):self.controller.step(job)
                self.assertEqual(self.charges(),0)
            else:self.assertTrue(self.controller.step(job)['charged'])

    def test_independent_member_decision_is_preserved(self):
        job=self.reserve();self.capture(job,report(self.queries.job(job),[DECISION,LARGE]))
        gate=self.life.open_gate(self.command(),'decision_required',member_id=self.member,details={'source':'operator','question':'Keep my independent choice'})['gate_id']
        self.assertTrue(self.controller.step(job)['accepted'])
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(gate,)).fetchone()[0])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL").fetchone()[0],2)


class StewardEnvironmentTests(unittest.TestCase):
    setUp=runtime.RuntimeRequirementsTests.setUp
    command=runtime.RuntimeRequirementsTests.command
    snapshot=runtime.RuntimeRequirementsTests.snapshot

    def test_brief_choice_uses_existing_projection_and_operator_override_wins(self):
        env={'environment_id':'full','services':['postgres']}
        command=self.command()
        with self.store.write() as db:
            unit=dict(db.execute("SELECT * FROM units WHERE id='u'").fetchone())
            steward_environment_in(self.commands,db,command,unit,env,self.config)
        with self.store.read_snapshot() as db:
            choice=json.loads(db.execute('SELECT choice FROM unit_environment ORDER BY revision DESC LIMIT 1').fetchone()[0])
            self.assertEqual((choice['tier'],choice['services'],choice['source']),('full',['postgres'],'steward'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM environment_label_effects').fetchone()[0],1)
        item={'repo_id':1,'environment_choice':choice,'environment_label':'env:full'}
        self.assertEqual(choose(self.config,item,{'labels':['env:full']})['services'],['postgres'])
        item['environment_retry']={'services':['redis']}
        self.assertEqual(choose(self.config,item,{'labels':['env:full']})['services'],['redis'])
        self.commands.set_override(self.command(),requirements_revision_id=self.revision,override={'mode':'standard','services':[]})
        from forge.store.models import TransitionError
        with self.store.write() as db,self.assertRaises(TransitionError):
            unit=dict(db.execute("SELECT * FROM units WHERE id='u'").fetchone())
            steward_environment_in(self.commands,db,self.command(),unit,env,self.config)


class CombinedEnvironmentClaimsTests(unittest.TestCase):
    setUp=runtime.RuntimeRequirementsTests.setUp
    command=runtime.RuntimeRequirementsTests.command
    snapshot=runtime.RuntimeRequirementsTests.snapshot
    counts=runtime.RuntimeRequirementsTests.counts

    def test_retest_releases_only_environment_claim_with_other_claims_and_independent_hold(self):
        from forge.factory.result_controller import ResultController
        from forge.factory.wait_controller import WaitController
        job_id=self.dispatch.step('u')['job_id'];job=self.jobs.job(job_id)
        value=report(job,[ENV,DECISION,LARGE,WAIT])
        self.github.dependencies[2]={'state':'open','updated_at':'2026-10-08T01:00:00Z'}
        self.commands.collect(self.command(),job_id,execution_fence=job['execution_fence'],capture={'result':value,'logs':''})
        self.commands.verify_cleanup(self.command(),job_id,execution_fence=job['execution_fence'],evidence={'process_stopped':True,'resources_released':True})
        independent=self.commands.open_gate(self.command(),'environment',member_id=self.member,details={'source':'runtime_selection','reason':'Independent operator selection'})['gate_id']
        controller=ResultController(self.store,self.github,policy=lambda _:job['policy'],requirements=self.controller,clock=lambda:self.now)
        outcome=controller.step(job_id)
        self.assertTrue(outcome['accepted']);self.assertFalse(outcome['charged'])
        with self.store.read_snapshot() as db:
            gates=[dict(row) for row in db.execute('SELECT * FROM unit_gates WHERE resolved_at IS NULL')]
            claim=next(g for g in gates if g['code']=='environment' and g['id']!=independent)
            self.assertEqual(claim['job_id'],job_id)
            self.assertEqual({g['code'] for g in gates},{'environment','dependency','decision_required'})
        self.now=400
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(claim['id'])['resolved'])
        with self.store.read_snapshot() as db:
            remaining=[dict(row) for row in db.execute('SELECT * FROM unit_gates WHERE resolved_at IS NULL')]
            self.assertIn(independent,[g['id'] for g in remaining])
            self.assertEqual({g['code'] for g in remaining},{'environment','dependency','decision_required'})
        self.assertEqual(self.counts(),(0,0))


class GroupedClaimTests(unittest.TestCase):
    def test_one_captured_claim_pins_every_member_and_reopening_is_observed(self):
        from tests.test_delivery_pipeline import DeliveryPipelineTests
        from forge.factory.prerequisites import collect
        from forge.store.runtime_requirements import RuntimeRequirements
        f=DeliveryPipelineTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        job=f.jobs.job(f.dispatch.step('batch')['job_id'])
        fact=dict(reference='example/repo#10',repository_id=1,github_id=110,number=10,kind='issue',state='open',state_reason=None,merged=False,updated_at='2026-10-10T12:00:00Z')
        f.github.prerequisite.side_effect=lambda ref,binding:dict(fact)
        value=report(job,[{**WAIT,'issue_number':10}])
        f.executions.collect(f.command(),job['id'],execution_fence=job['execution_fence'],capture={'result':value,'logs':''})
        f.life.verify_cleanup(f.command(),job['id'],execution_fence=job['execution_fence'],evidence={'process_stopped':True,'resources_released':True})
        outcome=f.results.step(job['id']);self.assertTrue(outcome['accepted'])
        commands=RuntimeRequirements(f.store,clock=lambda:101)
        with f.store.read_snapshot() as db:
            details=json.loads(db.execute("SELECT details_json FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL").fetchone()[0])
            self.assertEqual(details['requirements_by_member'],{p['member_id']:p['requirements_revision_id'] for p in job['members']})
        for state,generation in [('closed',1),('open',2)]:
            fact.update(state=state,state_reason='completed' if state=='closed' else None)
            for pin in job['members']:
                observation=collect(f.store,f.github,unit_id='batch',member_id=pin['member_id'],requirements_revision_id=pin['requirements_revision_id'],body='No dependency quote')
                self.assertEqual(observation['facts'],[fact])
                self.assertEqual(commands.dependencies(f.command(),requirements_revision_id=pin['requirements_revision_id'],generation=generation,facts=observation['facts'])['clear'],state=='closed')
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL").fetchone()[0],2)
