import json
import unittest

from forge.factory.execution_controller import ExecutionController
from forge.factory.recovery_controller import RecoveryController
from forge.store.executions import ExecutionQueries
from forge.store.models import Command
from forge.store.recovery import RecoveryQueries
from forge.store.transitions import Lifecycle
from tests import test_store_reconciliation as reconciliation_fixtures
from tests.test_store_recovery_controller import Runner
from tests import test_store_import as fixtures


class MigratedRecoveryTests(unittest.TestCase):
    setUp=fixtures.ImportTests.setUp
    write=fixtures.ImportTests.write
    snapshot=fixtures.ImportTests.snapshot
    setup_store=reconciliation_fixtures.ReconciliationTests.setup_store
    view=reconciliation_fixtures.ReconciliationTests.view
    unit=reconciliation_fixtures.ReconciliationTests.unit

    def prepare(self,*,count=1,investigation='pending',role='build',retry='ready',candidate=False,all_pending=False):
        self.issue['state']='hold' if investigation=='complete' else 'investigating'
        for index in range(count):
            name='failure-'+str(index)
            self.ledger['jobs'][name]=dict(id=name,issue_key='1:1',role=role,cleanup=True,fenced=True,slot='worker',
                created_at=20+index,phase='cleaned',outcome='failed',branch='work',target_branch='main',
                capture={'result':{'outcome':'unable','summary':'Original failed diagnostic'},'logs':'Preserved diagnostic bytes'})
            self.issue['failures'].append(dict(attempt=name,role=role,cause='Original failure',investigation=investigation if index==count-1 or all_pending else 'complete',
                retry_state=retry,evidence=['Original diagnostic evidence'],findings=None if index==count-1 else dict(pattern='earlier distinct failure'),at=21+index,charged=True))
        self.setup_store(candidate=candidate,phase=self.issue['state'])
        self.github.publisher=lambda role:role+'[bot]'
        self.github.token=lambda role,repo_id,worker:'ephemeral-token'
        self.runner=Runner(self.github)
        self.execution=ExecutionController(self.store,self.runner,self.github,enabled=lambda: True,admission=lambda _:True,clock=lambda:self.now)
        self.recovery=RecoveryController(self.store,self.runner,self.github,config=self.config,admission=lambda _:True,clock=lambda:self.now)
        return 'failure-'+str(count-1)

    def run_recovery(self,failed):
        result=self.recovery.schedule(failed);self.assertTrue(result.get('reserved'),result)
        job=result['job_id'];self.execution.step(job)
        self.assertTrue(ExecutionQueries(self.store).job(job)['cleanup'])
        return self.recovery.finish(job)

    def test_pending_imported_failure_gets_one_readonly_investigation_without_original_launch(self):
        failed=self.prepare();self.assertTrue(self.controller.step(self.unit)['reconciled'])
        self.assertEqual(self.view()['reason_code'],'recovery_pending')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_launches').fetchone()[0],0)
            original=json.loads(db.execute('SELECT original_json FROM migration_recovery_adoptions').fetchone()[0])
            self.assertEqual(original['investigation'],'pending')
        result=self.run_recovery(failed);self.assertTrue(result['accepted'])
        self.assertEqual(self.view()['charged_failures'],1)
        self.assertIsNone(self.view()['reason_code'])
        self.assertIn('Original failed diagnostic',json.dumps(self.runner.notes[0]['failure']))
        self.assertIn('Preserved diagnostic bytes',json.dumps(self.runner.notes[0]['failure']))
        self.assertEqual(RecoveryQueries(self.store).pending_failures()['items'],[])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM recovery_assignments').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT outcome FROM jobs WHERE id=?',(failed,)).fetchone()[0],'failed')
            self.assertIsNone(db.execute('SELECT 1 FROM job_launches WHERE job_id=?',(failed,)).fetchone())
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='correction'").fetchone()[0],0)

    def test_third_failure_investigated_then_budget_remains_exhausted(self):
        failed=self.prepare(count=3);self.controller.step(self.unit)
        self.assertTrue(self.run_recovery(failed)['accepted'])
        self.assertEqual(self.view()['charged_failures'],3)
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM unit_gates WHERE code='budget_exhausted' AND resolved_at IS NULL").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM migration_recovery_adoptions').fetchone()[0],1)

    def test_incomplete_prior_investigation_preserved_for_attributed_operator_decision(self):
        self.prepare(investigation='incomplete');before=self.view()
        self.assertTrue(self.controller.step(self.unit)['reconciled'])
        self.assertEqual(self.view()['reason_code'],'decision_required')
        self.assertEqual(self.view()['charged_failures'],before['charged_failures'])
        self.assertEqual(RecoveryQueries(self.store).pending_failures()['items'],[])
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT * FROM migration_recovery_adoptions').fetchone()
            self.assertEqual(row['disposition'],'operator_required');self.assertEqual(row['actor_id'],'operator:cli:0')
            self.assertEqual(db.execute('SELECT investigation FROM attempt_failures').fetchone()[0],'incomplete')

    def test_explicit_pause_still_fences_imported_recovery(self):
        failed=self.prepare()
        Lifecycle(self.store,clock=lambda:self.now).pause(Command('pause','operator',self.unit,self.view()['version']))
        self.controller.step(self.unit)
        from forge.store.models import TransitionError
        with self.assertRaisesRegex(TransitionError,'gate blocks recovery'):self.recovery.schedule(failed)
        self.assertEqual(self.view()['reason_code'],'operator_pause')

    def test_review_execution_failure_rebinds_same_candidate_and_resumes_review(self):
        failed=self.prepare(role='review',retry='built',candidate=True)
        self.assertTrue(self.controller.step(self.unit)['reconciled'])
        self.assertEqual(self.view()['state'],'reviewing')
        selected=self.view()['current_candidate_id']
        self.assertTrue(self.run_recovery(failed)['accepted'])
        self.assertEqual(self.view()['current_candidate_id'],selected)
        self.assertEqual(self.view()['state'],'reviewing')
        self.assertEqual(self.view()['charged_failures'],1)

    def test_technical_rejection_requires_rebuild_of_preserved_candidate(self):
        failed=self.prepare(role='review',retry='ready',candidate=True)
        self.assertEqual(self.controller.step(self.unit)['invalidated'],'legacy_retry')
        self.assertIsNone(self.view()['current_candidate_id']);self.assertEqual(self.view()['state'],'building')
        self.assertTrue(self.run_recovery(failed)['accepted'])
        self.assertEqual(self.view()['charged_failures'],1)

    def test_several_pending_failures_investigate_in_order_without_mutual_gate_deadlock(self):
        self.prepare(count=2,all_pending=True)
        self.controller.step(self.unit)
        self.assertTrue(self.run_recovery('failure-0')['accepted'])
        with self.store.read_snapshot() as db:
            gate=json.loads(db.execute("SELECT details_json FROM unit_gates WHERE code='recovery_pending' AND resolved_at IS NULL").fetchone()[0])
            self.assertEqual(gate['job_id'],'failure-1')
        self.assertTrue(self.run_recovery('failure-1')['accepted'])
        self.assertEqual(self.view()['charged_failures'],2)
        self.assertEqual(self.view()['reason_code'],'approach_required')

    def test_incomplete_new_investigation_cannot_charge_or_recursively_investigate(self):
        failed=self.prepare();self.controller.step(self.unit);self.runner.recovery_valid=False
        self.assertTrue(self.run_recovery(failed)['accepted'])
        self.assertEqual(self.view()['charged_failures'],1)
        self.assertEqual(RecoveryQueries(self.store).pending_failures()['items'],[])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT incomplete FROM recovery_findings').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempt_failures').fetchone()[0],1)

    def test_recorded_approach_hold_survives_reconciliation(self):
        self.issue['reason']='Repeated failure requires a supported change of approach'
        self.prepare(investigation='complete');self.controller.step(self.unit)
        self.assertEqual(self.view()['reason_code'],'approach_required')
        self.assertEqual(self.view()['charged_failures'],1)

    def test_completed_investigation_preserves_candidate_hold_and_known_retry_phase(self):
        self.issue['reason']='Repeated failure requires a supported change of approach'
        self.prepare(investigation='complete',role='review',retry='built',candidate=True)
        self.assertTrue(self.controller.step(self.unit)['reconciled'])
        self.assertEqual(self.view()['state'],'reviewing')
        self.assertEqual(self.view()['reason_code'],'approach_required')
        self.assertEqual(RecoveryQueries(self.store).pending_failures()['items'],[])
