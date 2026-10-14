"""Corrupted host evidence remains uncharged and cannot silently loop."""
import unittest

from forge.factory.wait_controller import WaitController
from forge.store.capture_problems import CaptureProblems
from forge.store.execution_problems import ExecutionProblems
from forge.store.models import TransitionError
from tests import test_forge_startup_problem as startup
from tests import test_store_result_controller as results


class CaptureProblemTests(unittest.TestCase):
    setUp = results.ResultControllerTests.setUp
    view = results.ResultControllerTests.view
    command = results.ResultControllerTests.command
    capture = results.ResultControllerTests.capture
    charges = results.ResultControllerTests.charges
    reserve = startup.StartupRepairTests.reserve
    launch = startup.StartupResultTests.launch
    save = startup.StartupRepairTests.save

    def corrupt(self, job):
        # Simulate on-disk corruption outside the supported immutable store API.
        with self.store.write() as db:
            db.execute('DROP TRIGGER artifacts_expire_only')
            db.execute("UPDATE job_artifacts SET digest=? WHERE job_id=? AND kind='worker_capture'",
                       ('0' * 64, job))

    def test_corrupt_capture_opens_scoped_draft_and_replays_without_charge(self):
        job = self.launch()
        self.capture(job, logs='private repository or credential text')
        self.corrupt(job)
        outcome = self.controller.step(job)
        self.assertTrue(outcome['accepted'])
        self.assertFalse(outcome['charged'])
        self.assertEqual(self.controller.step(job)['outcome'], 'deferred')
        self.assertEqual(self.charges(), 0)
        report = ExecutionProblems(self.store).report(outcome['report_signature'])
        self.assertNotIn('private repository', report['body'])
        self.assertEqual(report['affected'], [{'unit_id': 'u', 'job_id': job}])
        waits = WaitController(self.store, clock=lambda: 110)
        self.assertEqual(waits.step(outcome['gate_id'])['pending'], 'qualified_runner_update')
        image = 'sha256:' + 'b' * 64
        self.save('runtime.json', {'image': image})
        self.save('host.json', {'image': image, 'updated_at': 105})
        self.assertTrue(waits.step(outcome['gate_id'])['resolved'])

    def test_valid_capture_cannot_be_discarded_as_a_corruption_fault(self):
        job = self.launch()
        self.capture(job)
        value = self.queries.job(job)
        with self.assertRaises(TransitionError):
            CaptureProblems(self.store).defer(self.command(), job,
                execution_fence=value['execution_fence'], input_manifest=value['input_manifest'])
        self.assertIsNone(self.queries.job(job)['outcome'])

    def test_stale_corruption_does_not_gate_the_new_assignment(self):
        job = self.launch()
        self.capture(job)
        self.corrupt(job)
        self.life.pause(self.command())
        outcome = self.controller.step(job)
        self.assertFalse(outcome['accepted'])
        self.assertFalse(outcome['charged'])
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='forge_problem'").fetchone()[0], 0)
