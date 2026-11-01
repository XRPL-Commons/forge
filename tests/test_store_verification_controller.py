from copy import deepcopy
from dataclasses import replace
import json
import sqlite3
import unittest
from unittest.mock import Mock

from forge.factory.verification_controller import VerificationController
from forge.store.models import IdempotencyConflict, TransitionError, VersionConflict
from forge.store.verification import Verification
from tests import test_store_result_controller as helpers


class VerificationTests(unittest.TestCase):
    view = helpers.ResultControllerTests.view
    command = helpers.ResultControllerTests.command
    reserve = helpers.ResultControllerTests.reserve
    report = helpers.ResultControllerTests.report
    capture = helpers.ResultControllerTests.capture
    publish = helpers.ResultControllerTests.publish
    build = helpers.ResultControllerTests.build

    def setUp(self):
        helpers.ResultControllerTests.setUp(self)
        self.now = 100.0
        self.build()
        self.allowed = [{'name': 'api-tests', 'app_id': 73}]
        self.verification = Verification(self.store, clock=lambda: self.now)
        self.verifier = VerificationController(self.store, self.github, profiles=lambda repo: deepcopy(self.allowed),
            policy=lambda item: self.policy, clock=lambda: self.now)
        self.github.approvals = Mock(return_value=[])
        self.snapshot = dict(head=helpers.HEAD, base=helpers.BASE, observed_at=100,
            checks={helpers.HEAD: [dict(id=91, name='api-tests', app_id=73, status='completed', conclusion='success')]})
        self.after_snapshot = None
        def ci(item):
            # A second connection can write during collection: no SQL
            # transaction spans the remote observation.
            with self.store.write() as db:
                db.execute("INSERT OR REPLACE INTO store_metadata VALUES('test-ci-network','outside-transaction')")
            value = deepcopy(self.snapshot)
            if self.after_snapshot:
                self.after_snapshot()
            return value
        self.github.ci_evidence = Mock(side_effect=ci)

    def gate(self, kind='verification_ci'):
        details = dict(what='Verify the API workflow', ci=deepcopy(self.allowed) if kind == 'verification_ci' else [])
        return self.life.open_gate(self.command(), kind, candidate_id=self.view()['current_candidate_id'], details=details)['gate_id']

    def status(self, gate_id):
        with self.store.read_snapshot() as db:
            row = dict(db.execute('SELECT * FROM unit_gates WHERE id=?', (gate_id,)).fetchone())
            evidence = [dict(row) for row in db.execute("SELECT * FROM candidate_evidence WHERE kind LIKE 'verification_%'")]
            return row, evidence

    def ci_proof(self, gate_id):
        item = self.verifier.queries.gate(gate_id)
        from forge.store.transitions import manifest_hash
        return {**self.verifier._proof(item), 'via': 'ci', 'profile_hash': manifest_hash(self.allowed),
                'checks': [{**deepcopy(self.snapshot['checks'][helpers.HEAD][0]), 'head_oid': helpers.HEAD}]}

    def test_trusted_ci_releases_gate_atomically_then_review_can_start(self):
        gate_id = self.gate()
        with self.assertRaises(TransitionError):
            self.reserve('review')
        result = self.verifier.step(gate_id)
        gate, evidence = self.status(gate_id)
        self.assertEqual(gate['resolution'], 'evidence')
        self.assertEqual(evidence[0]['id'], result['evidence_id'])
        self.assertEqual(evidence[0]['candidate_id'], self.view()['current_candidate_id'])
        self.assertEqual(evidence[0]['kind'], 'verification_ci')
        review = self.reserve('review')
        history = self.queries.job(review)['history']['verified_checks']
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]['candidate'], self.view()['current_candidate_id'])
        self.assertEqual(history[0]['checks'], ['api-tests'])
        self.assertEqual(history[0]['proof']['checks'], [{'id': 91, 'name': 'api-tests', 'app_id': 73}])
        self.assertEqual(history[0]['what'], 'Verify the API workflow')

    def test_review_requested_ci_resumes_review_instead_of_inventing_a_pass(self):
        job = self.reserve('review', context={'verification_checks': deepcopy(self.allowed)})
        self.capture(job, self.report(job, 'needs_human_verification',
            verification={'what': 'Check API', 'why': 'Needs trusted CI', 'checks': ['api-tests']}))
        self.controller.step(job)
        gate_id = self.view()['current_blocker']['id']
        self.verifier.step(gate_id)
        self.assertEqual(self.view()['state'], 'reviewing')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM candidate_evidence WHERE kind='review'").fetchone()[0], 0)
        self.assertIsNotNone(self.reserve('review'))

    def test_ios_verification_waits_for_matching_trusted_mac_ci_and_still_needs_review(self):
        self.allowed = [{'name': 'ios-simulator', 'app_id': 73}]
        job = self.reserve('review', context={'verification_checks': deepcopy(self.allowed)})
        self.capture(job, self.report(job, 'needs_human_verification', verification={
            'what': 'Run xcodebuild test for the App scheme on an iOS Simulator and confirm all tests pass.',
            'why': 'The Linux worker has no Xcode or iOS Simulator.', 'checks': ['ios-simulator']}))
        self.controller.step(job)
        gate_id = self.view()['current_blocker']['id']
        # Successful Linux API tests do not satisfy the requested Mac check.
        self.assertIn('pending', self.verifier.step(gate_id))
        self.assertIsNone(self.status(gate_id)[0]['resolved_at'])
        self.snapshot['checks'][helpers.HEAD][0]['name'] = 'ios-simulator'
        self.verifier.step(gate_id)
        self.assertEqual(self.view()['state'], 'reviewing')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM candidate_evidence WHERE kind='review'").fetchone()[0], 0)
        self.assertIsNotNone(self.reserve('review'))

    def test_verification_can_release_during_audit(self):
        job = self.reserve('review')
        self.capture(job, self.report(job))
        self.controller.step(job)
        self.assertEqual(self.view()['state'], 'auditing')
        gate_id = self.gate()
        self.verifier.step(gate_id)
        self.assertIsNotNone(self.reserve('audit'))

    def test_generic_resolution_cannot_bypass_typed_receipt(self):
        gate_id = self.gate()
        with self.assertRaises(TransitionError):
            self.life.resolve_gate(self.command(), gate_id, release='evidence',
                evidence={'verified': True, 'observed_at': self.now, 'candidate_id': self.view()['current_candidate_id']})
        self.assertIsNone(self.status(gate_id)[0]['resolved_at'])

    def test_missing_pending_failed_duplicate_wrong_app_and_head_do_not_release(self):
        gate_id = self.gate()
        original = deepcopy(self.snapshot)
        variants = [[], [dict(original['checks'][helpers.HEAD][0], status='in_progress')],
                    [dict(original['checks'][helpers.HEAD][0], conclusion='failure')],
                    original['checks'][helpers.HEAD] * 2,
                    [dict(original['checks'][helpers.HEAD][0], app_id=74)],
                    [dict(original['checks'][helpers.HEAD][0], head_sha='other')]]
        for rows in variants:
            with self.subTest(rows=rows):
                self.snapshot = {**original, 'checks': {helpers.HEAD: rows}}
                self.assertIn('pending', self.verifier.step(gate_id))
                self.assertEqual(self.status(gate_id)[1], [])
        self.snapshot = {**original, 'base': 'new-base'}
        self.assertIn('pending', self.verifier.step(gate_id))
        self.assertIsNone(self.status(gate_id)[0]['resolved_at'])

    def test_profile_changes_cannot_authorize_workers_old_allowlist(self):
        gate_id = self.gate()
        self.allowed = [{'name': 'api-tests', 'app_id': 74}]
        self.assertEqual(self.verifier.step(gate_id)['pending'], 'verification_configuration')
        self.github.ci_evidence.assert_not_called()
        self.allowed = [{'name': 'api-tests', 'app_id': 73}]
        self.after_snapshot = lambda: self.allowed.clear()
        self.assertEqual(self.verifier.step(gate_id)['pending'], 'candidate_reconciliation')
        self.assertIsNone(self.status(gate_id)[0]['resolved_at'])

    def test_pause_racing_successful_ci_prevents_release_and_preserves_evidence_boundary(self):
        gate_id = self.gate()
        self.after_snapshot = lambda: self.life.pause(self.command())
        with self.assertRaises(VersionConflict):
            self.verifier.step(gate_id)
        self.assertEqual(self.status(gate_id)[1], [])
        self.assertIsNone(self.status(gate_id)[0]['resolved_at'])

    def test_candidate_and_membership_race_cannot_release(self):
        gate_id = self.gate()
        def membership_change():
            with self.store.write() as db:
                db.execute("UPDATE units SET membership_generation=membership_generation+1 WHERE id='u'")
        self.after_snapshot = membership_change
        with self.assertRaises(TransitionError):
            self.verifier.step(gate_id)
        self.assertEqual(self.status(gate_id)[1], [])

    def test_remote_head_base_policy_and_requirements_drift_keep_gate_held(self):
        gate_id = self.gate()
        for kind in ('head', 'base', 'policy', 'requirements'):
            with self.subTest(kind=kind):
                original_pr, original_policy, original_requirements = deepcopy(self.github.pr_value), deepcopy(self.policy), self.github.requirements
                def change():
                    if kind in {'head', 'base'}:
                        self.github.pr_value[kind]['sha'] = 'other'
                    elif kind == 'policy':
                        self.policy['human_required'] = True
                    else:
                        self.github.requirements = 'new requirements'
                self.after_snapshot = change
                self.assertEqual(self.verifier.step(gate_id)['pending'], 'candidate_reconciliation')
                self.assertEqual(self.status(gate_id)[1], [])
                self.github.pr_value, self.policy, self.github.requirements = original_pr, original_policy, original_requirements

    def test_ci_command_replay_binds_proof_actor_and_expected_version(self):
        gate_id = self.gate()
        proof, command = self.ci_proof(gate_id), self.command()
        first = self.verification.complete_ci(command, gate_id, proof=proof, allowed_checks=self.allowed)
        self.assertEqual(first, self.verification.complete_ci(command, gate_id, proof=proof, allowed_checks=self.allowed))
        for changed in (replace(command, actor_id='other'), replace(command, expected_version=command.expected_version + 1)):
            with self.assertRaises(IdempotencyConflict):
                self.verification.complete_ci(changed, gate_id, proof=proof, allowed_checks=self.allowed)
        self.assertEqual(len(self.status(gate_id)[1]), 1)

    def test_receipt_and_gate_release_roll_back_together(self):
        gate_id = self.gate()
        with self.store.write() as db:
            db.execute("CREATE TRIGGER test_release_failure BEFORE UPDATE ON unit_gates "
                       "WHEN NEW.resolved_at IS NOT NULL BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.verifier.step(gate_id)
        gate, evidence = self.status(gate_id)
        self.assertIsNone(gate['resolved_at'])
        self.assertEqual(evidence, [])

    def test_human_gate_never_uses_pr_approval_as_verification(self):
        gate_id = self.gate('verification_human')
        self.github.approvals.return_value = [{'state': 'APPROVED', 'counted': True}]
        self.assertEqual(self.verifier.step(gate_id)['pending'], 'operator_confirmation')
        self.github.approvals.assert_not_called()
        self.assertEqual(self.status(gate_id)[1], [])
        self.now = 120
        command = self.command()
        first = self.verifier.confirm_human(command, gate_id, statement='Ran the visual acceptance steps on this revision.')
        self.now = 200
        self.github.issue_error = True
        self.assertEqual(first, self.verifier.confirm_human(command, gate_id, statement='Ran the visual acceptance steps on this revision.'))
        with self.assertRaises(IdempotencyConflict):
            self.verifier.confirm_human(command, gate_id, statement='A changed confirmation')
        gate, evidence = self.status(gate_id)
        self.assertEqual(gate['resolved_at'], 120)
        self.assertEqual(evidence[0]['actor_id'], command.actor_id)
        self.assertTrue(json.loads(evidence[0]['details_json'])['operator_confirmed'])

    def test_human_proof_requires_explicit_attributed_confirmation_and_exact_candidate(self):
        gate_id = self.gate('verification_human')
        proof = {**self.verifier._proof(self.verifier.queries.gate(gate_id)), 'via': 'human',
                 'operator_confirmed': True, 'actor_id': 'operator', 'statement': 'Verified the requested steps.'}
        for altered in ({'operator_confirmed': False}, {'via': 'github_approval'}, {'actor_id': 'someone_else'},
                        {'head_oid': 'old-head'}, {'observed_at': 1}, {'fencing_generation': 999}):
            with self.subTest(altered=altered), self.assertRaises(TransitionError):
                self.verification.complete_human(self.command(), gate_id, proof={**proof, **altered})
        self.assertEqual(self.status(gate_id)[1], [])

    def test_cycle_advances_bounded_cursor_past_unavailable_gate(self):
        first = self.gate()
        second = self.gate('verification_human')
        self.github.issue_error = True
        page = self.verifier.cycle(limit=1)
        self.assertIsNotNone(page['next_cursor'])
        next_page = self.verifier.cycle(after=page['next_cursor'], limit=1)
        self.assertIsNone(next_page['next_cursor'])
        self.assertEqual({page['items'][0]['gate_id'], next_page['items'][0]['gate_id']}, {first, second})

    def test_staging_import_cannot_observe_or_confirm(self):
        gate_id = self.gate()
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        with self.assertRaises(TransitionError):
            self.verifier.step(gate_id)
        self.github.ci_evidence.assert_not_called()


if __name__ == '__main__':
    unittest.main()
