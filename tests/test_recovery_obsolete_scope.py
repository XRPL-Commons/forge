"""Mandatory diagnostics survive closure without reviving implementation authority."""
from pathlib import Path
import unittest
from unittest.mock import Mock

from forge.factory.execution_controller import ExecutionController
from forge.factory.harness import checkout_recovery, PreparationError
from forge.factory.recovery_controller import RecoveryController
from forge.store.models import Candidate, TransitionError
from forge.store.recovery import pending_mandatory
from tests import test_delivery_pipeline as delivery_fixture
from tests import test_store_recovery as store_fixture
from tests import test_store_recovery_controller as runner_fixture


class ObsoleteMemberRecoveryTests(unittest.TestCase):
    dependencies=()
    setUp=delivery_fixture.DeliveryPipelineTests.setUp
    git=delivery_fixture.DeliveryPipelineTests.git
    dispatcher=delivery_fixture.DeliveryPipelineTests.dispatcher
    command=delivery_fixture.DeliveryPipelineTests.command
    capture=delivery_fixture.DeliveryPipelineTests.capture

    def failed_child(self):
        parent=self.dispatch.step('batch')['job_id']
        children=[dict(key=str(index),executor='child',member_ids=[member['member_id']],scope='Implement member',depends_on=[],internalizes=[])
                  for index,member in enumerate(self.jobs.job(parent)['members'])]
        self.capture(parent,outcome='delegate',delegation_plan=delivery_fixture.DeliveryPipelineTests.plan_value(children))
        identifier=self.dispatch.step('batch')['job_id']
        self.assertTrue(self.capture(identifier,outcome='unable')['charged'])
        return self.jobs.job(identifier)

    def change(self,job,*,closed=False):
        number=job['issue_number']
        requirements=job['requirements'] if closed else 'changed requirements'
        body='Scope' if closed else 'Changed scope'
        self.life.observe_issue(self.command(),job['issue_key'],state='closed' if closed else 'open',
            state_reason='completed' if closed else None,title='Issue',body=body,requirements=requirements,
            remote_updated_at='2026-10-10T00:00:00Z',generation=1)
        if closed:
            self.life.open_gate(self.command(),'membership_decision',member_id=job['members'][0]['member_id'],
                details={'reason':'member_closed','state_reason':'completed'})
        self.github.issue.side_effect=lambda item: (
            {'number':item['number'],'state':'closed' if closed and item['number']==number else 'open',
             'body':body if item['number']==number else 'Scope','labels':[],'assignees':[]},
            requirements if item['number']==number else 'req-'+str(item['number']))

    def investigate(self,failed):
        self.github.token.return_value='ephemeral-token'
        runner=runner_fixture.Runner(self.github)
        recovery=RecoveryController(self.store,runner,self.github,config=self.config,admission=lambda _:True,clock=lambda:100)
        result=recovery.schedule(failed['id'])
        self.assertTrue(result.get('reserved'),result)
        identifier=result['job_id']
        self.assertTrue(self.jobs.job(identifier)['launch_allowed'])
        execution=ExecutionController(self.store,runner,self.github,enabled=lambda:True,admission=lambda _:True,clock=lambda:100)
        self.assertTrue(execution.step(identifier)['cleanup'])
        finished=recovery.finish(identifier)
        self.assertTrue(finished['accepted'],finished)
        note=runner.notes[0]
        self.assertEqual(note['failure']['input_manifest'],failed['input_manifest'])
        self.assertEqual(note['members'][0]['requirements'],failed['requirements'])
        self.assertEqual(note['members'][0]['body'],'Scope')
        self.github.publication.assert_not_called()
        with self.store.read_snapshot() as db:
            self.assertFalse(pending_mandatory(db,'batch'))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='charge'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='correction'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM recovery_findings WHERE incomplete=0').fetchone()[0],1)
            self.assertIsNone(db.execute("SELECT current_candidate_id FROM units WHERE id='batch'").fetchone()[0])
            self.assertIsNone(db.execute("SELECT retry_plan_json FROM unit_context WHERE unit_id='batch'").fetchone()[0])
        return recovery

    def test_closed_child_is_investigated_then_explicit_close_member_can_finish(self):
        failed=self.failed_child();self.change(failed,closed=True)
        with self.assertRaisesRegex(TransitionError,'mandatory investigations'):
            self.delivery.drop_member(self.command(),failed['members'][0]['member_id'],membership_generation=failed['membership_generation'],
                dependent_decisions={},evidence={'approved':True},disposition='close')
        self.investigate(failed)
        result=self.delivery.drop_member(self.command(),failed['members'][0]['member_id'],membership_generation=failed['membership_generation'],
            dependent_decisions={},evidence={'approved':True},disposition='close')
        self.assertEqual(result['closed_issue_id'],failed['issue_key'])

    def test_changed_requirements_are_investigated_before_explicit_dissolve(self):
        failed=self.failed_child();self.change(failed)
        with self.assertRaisesRegex(TransitionError,'mandatory investigations'):
            self.life.dissolve(self.command(),evidence={'approved':True})
        self.investigate(failed)
        self.life.dissolve(self.command(),evidence={'approved':True})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='batch'").fetchone()[0],'dissolved')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='charge'").fetchone()[0],1)

    def test_closed_member_still_respects_hold_and_human_ownership(self):
        failed=self.failed_child();self.change(failed,closed=True)
        recovery=RecoveryController(self.store,self.runner,self.github,config=self.config,admission=lambda _:True,clock=lambda:100)
        remote,requirements=self.github.issue({'number':failed['issue_number']})
        for change in ({'labels':['hold']},{'assignees':[{'login':'person','type':'User'}]}):
            with self.subTest(change=change):
                self.github.issue.side_effect=None
                self.github.issue.return_value=(remote|change,requirements)
                self.assertEqual(recovery.schedule(failed['id'])['pending'],'issue_reconciliation')
        with self.store.read_snapshot() as db:
            self.assertTrue(pending_mandatory(db,'batch'))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM recovery_assignments').fetchone()[0],0)


