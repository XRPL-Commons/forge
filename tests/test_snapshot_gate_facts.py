"""Read projection shares canonical facts without sharing authority across snapshots."""
import unittest
from unittest.mock import patch

from forge.factory.issue_selection import IssueSelection
from forge.store import queries, transitions
from forge.store.delivery_inputs import current_output_in
from forge.store.queries import IssueGateFacts, Queries
from tests import test_delivery_execution as fixtures


class SnapshotGateFactsTests(unittest.TestCase):
    setUp = fixtures.DeliveryExecutionTests.setUp
    command = fixtures.DeliveryExecutionTests.command
    compose = fixtures.DeliveryExecutionTests.compose
    manifest = fixtures.DeliveryExecutionTests.manifest
    clean = fixtures.DeliveryExecutionTests.clean
    plan = fixtures.DeliveryExecutionTests.plan
    child = fixtures.DeliveryExecutionTests.child
    accept = fixtures.DeliveryExecutionTests.accept

    def assert_parity(self, db, identifiers):
        contexts = Queries._issue_contexts(db, identifiers, include_gates=True)
        for identifier, (issue, unit) in contexts.items():
            gates = transitions.applicable_gates(db, unit, self.members[identifier], unit['current_candidate_id'])
            self.assertEqual(issue['lifecycle']['gates'], gates)
            self.assertEqual(issue['lifecycle']['current_blocker'], gates[0] if gates else None)
        return contexts

    def test_shared_gate_priority_and_member_candidate_scope_are_exact(self):
        self.compose(3, [dict(issue_id='issue-1', prerequisite_issue_id='issue-0')])
        self.life.open_gate(self.command(), 'label_hold', member_id=self.members['issue-0'], details={})
        self.life.open_gate(self.command(), 'environment', member_id=self.members['issue-0'], details={})
        self.life.pause(self.command())
        with self.store.write() as db:
            for name in ('candidate-a', 'candidate-b'):
                db.execute("INSERT INTO candidates(id,unit_id,pull_number,branch,head_oid,base_oid,policy_hash,requirements_hash,membership_generation,input_manifest_hash,accepted_at) VALUES(?,'batch',?,'work','head','base','policy','requirements',1,'manifest',1)", (name, 1 if name.endswith('a') else 2))
                db.execute("INSERT INTO unit_gates(id,unit_id,candidate_id,subject_key,code,created_at) VALUES(?,'batch',?,?,'approval_required',1)", (name, name, 'candidate:'+name))
        with self.store.read_snapshot() as db, patch.object(queries, '_collect_gates', wraps=transitions._collect_gates) as collect:
            unit = dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
            facts = IssueGateFacts(db)
            for state in ('queued', 'building', 'reviewing', 'auditing'):
                scoped_unit = {**unit, 'state': state}
                for member in (*self.members.values(), None):
                    for candidate in ('candidate-a', 'candidate-b', None):
                        with self.subTest(state=state, member=member, candidate=candidate):
                            actual = facts.for_assignment(scoped_unit, member, candidate)
                            self.assertEqual(actual, transitions.applicable_gates(db, scoped_unit, member, candidate))
                            self.assertEqual(sum(gate['code']=='approval_required' for gate in actual), int(candidate is not None))
                            self.assertIn('operator_pause', [gate['code'] for gate in actual])
            self.assertEqual(collect.call_count, 1)
            own = facts.for_assignment(unit, self.members['issue-0'], None)
            sibling = facts.for_assignment(unit, self.members['issue-2'], None)
            self.assertTrue({'label_hold','environment'} <= {gate['code'] for gate in own})
            self.assertEqual({gate['code'] for gate in sibling}, {'operator_pause'})

    def test_context_batches_above_one_page_collect_each_delivery_once(self):
        self.compose(101)
        with self.store.read_snapshot() as db, patch.object(queries, '_collect_gates', wraps=transitions._collect_gates) as collect:
            selection = IssueSelection(db)
            self.assertEqual(len(selection._contexts), 101)
            self.assertEqual(collect.call_count, 1)
            tail = selection.contexts(['issue-100', 'issue-0'])
            self.assertEqual(set(tail), {'issue-100', 'issue-0'})
            self.assertEqual(collect.call_count, 1)
            self.assertTrue(all(value[0]['lifecycle']['current_blocker'] is None for value in tail.values()))

    def test_new_snapshot_observes_gate_change_and_mutation_calls_stay_fresh(self):
        self.compose(2)
        with self.store.read_snapshot() as db:
            before = self.assert_parity(db, list(self.members))
            self.assertTrue(all(issue['lifecycle']['current_blocker'] is None for issue, _ in before.values()))
        self.life.open_gate(self.command(), 'label_hold', member_id=self.members['issue-0'], details={})
        with self.store.read_snapshot() as db:
            after = self.assert_parity(db, list(self.members))
            self.assertEqual(after['issue-0'][0]['lifecycle']['reason_code'], 'label_hold')
            self.assertIsNone(after['issue-1'][0]['lifecycle']['reason_code'])
        # The ordinary lifecycle entry point does not reuse even within a write.
        with self.store.write() as db, patch.object(transitions, '_collect_gates', wraps=transitions._collect_gates) as collect:
            unit = dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
            first = transitions.applicable_gates(db, unit, self.members['issue-1'])
            db.execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('new-pause','batch','unit','operator_pause',2)")
            second = transitions.applicable_gates(db, unit, self.members['issue-1'])
            self.assertEqual(first, [])
            self.assertEqual([gate['code'] for gate in second], ['operator_pause'])
            self.assertEqual(collect.call_count, 2)

    def test_recursive_accepted_outputs_and_invalidation_are_not_reused_across_snapshots(self):
        self.compose(3, [dict(issue_id='issue-1', prerequisite_issue_id='issue-0'),
                         dict(issue_id='issue-2', prerequisite_issue_id='issue-1')])
        _, plan, _ = self.plan()
        with self.store.read_snapshot() as db:
            initial = self.assert_parity(db, list(self.members))
            self.assertEqual(initial['issue-2'][0]['lifecycle']['reason_code'], 'dependency')
        for key in ('0', '1'):
            child, manifest = self.child(plan, key); self.accept(child, manifest, key)
        with self.store.read_snapshot() as db:
            accepted = self.assert_parity(db, list(self.members))
            self.assertIsNone(accepted['issue-2'][0]['lifecycle']['reason_code'])
        self.delivery.drop_member(self.command(), self.members['issue-0'], membership_generation=1,
            dependent_decisions={self.members['issue-1']:'wait_external'}, evidence={'approved':True})
        with self.store.read_snapshot() as db:
            invalidated = self.assert_parity(db, ['issue-1','issue-2'])
            self.assertEqual(invalidated['issue-1'][0]['lifecycle']['reason_code'], 'dependency')
            self.assertEqual(invalidated['issue-2'][0]['lifecycle']['reason_code'], 'dependency')

    def test_cycle_guard_is_preserved_for_persisted_graph_reads(self):
        self.compose(2, [dict(issue_id='issue-1', prerequisite_issue_id='issue-0')])
        # Public composition rejects cycles. Exercise the reader's existing
        # recursion guard independently on a deliberately cyclic SQL fixture.
        with self.store.write() as db:
            db.execute("INSERT INTO member_dependencies VALUES('batch',?,?,1)",
                       (self.members['issue-0'], self.members['issue-1']))
        with self.store.read_snapshot() as db:
            contexts = self.assert_parity(db, list(self.members))
            for identifier, (issue, unit) in contexts.items():
                self.assertEqual(issue['lifecycle']['reason_code'], 'dependency')
                self.assertIsNone(current_output_in(db, unit, self.members[identifier]))
