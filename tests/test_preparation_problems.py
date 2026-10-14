import unittest
from unittest.mock import patch

from forge.factory.runner import RunnerPreparationError
from forge.factory.result_controller import ResultController
from forge.factory.preparation_problems import problem
from forge.store.transitions import manifest_hash
from forge.store.models import Command, TransitionError
from tests import test_store_executions as fixtures


class PreparationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ExecutionTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def refuse(self, transient=False):
        raise RunnerPreparationError('project_denied', gate='dependency', transient=transient)

    def test_denial_is_cleaned_and_deferred_without_charging_or_retry(self):
        f = self.f; job = f.reserve()
        with patch.object(f.engine, 'create', side_effect=lambda _: self.refuse()) as create:
            self.assertTrue(f.controller.step(job)['cleanup'])
            f.controller.step(job)
            self.assertEqual(create.call_count, 1)
        current = f.queries.job(job)
        self.assertTrue(current['capture_authorized'])
        self.assertEqual(problem(f.store, current)['reason'], 'project_denied')
        result = ResultController(f.store, object(), policy=lambda _: {}, clock=lambda: f.now).step(job)
        self.assertFalse(result['charged'])
        self.assertEqual(f.queries.job(job)['outcome'], 'deferred')
        with f.store.read_snapshot() as db:
            gate = db.execute("SELECT details_json FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL").fetchone()
            self.assertIn('steward', gate[0])
            self.assertEqual(db.execute('SELECT count(*) FROM attempt_failures').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0], 0)

    def test_transient_refusal_backs_off_but_deadline_still_cleans(self):
        f = self.f; job = f.reserve()
        with patch.object(f.engine, 'create', side_effect=lambda _: self.refuse(True)) as create:
            self.assertEqual(f.controller.step(job)['pending'], 'project_denied')
            self.assertIn('retry_at', f.controller.step(job))
            self.assertEqual(create.call_count, 1)
            f.now = 201
            self.assertTrue(f.controller.step(job)['cleanup'])
        self.assertIsNone(problem(f.store, f.queries.job(job)))

    def test_created_worker_is_removed_when_preparation_is_denied(self):
        f = self.f; job = f.reserve()
        with patch.object(f.engine, 'prepare', side_effect=lambda _: self.refuse()):
            self.assertTrue(f.controller.step(job)['cleanup'])
        self.assertEqual(f.engine.starts, 0)
        self.assertEqual(f.engine.resets, 1)

    def test_worker_capture_cannot_impersonate_host_refusal(self):
        f = self.f; job = f.reserve()
        f.life.collect(f.command(), job, execution_fence='fence', capture={'logs': '', 'result': None,
            'preparation_refusal': {'version': 1, 'reason': 'project_denied', 'gate': 'dependency',
                                    'manifest_digest': manifest_hash(f.manifest)}})
        self.assertIsNone(problem(f.store, f.queries.job(job)))

    def test_started_worker_cannot_gain_host_refusal_receipt(self):
        f = self.f; job = f.reserve(); f.controller.step(job)
        command = f.command()
        with self.assertRaises(TransitionError):
            f.life.collect(Command(command.request_id, 'supervisor:execution', 'u', command.expected_version), job,
                execution_fence='fence', capture={'logs': '', 'result': None},
                preparation_refusal={'version': 1, 'reason': 'project_denied', 'gate': 'dependency',
                                     'manifest_digest': manifest_hash(f.manifest)})