class OriginalOutputRecoveryTests(unittest.TestCase):
    dependencies=({'issue_id':'issue-2','prerequisite_issue_id':'issue-1'},)
    setUp=delivery_fixture.DeliveryPipelineTests.setUp
    git=delivery_fixture.DeliveryPipelineTests.git
    dispatcher=delivery_fixture.DeliveryPipelineTests.dispatcher
    command=delivery_fixture.DeliveryPipelineTests.command
    capture=delivery_fixture.DeliveryPipelineTests.capture
    child_bundle=delivery_fixture.DeliveryPipelineTests.child_bundle

    def test_invalidated_predecessor_still_supplies_exact_archived_diagnostic_bundle(self):
        from forge.store.delivery_inputs import dependencies_in
        parent=self.dispatch.step('batch')['job_id']
        planning=self.jobs.job(parent)
        with self.store.read_snapshot() as db:
            unit=dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
            keys={member['member_id']:str(member['issue_number']) for member in planning['members']}
            children=[dict(key=keys[member['member_id']],executor='child',member_ids=[member['member_id']],scope='Implement member',
                depends_on=[keys[predecessor] for predecessor in dependencies_in(db,unit,member['member_id'])], internalizes=[]) for member in planning['members']]
        self.capture(parent,outcome='delegate',delegation_plan=delivery_fixture.DeliveryPipelineTests.plan_value(children))
        first=self.dispatch.step('batch')['job_id']
        self.assertEqual(self.jobs.job(first)['issue_number'],1)
        head,bundle=self.child_bundle(1)
        self.assertTrue(self.capture(first,output_commit=head,bundle=bundle)['accepted'])
        failed_id=self.dispatch.step('batch')['job_id']
        self.assertEqual(self.jobs.job(failed_id)['issue_number'],2)
        self.assertTrue(self.capture(failed_id,outcome='unable')['charged'])
        failed=self.jobs.job(failed_id)
        self.life.observe_issue(self.command(),'issue-1',state='open',title='Issue',body='Changed scope',requirements='changed',
            remote_updated_at='2026-10-10T00:00:00Z',generation=1)
        self.github.token.return_value='ephemeral-token'
        runner=runner_fixture.Runner(self.github)
        recovery=RecoveryController(self.store,runner,self.github,config=self.config,admission=lambda _:True,clock=lambda:100)
        identifier=recovery.schedule(failed_id)['job_id']
        self.assertTrue(self.jobs.job(identifier)['launch_allowed'])
        execution=ExecutionController(self.store,runner,self.github,enabled=lambda:True,admission=lambda _:True,clock=lambda:100)
        self.assertTrue(execution.step(identifier)['cleanup'])
        # Source preparation owns bundle loading now; recovery must retain the
        # exact archived references even after predecessor membership changes.
        received=list(self.jobs.input_bundles(self.jobs.input_refs(self.jobs.job(identifier))))
        self.assertEqual(len(received),1)
        self.assertEqual(received[0]['commit_oid'],head)
        self.assertEqual(received[0]['content'],bundle)
        self.assertEqual(runner.notes[0]['failure']['input_manifest'],failed['input_manifest'])
        self.assertTrue(recovery.finish(identifier)['accepted'])
        with self.store.read_snapshot() as db:
            self.assertFalse(pending_mandatory(db,'batch'))
            self.assertIsNotNone(db.execute("SELECT 1 FROM unit_gates WHERE code='membership_decision' AND resolved_at IS NULL").fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='correction'").fetchone()[0],0)


class ClosedPublisherRecoveryTests(unittest.TestCase):
    setUp=store_fixture.RecoveryStoreTests.setUp
    command=store_fixture.RecoveryStoreTests.command
    view=store_fixture.RecoveryStoreTests.view
    launch=store_fixture.RecoveryStoreTests.launch
    capture_cleanup=store_fixture.RecoveryStoreTests.capture_cleanup
    fail=store_fixture.RecoveryStoreTests.fail
    reserve_recovery=store_fixture.RecoveryStoreTests.reserve_recovery
    findings=staticmethod(store_fixture.RecoveryStoreTests.findings)

    def test_store_cannot_refund_or_select_publication_for_closed_issue(self):
        failed=self.fail()
        self.life.observe_issue(self.command(),'i',state='closed',state_reason='completed',title='Issue',body='',
            requirements='requirements',remote_updated_at='2026-10-10T00:00:00Z',generation=1)
        recovery=self.reserve_recovery(failed)
        recovery=self.capture_cleanup(recovery['id'])
        answer=self.life.recover_publication(self.command(),recovery['id'],execution_fence=recovery['execution_fence'],
            input_manifest=recovery['input_manifest'],findings=self.findings(),
            candidate=Candidate(1,'forge/issue-1','head','base','policy','requirements','inputs'),
            evidence={'verified':True})
        self.assertTrue(answer['accepted']);self.assertFalse(answer['corrected'])
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.view()['charged_failures'],1)
        with self.store.read_snapshot() as db:self.assertFalse(pending_mandatory(db,'u'))


class RecoveryCheckoutTests(unittest.TestCase):
    def test_failed_head_then_failed_base_are_exact_checkout_authority(self):
        for manifest,expected in [({'head_oid':'a'*40,'base_oid':'b'*40},'a'*40),({'base_oid':'b'*40},'b'*40)]:
            git=Mock()
            checkout_recovery({'failure':{'input_manifest':manifest},'candidate':{'head':'c'*40}},Path('/work/repository'),git)
            self.assertEqual(git.call_args.args,('-C','/work/repository','checkout','--detach',expected))
            self.assertEqual(git.call_args_list[0].args,('-C','/work/repository','fetch','origin',expected))

    def test_unknown_original_revision_cannot_use_current_repository_head(self):
        git=Mock()
        with self.assertRaises(PreparationError):
            checkout_recovery({'failure':{'input_manifest':{}},'candidate':{'head':'c'*40}},Path('/work/repository'),git)
        git.assert_not_called()
