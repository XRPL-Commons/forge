import unittest
from unittest.mock import patch

from forge.factory.wait_controller import WaitController
from forge.store.models import TransitionError
from tests import test_store_transitions as helpers


class WaitTests(unittest.TestCase):
    setUp = helpers.StoreTransitionTests.setUp
    admit = helpers.StoreTransitionTests.admit
    view = helpers.StoreTransitionTests.view
    command = helpers.StoreTransitionTests.command

    def wait(self, retry_at=200, code='provider_unavailable', unit='unit-1'):
        return self.lifecycle.open_gate(self.command(unit_id=unit), code, details={'retry_at': retry_at})['gate_id']

    def test_elapsed_wait_releases_only_its_gate_and_preserves_pause_and_budget(self):
        gate = self.wait()
        self.lifecycle.pause(self.command())
        controller = WaitController(self.store, clock=lambda: 199)
        self.assertEqual(controller.step(gate)['pending'], 'retry_time')
        self.assertTrue(WaitController(self.store, clock=lambda: 200).step(gate)['resolved'])
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        self.assertEqual(self.view()['charged_failures'], 0)
        self.lifecycle.resume(self.command())
        self.assertIsNone(self.view()['reason_code'])
        self.assertTrue(WaitController(self.store, clock=lambda: 201).step(gate)['resolved'])

    def test_bad_deadline_does_not_release_wait(self):
        for invalid in (None, 'yesterday', True):
            with self.subTest(invalid=invalid):
                gate = self.wait(invalid)
                self.assertEqual(WaitController(self.store, clock=lambda: 200).step(gate)['pending'], 'missing_retry_time')
                # Close it only as test cleanup; the controller leaves it in place.
                with self.store.write() as db:
                    db.execute("UPDATE unit_gates SET resolved_at=200,resolution='superseded' WHERE id=?", (gate,))

    def test_wait_controller_cannot_release_human_or_environment_gate(self):
        gate = self.lifecycle.open_gate(self.command(), 'environment', details={'retry_at': 100})['gate_id']
        with self.assertRaises(TransitionError):
            WaitController(self.store, clock=lambda: 200).step(gate)
        self.assertEqual(self.view()['reason_code'], 'environment')

    def test_paged_waits_allow_later_eligible_work(self):
        self.admit(2)
        self.wait(500)
        self.wait(100, unit='unit-2')
        controller = WaitController(self.store, clock=lambda: 200)
        first = controller.cycle(limit=1)
        self.assertIsNotNone(first['next_cursor'])
        controller.cycle(after=first['next_cursor'], limit=1)
        self.assertIsNone(self.view('unit-2')['reason_code'])
        self.assertEqual(self.view()['reason_code'], 'provider_unavailable')

    def test_racing_operator_change_keeps_wait_and_records_coded_conflict(self):
        self.wait(100)
        controller = WaitController(self.store, clock=lambda: 200)
        original = controller.commands.resolve_gate
        def race(*args, **kwargs):
            self.lifecycle.pause(self.command())
            return original(*args, **kwargs)
        with patch.object(controller.commands, 'resolve_gate', side_effect=race):
            result = controller.cycle()
        self.assertEqual(result['items'][0]['error'], 'VersionConflict')
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM unit_gates WHERE code='provider_unavailable' AND resolved_at IS NULL").fetchone())

    def test_import_only_cycle_is_rejected_even_when_no_gates_exist(self):
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        with self.assertRaises(TransitionError):
            WaitController(self.store).cycle()
