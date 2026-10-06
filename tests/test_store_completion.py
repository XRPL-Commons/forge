import json
import unittest

from forge.store.models import Candidate, Command, TransitionError
from forge.store.transitions import manifest_hash
from tests import test_store_transitions as helpers


class StoreCompletionTests(unittest.TestCase):
    setUp = helpers.StoreTransitionTests.setUp
    admit = helpers.StoreTransitionTests.admit
    view = helpers.StoreTransitionTests.view
    command = helpers.StoreTransitionTests.command
    reserve = helpers.StoreTransitionTests.reserve
    cleanup = helpers.StoreTransitionTests.cleanup
    fail = helpers.StoreTransitionTests.fail
    accept_build = helpers.StoreTransitionTests.accept_build
    add_independent_member = helpers.StoreTransitionTests.add_independent_member

    def observe(self, *, state='open', reason=None, date='2026-10-06T10:00:00Z', generation=1, title='Issue 1', requirements=None, labels=()):
        return self.lifecycle.observe_issue(self.command(), 'issue-1', state=state, state_reason=reason,
            title=title, body='Implement the requirement', requirements=requirements or {'acceptance': ['works']},
            remote_updated_at=date, generation=generation, labels=labels)

    def candidate(self):
        return Candidate(10, 'existing-branch', 'head', 'base', 'policy', manifest_hash({'acceptance': ['works']}), 'inputs')

    def adopt(self):
        candidate = self.candidate()
        return self.lifecycle.adopt_candidate(self.command(), candidate, evidence={
            'approved': True, 'verified': True, 'pull_number': 10, 'head_oid': 'head', 'base_oid': 'base',
            'policy_hash': 'policy', 'input_manifest_hash': 'inputs'})

    def reserve_review(self):
        candidate_id = self.view()['current_candidate_id']
        with self.store.read_snapshot() as db:
            identity = dict(db.execute('SELECT * FROM candidates WHERE id = ?', (candidate_id,)).fetchone())
        manifest = {key: identity[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
        manifest['candidate_id'] = candidate_id
        job = self.lifecycle.reserve_job(self.command(), 'review', slot='slot-1', execution_fence='fence-slot-1', input_manifest=manifest, candidate_id=candidate_id)
        return job, manifest

    def merge(self, candidate_id):
        return self.lifecycle.confirm_merged(self.command(), candidate_id, merge_sha='merge-sha', evidence={
            'verified': True, 'merged': True, 'merge_sha': 'merge-sha', 'pull_number': 10, 'head_oid': 'head', 'base_oid': 'base'})

    def test_ordered_observation_retains_revisions_and_rejects_equal_identity_conflicts(self):
        first = self.observe()
        stale = self.observe(state='closed', reason='completed', date='2026-10-05T10:00:00Z', generation=200)
        self.assertTrue(stale['stale'])
        self.assertEqual(self.view()['members'][0]['github_state'], 'open')
        with self.assertRaisesRegex(TransitionError, 'conflicting observations'):
            self.observe(title='Conflicting title')
        second = self.observe(title='New title', generation=2)
        self.assertNotEqual(first['revision_id'], second['revision_id'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM issue_revisions').fetchone()[0], 2)

    def test_label_observation_clears_only_its_own_gate_and_preserves_pause(self):
        self.observe(labels=['hold'])
        self.assertEqual(self.view()['current_blocker']['code'], 'label_hold')
        self.lifecycle.pause(self.command())
        self.observe(labels=[], generation=2)
        self.assertEqual(self.view()['current_blocker']['code'], 'operator_pause')
        self.lifecycle.resume(self.command())
        self.assertIsNone(self.view()['current_blocker'])

    def test_confirm_close_reopen_and_readmit_preserves_old_unit_history(self):
        self.observe(state='closed', reason='completed')
        with self.store.read_snapshot() as db:
            issue_version = db.execute("SELECT version FROM issues WHERE id = 'issue-1'").fetchone()[0]
        self.lifecycle.close_unit(self.command(), close_code='github_completed', evidence={'issue_version': issue_version})
        closed = self.view()
        self.assertEqual(closed['state'], 'closed')
        self.assertEqual(closed['close_code'], 'github_completed')
        self.assertEqual(len(closed['members']), 1)
        self.observe(state='open', reason='reopened', generation=2)
        self.assertEqual(self.view()['state'], 'closed')
        with self.store.read_snapshot() as db:
            issue_version = db.execute("SELECT version FROM issues WHERE id = 'issue-1'").fetchone()[0]
        result = self.lifecycle.admit_known_issue(request_id='readmit', actor_id='operator', unit_id='unit-new', issue_id='issue-1', expected_issue_version=issue_version)
        self.assertEqual(result['version'], 1)
        self.assertEqual(self.view('unit-new')['state'], 'queued')
        with self.assertRaisesRegex(TransitionError, 'current owning unit'):
            self.observe(generation=3)

    def test_requirements_change_fences_old_assignment_and_preserves_pause(self):
        job = self.reserve()
        self.lifecycle.pause(self.command())
        result = self.observe(requirements={'acceptance': ['changed']})
        self.assertEqual(self.view()['current_blocker']['code'], 'operator_pause')
        self.assertEqual(self.view()['membership_generation'], 2)
        self.assertEqual(self.view()['members'][0]['requirements_revision_id'], result['revision_id'])
        self.assertFalse(self.accept_build(job)['accepted'])
        self.cleanup(job)

    def test_review_rejection_invalidates_candidate_and_retains_charged_evidence(self):
        adopted = self.adopt()
        job, manifest = self.reserve_review()
        result = self.lifecycle.reject_job(self.command(), job['job_id'], execution_fence='fence-slot-1', input_manifest=manifest,
            evidence={'candidate_id': adopted['candidate_id'], 'verdict': 'fail', 'findings': ['fix regression']})
        self.assertTrue(result['charged'])
        self.assertEqual(self.view()['state'], 'building')
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.view()['charged_failures'], 1)
        self.assertEqual(self.view()['current_blocker']['code'], 'cleanup_pending')
        self.cleanup(job)
        self.reserve()
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM candidate_evidence WHERE verdict='fail'").fetchone()[0], 1)

    def test_candidate_invalidation_keeps_pause_and_supersedes_exact_candidate_gates(self):
        adopted = self.adopt()
        gate = self.lifecycle.open_gate(self.command(), 'verification_human', details={'instructions': 'check'}, candidate_id=adopted['candidate_id'])
        self.lifecycle.pause(self.command())
        self.lifecycle.invalidate_candidate(self.command(), adopted['candidate_id'], reason='base_changed', evidence={'verified': True, 'base_oid': 'new'})
        self.assertEqual(self.view()['current_blocker']['code'], 'operator_pause')
        self.assertEqual(self.view()['state'], 'building')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT resolution FROM unit_gates WHERE id=?', (gate['gate_id'],)).fetchone()[0], 'superseded')

    def test_explicit_retry_requires_cleanup_and_preserves_old_budget_and_pause(self):
        job = self.reserve()
        self.fail(job)
        with self.assertRaisesRegex(TransitionError, 'verified cleanup'):
            self.lifecycle.retry(self.command(), evidence={'approved': True})
        self.cleanup(job)
        self.lifecycle.pause(self.command())
        result = self.lifecycle.retry(self.command(), evidence={'approved': True})
        self.assertEqual(result['budget_generation'], 2)
        self.assertEqual(self.view()['charged_failures'], 0)
        self.assertEqual(self.view()['current_blocker']['code'], 'operator_pause')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 1)

    def test_recovery_can_correct_failure_after_cleanup_without_resetting_epoch(self):
        job = self.reserve()
        self.fail(job)
        self.cleanup(job)
        self.lifecycle.reconcile_recovery(self.command(), job['job_id'], evidence={'verified': True, 'cause': 'environment'}, reverse_charge=True)
        self.assertEqual(self.view()['budget_generation'], 1)
        self.assertEqual(self.view()['charged_failures'], 0)
        self.assertIsNone(self.view()['current_blocker'])
        self.reserve()

    def test_release_rule_scope_candidate_and_freshness_are_enforced(self):
        with self.assertRaisesRegex(TransitionError, 'scope'):
            self.lifecycle.open_gate(self.command(), 'verification_human', details={})
        gate = self.lifecycle.open_gate(self.command(), 'environment', details={})
        with self.assertRaisesRegex(TransitionError, 'release rule'):
            self.lifecycle.resolve_gate(self.command(), gate['gate_id'], release='observation', evidence={'verified': True, 'observed_at': 101})
        with self.assertRaisesRegex(TransitionError, 'fresh'):
            self.lifecycle.resolve_gate(self.command(), gate['gate_id'], release='recheck', evidence={'verified': True, 'observed_at': 99})
        self.lifecycle.resolve_gate(self.command(), gate['gate_id'], release='recheck', evidence={'verified': True, 'observed_at': 101})
        adopted = self.adopt()
        gate = self.lifecycle.open_gate(self.command(), 'approval_required', details={}, candidate_id=adopted['candidate_id'])
        with self.assertRaisesRegex(TransitionError, 'gated candidate'):
            self.lifecycle.resolve_gate(self.command(), gate['gate_id'], release='evidence', evidence={'verified': True, 'observed_at': 101, 'candidate_id': 'old'})

    def test_independent_merge_confirmation_requires_exact_receipt_and_cleanup(self):
        adopted = self.adopt()
        job, _ = self.reserve_review()
        with self.assertRaisesRegex(TransitionError, 'verified cleanup'):
            self.merge(adopted['candidate_id'])
        self.lifecycle.pause(self.command())
        self.cleanup(job)
        self.merge(adopted['candidate_id'])
        view = self.view()
        self.assertEqual(view['state'], 'merged')
        self.assertEqual(view['merge_sha'], 'merge-sha')
        self.assertEqual(len(view['members']), 1)
        self.assertIsNone(view['current_blocker'])
        self.assertEqual(view['members'][0]['exit_reason'], 'merged')
        with self.assertRaises(TransitionError):
            self.lifecycle.pause(self.command())

    def test_closing_current_candidate_requires_fresh_unmerged_receipt(self):
        adopted = self.adopt()
        with self.assertRaisesRegex(TransitionError, 'unmerged observation'):
            self.lifecycle.close_unit(self.command(), close_code='operator_closed', evidence={'approved': True})
        evidence = {'approved': True, 'verified': True, 'candidate_id': adopted['candidate_id'], 'merged': False,
                    'pull_number': 10, 'head_oid': 'head', 'observed_at': 101}
        self.lifecycle.close_unit(self.command(), close_code='operator_closed', evidence=evidence)
        self.assertEqual(self.view()['state'], 'closed')
        self.assertEqual(self.view()['members'][0]['github_state'], 'open')

    def test_requirements_observation_survives_ambiguous_merge_and_stays_blocked(self):
        adopted = self.adopt()
        with self.store.write() as db:
            db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,payload_json,created_at,updated_at) VALUES('effect','merge-effect','unit-1','merge','uncertain','{}',100,100)")
            db.execute("INSERT INTO merge_requests(id,unit_id,candidate_id,effect_id,fencing_generation,status,created_at) VALUES('merge','unit-1',?,'effect',1,'uncertain',100)", (adopted['candidate_id'],))
        result = self.observe(requirements={'acceptance': ['changed']})
        self.assertFalse(result['stale'])
        self.assertEqual(self.view()['current_candidate_id'], adopted['candidate_id'])
        self.assertEqual(self.view()['current_blocker']['code'], 'membership_decision')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT current_revision_id FROM issues WHERE id='issue-1'").fetchone()[0], result['revision_id'])

    def test_dissolved_children_retain_operator_pause_and_observed_label_hold(self):
        self.add_independent_member(2)
        self.observe(labels=['hold'])
        self.lifecycle.pause(self.command())
        result = self.lifecycle.dissolve(self.command(), evidence={'approved': True})
        for child in result['released']:
            self.assertEqual(self.view(child['unit_id'])['current_blocker']['code'], 'operator_pause')
        issue_child = next(c for c in result['released'] if c['issue_id'] == 'issue-1')
        command = self.command(unit_id=issue_child['unit_id'])
        self.lifecycle.resume(command)
        self.assertEqual(self.view(issue_child['unit_id'])['current_blocker']['code'], 'label_hold')

    def test_close_and_dissolve_refuse_ambiguous_external_effects_and_preserve_members(self):
        sibling, _ = self.add_independent_member(2)
        with self.store.write() as db:
            db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,payload_json,created_at,updated_at) VALUES('effect','merge-effect','unit-1','merge','uncertain','{}',100,100)")
        with self.assertRaisesRegex(TransitionError, 'external effects'):
            self.lifecycle.dissolve(self.command(), evidence={'approved': True})
        self.assertEqual(len(self.view()['members']), 2)
        with self.store.write() as db:
            db.execute("UPDATE external_effects SET status = 'failed' WHERE id = 'effect'")
        command = self.command()
        result = self.lifecycle.dissolve(command, evidence={'approved': True})
        self.assertEqual(result, self.lifecycle.dissolve(command, evidence={'approved': True}))
        self.assertEqual(self.view()['state'], 'dissolved')
        self.assertEqual(len(self.view()['members']), 2)
        self.assertEqual(len(result['released']), 2)
        for child in result['released']:
            self.assertEqual(self.view(child['unit_id'])['state'], 'queued')
            self.assertEqual(self.view(child['unit_id'])['members'][0]['issue_id'], child['issue_id'])


if __name__ == '__main__':
    unittest.main()
