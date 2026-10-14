import json
from unittest.mock import Mock
from forge.factory.operator_reconciliation import ImportedOperatorController
from forge.factory.store_operator import OperatorActions
from forge.store.models import Command, TransitionError, VersionConflict
from forge.store.operator_reconciliation import OperatorReconciliation
from forge.store.transitions import Lifecycle
import unittest
from tests import test_store_reconciliation as fixtures
from tests.legacy_rows import LegacyLedger


class OperatorReconciliationTests(LegacyLedger, unittest.TestCase):
    setup_store=fixtures.ReconciliationTests.setup_store
    view=fixtures.ReconciliationTests.view
    unit=fixtures.ReconciliationTests.unit
    def imported_action(self):
        self.setup_store()
        with self.store.write() as db:
            db.execute("INSERT INTO issue_action_requests VALUES('old','1:1','resume','legacy-revision','pending','Original reason','Old progress',?,NULL,12)",(json.dumps({'add':['ready'],'remove':['hold']}),))
            db.execute("INSERT INTO imported_operator_requests VALUES('old','manifest','pending','Old progress',NULL,NULL)")
        return ImportedOperatorController(self.store,self.github,clock=lambda:self.now)
    def test_pending_intent_is_interrupted_and_preserved_then_issue_reconciles_paused(self):
        controller=self.imported_action()
        result=controller.step('1:1')
        self.assertEqual(result['requests'],['old'])
        with self.store.read_snapshot() as db:
            row=db.execute("SELECT * FROM issue_action_requests WHERE id='old'").fetchone()
            self.assertEqual(row['status'],'rejected');self.assertEqual(row['reason'],'Original reason')
            self.assertEqual(json.loads(row['plan_json']),{'add':['ready'],'remove':['hold']})
            provenance=db.execute("SELECT * FROM imported_operator_requests WHERE request_id='old'").fetchone()
            self.assertEqual(provenance['source_message'],'Old progress')
            self.assertEqual(json.loads(provenance['evidence_json'])['operation_id'],'activation')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM operator_action_authority").fetchone()[0],0)
        self.assertFalse(controller.step('1:1')['interrupted'])
        self.assertTrue(self.controller.step(self.unit)['reconciled'])
        self.assertEqual(self.view()['reason_code'],'operator_pause')
        self.assertEqual(self.view()['charged_failures'],0)
    def test_ledger_sync_without_pending_row_is_interrupted_but_hold_is_preserved(self):
        self.setup_store()
        life=Lifecycle(self.store,clock=lambda:self.now)
        old={'legacy_field':'operator_sync','value':{'add':['ready'],'remove':['hold']}}
        gate=life.open_gate(Command('sync','import',self.unit,self.view()['version']),'operator_sync',details=old)['gate_id']
        self.assertTrue(self.controller.step(self.unit)['interrupted'])
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT * FROM unit_gates WHERE id=?',(gate,)).fetchone()
            self.assertEqual(json.loads(row['details_json']),old);self.assertIsNotNone(row['resolved_at'])
        self.assertTrue(self.controller.step(self.unit)['reconciled'])
        self.assertEqual(self.view()['reason_code'],'operator_pause')
    def test_operator_cycle_visits_imported_pending_without_adopting_authority(self):
        self.imported_action()
        actions=OperatorActions(self.store,self.github,config=self.config,clock=lambda:self.now)
        self.assertTrue(actions.cycle()['items'][0]['interrupted'])
        self.assertEqual(actions.cycle()['items'],[])
    def test_activation_missing_or_remote_outage_preserves_intent(self):
        controller=self.imported_action()
        with self.store.write() as db:db.execute("DELETE FROM store_metadata WHERE key='migration_reconciliation_authorization'")
        self.assertEqual(controller.step('1:1')['pending'],'migration_authorization_required')
        self.assertEqual(self.github.calls,0)
    def test_stale_observation_does_not_interrupt(self):
        self.imported_action()
        with self.assertRaises(VersionConflict):
            OperatorReconciliation(self.store,clock=lambda:self.now).interrupt('1:1',proof={'verified':True,'observed_at':self.now,'issue_version':-1,'issue':self.github.remote})
        with self.store.read_snapshot() as db:self.assertEqual(db.execute("SELECT status FROM issue_action_requests WHERE id='old'").fetchone()[0],'pending')
