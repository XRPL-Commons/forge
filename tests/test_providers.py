import tempfile
import unittest

from forge.providers import ProviderRouter


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.router = ProviderRouter(self.temp.name)

    def test_alternates_from_actual_completed_provider(self):
        self.assertEqual(self.router.choose("builder").provider, "codex")
        self.assertEqual(self.router.choose("reviewer", successful_builder="codex").provider, "claude")
        self.assertEqual(self.router.choose("auditor", successful_reviewer="claude").provider, "codex")
        self.assertEqual(self.router.choose("reviewer", successful_builder="claude").provider, "codex")

    def test_account_circuit_survives_restart_and_half_open_probe_is_single(self):
        self.router.record_failure("codex", "blocked_quota", now=10)
        router = ProviderRouter(self.temp.name)
        route = router.choose("builder", now=11)
        self.assertEqual(route.provider, "claude")
        self.assertTrue(route.degraded)
        self.assertFalse(router.reserve_probe("codex", now=11))
        self.assertTrue(router.reserve_probe("codex", now=911))
        self.assertFalse(ProviderRouter(self.temp.name).reserve_probe("codex", now=911))
        router.record_success("codex", attempt_started_at=911)
        self.assertFalse(router.choose("builder", now=912).degraded)

    def test_task_and_policy_failures_never_open_provider_circuit(self):
        for failure in ("blocked_permission", "failed_task", "failed_infrastructure"):
            self.router.record_failure("codex", failure, now=1)
            self.assertEqual(self.router.choose("builder", now=2).provider, "codex")

    def test_both_down_stop_and_account_rotation_has_separate_health(self):
        for provider in ("codex", "claude"):
            self.router.record_failure(provider, "blocked_auth", now=1)
        route = self.router.choose("builder", now=2)
        self.assertIsNone(route.provider)
        self.assertEqual(route.retry_after, 901)
        rotated = ProviderRouter(self.temp.name, accounts={"codex": "new", "claude": "default"})
        self.assertEqual(rotated.choose("builder", now=2).provider, "codex")

    def test_missing_provider_and_transient_backoff(self):
        self.assertIsNone(ProviderRouter(self.temp.name, available=set()).choose("builder").provider)
        self.router.record_failure("codex", "failed_retryable", now=0)
        self.assertFalse(self.router.reserve_probe("codex", now=14))
        self.router.record_failure("codex", "failed_retryable", now=15)
        self.assertFalse(self.router.reserve_probe("codex", now=44))
        self.assertTrue(self.router.reserve_probe("codex", now=45))

    def test_old_success_cannot_close_newer_account_outage(self):
        self.router.record_failure("codex", "blocked_quota", now=100)
        self.assertFalse(self.router.record_success("codex", attempt_started_at=99))
        self.assertEqual(self.router.choose("builder", now=101).provider, "claude")
        self.assertFalse(self.router.record_success("codex", attempt_started_at=100))
        self.assertTrue(self.router.record_success("codex", attempt_started_at=101))
        self.assertEqual(self.router.choose("builder", now=102).provider, "codex")
        with self.assertRaises(ValueError):
            self.router.record_success("codex", attempt_started_at=float("nan"))
