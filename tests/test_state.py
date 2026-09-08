import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from forge.state import StateError, StateStore


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = StateStore(self.temp.name)

    def claim(self, **kwargs):
        return self.store.claim("owner/repo:11", "builder", "codex", "head", now=1, **kwargs)

    def test_concurrent_claim_has_single_winner(self):
        def claim(_):
            try:
                return self.claim()
            except StateError:
                return None
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            attempts = list(executor.map(claim, range(8)))
        self.assertEqual(sum(a is not None for a in attempts), 1)

    def test_cancellation_fences_success_and_replacement_until_stopped(self):
        first = self.claim()
        self.store.cancel(first.attempt_id, first.generation)
        with self.assertRaises(StateError):
            self.store.finish(first.attempt_id, first.generation, "completed", stopped=True)
        with self.assertRaises(StateError):
            self.claim()
        with self.assertRaises(StateError):
            self.store.finish(first.attempt_id, first.generation, "cancelled")
        self.store.finish(first.attempt_id, first.generation, "cancelled", stopped=True)
        second = self.claim()
        self.assertEqual(second.generation, 2)
        with self.assertRaises(StateError):
            self.store.cancel(first.attempt_id, first.generation)
        with self.assertRaises(StateError):
            self.store.finish(second.attempt_id, first.generation, "completed", stopped=True)

    def test_budgets_survive_restart_and_candidate_change_resets(self):
        for _ in range(3):
            attempt = self.claim()
            self.store.finish(attempt.attempt_id, attempt.generation, "failed_retryable", stopped=True)
            self.store = StateStore(self.temp.name)
        with self.assertRaises(StateError):
            self.claim()
        next_attempt = self.store.claim("owner/repo:11", "builder", "claude", "new-head")
        self.assertEqual(next_attempt.generation, 4)

    def test_one_switch_and_no_verdict_shopping(self):
        first = self.claim()
        self.store.finish(first.attempt_id, first.generation, "blocked_auth", stopped=True)
        second = self.store.claim("owner/repo:11", "builder", "claude", "head")
        self.store.finish(second.attempt_id, second.generation, "failed_retryable", stopped=True)
        with self.assertRaisesRegex(StateError, "provider-switch"):
            self.claim()
        third = self.store.claim("owner/repo:11", "builder", "claude", "head")
        self.store.finish(third.attempt_id, third.generation, "failed_task", stopped=True)
        with self.assertRaises(StateError):
            self.store.claim("owner/repo:11", "builder", "claude", "head", max_attempts=10)

    def test_lost_and_corrupt_initialized_state_fail_closed(self):
        self.claim()
        path = Path(self.temp.name) / "attempts.json"
        path.write_text("invalid json")
        with self.assertRaises(StateError):
            self.claim()
        path.unlink()
        with self.assertRaises(StateError):
            self.claim()

    def test_cross_role_attempts_cannot_overlap(self):
        self.claim()
        with self.assertRaises(StateError):
            self.store.claim("owner/repo:11", "reviewer", "claude", "head")
