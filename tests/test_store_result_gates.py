import unittest

from forge.store.models import IdempotencyConflict, TransitionError
from forge.store.models import Candidate
from forge.store.transitions import manifest_hash
from tests import test_store_executions as helpers


class ResultGateTests(unittest.TestCase):
    setUp = helpers.ExecutionTests.setUp
    driver = helpers.ExecutionTests.driver
    view = helpers.ExecutionTests.view
    command = helpers.ExecutionTests.command
    reserve = helpers.ExecutionTests.reserve
    collect = helpers.ExecutionTests.collect
    cleanup = helpers.ExecutionTests.cleanup

    def reserve_again(self):
        return self.life.reserve_job(self.command(), 'build', slot='slot', execution_fence='next-fence',
            input_manifest=self.manifest, member_id=self.member, launch=self.launch)['job_id']

    def defer(self, job, command=None, code='environment', details=None):
        return self.life.defer_job(command or self.command(), job, execution_fence='fence',
            input_manifest=self.manifest, evidence={'verified': 'environment unavailable'},
            code=code, details=details or {'reason': 'missing_database'})

    def test_wait_survives_cleanup_replays_and_does_not_charge(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        command = self.command()
        first = self.defer(job, command)
        self.assertEqual(first, self.defer(job, command))
        self.assertTrue(first['accepted'])
        self.assertEqual(self.view()['reason_code'], 'environment')
        self.assertEqual(self.view()['charged_failures'], 0)
        self.assertEqual(self.view()['state'], 'building')
        self.assertEqual(self.queries.pending(results=True)['items'], [])
        with self.assertRaises(IdempotencyConflict):
            self.defer(job, command, details={'reason': 'changed'})
        self.life.resolve_gate(self.command(), first['gate_id'], release='recheck',
                               evidence={'verified': True, 'observed_at': self.now})
        self.assertIsNotNone(self.reserve_again())

    def test_stale_wait_does_not_install_a_gate(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        self.life.pause(self.command())
        self.assertFalse(self.defer(job)['accepted'])
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        self.assertEqual(self.view()['charged_failures'], 0)
        self.assertEqual(self.queries.job(job)['outcome'], 'stale_deferred')

    def test_deferral_keeps_cleanup_lease_until_verified(self):
        job = self.reserve()
        self.collect(job)
        self.defer(job)
        self.assertFalse(self.queries.job(job)['cleanup'])
        self.assertIn(job, self.queries.pending()['items'])
        self.cleanup(job)
        self.assertEqual(self.view()['reason_code'], 'environment')

    def test_build_verification_without_candidate_rolls_back_result(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        with self.assertRaises(TransitionError):
            self.defer(job, code='verification_human')
        self.assertIsNone(self.queries.job(job)['outcome'])
        self.assertEqual(self.view()['reason_code'], 'result_pending')

    def test_provider_wait_requires_future_finite_deadline(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        for value in (True, float('inf'), float('nan'), 99, 100):
            with self.assertRaises(TransitionError):
                self.defer(job, code='provider_unavailable', details={'retry_at': value})
        result = self.defer(job, code='provider_unavailable', details={'retry_at': 150})
        with self.assertRaises(TransitionError):
            self.life.resolve_gate(self.command(), result['gate_id'], release='time', evidence={'clock': 100})
        self.now = 150
        self.life.resolve_gate(self.command(), result['gate_id'], release='time', evidence={'clock': 150})
        self.assertIsNotNone(self.reserve_again())

    def finish(self, job, gates):
        return self.life.finish_job(self.command(), job, execution_fence='fence', input_manifest=self.manifest,
            evidence={'head_oid': 'head', 'base_oid': 'base', 'pull_number': 1},
            candidate=Candidate(1, 'forge/issue-1', 'head', 'base', 'policy',
                manifest_hash({'acceptance': ['works']}), manifest_hash(self.manifest)), gates=gates)

    def test_publication_and_verification_gate_commit_together(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        self.finish(job, [{'code': 'verification_human', 'details': {'reason': 'visual acceptance'}}])
        unit = self.view()
        self.assertEqual(unit['state'], 'reviewing')
        self.assertEqual(unit['reason_code'], 'verification_human')
        self.assertEqual(unit['current_blocker']['candidate_id'], unit['current_candidate_id'])
        self.assertEqual(self.queries.job(job)['outcome'], 'success')

    def test_duplicate_completion_gates_leave_publication_unaccepted(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        gate = {'code': 'verification_human', 'details': {'reason': 'visual acceptance'}}
        with self.assertRaises(TransitionError):
            self.finish(job, [gate, gate])
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertIsNone(self.queries.job(job)['outcome'])
