import tempfile
import unittest
from forge.legacy_health import LegacyGuard, classify


class LegacyHealthTests(unittest.TestCase):
    def test_quota_pauses_entire_account_across_restart(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertFalse(LegacyGuard(state).transact("observe", "one-build", "Credit balance is too low", now=1)[0])
            self.assertFalse(LegacyGuard(state).transact("check", "two-audit", now=100)[0])
            self.assertTrue(LegacyGuard(state).transact("check", "two-audit", now=3602)[0])

    def test_launch_budget_survives_new_guard_objects(self):
        with tempfile.TemporaryDirectory() as state:
            for n in range(3):
                self.assertTrue(LegacyGuard(state).transact("launch", "s", now=n)[0])
            self.assertFalse(LegacyGuard(state).transact("launch", "s", now=4)[0])

    def test_quiet_prompt_is_not_provider_outage(self):
        self.assertIsNone(classify("Ready for your next instruction"))

    def test_permission_does_not_pause_other_repositories(self):
        with tempfile.TemporaryDirectory() as state:
            guard = LegacyGuard(state)
            self.assertFalse(guard.transact("observe", "one-build", "sandbox failed", now=1)[0])
            self.assertTrue(guard.transact("check", "two-build", now=2)[0])
