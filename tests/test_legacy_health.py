import tempfile
import unittest
from forge.legacy_health import LegacyGuard, classify


class LegacyHealthTests(unittest.TestCase):
    def test_unattested_terminal_quota_is_session_scoped_across_restart(self):
        with tempfile.TemporaryDirectory() as state:
            self.assertFalse(LegacyGuard(state).transact("observe", "one-build", "Credit balance is too low", now=1)[0])
            self.assertTrue(LegacyGuard(state).transact("check", "two-audit", now=100)[0])
            self.assertFalse(LegacyGuard(state).transact("check", "one-build", now=100)[0])
            self.assertTrue(LegacyGuard(state).transact("check", "one-build", now=3602)[0])

    def test_existing_account_guard_is_preserved(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as state:
            guard=LegacyGuard(state)
            path=Path(state)/'legacy-health/guard.json'
            path.write_text(json.dumps({'sessions':{},'account_until':5000,'account_reason':'blocked_quota'}))
            guard.transact('observe','one-build','API Error: insufficient_quota',now=100)
            self.assertFalse(guard.transact('check','other-review',now=101)[0])
            self.assertEqual(json.loads(path.read_text())['account_until'],5000)

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


    def test_task_quotes_and_old_error_are_not_current_provider_errors(self):
        for text in ['Added regression tests for insufficient_quota handling.\nAll tests passed.',
                     '> API Error: insufficient_quota', 'API Error: insufficient_quota\nRecovered successfully.\n❯',
                     'Testing authentication failed responses.', '❯ Explain permission denied',
                     'Review summary:\nAPI Error: insufficient_quota', '● Bash(test quota)\nAPI Error: insufficient_quota',
                     '```text\nAPI Error: insufficient_quota\n```']:
            with self.subTest(text=text):self.assertIsNone(classify(text))
        self.assertIsNone(classify('API Error: 429 insufficient_quota\n❯\n────\n ⏵⏵ bypass permissions on'))
        self.assertEqual(classify('API Error: 429 insufficient_quota'),'blocked_quota')

    def test_identical_error_does_not_extend_expired_cooldown(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as state:
            guard=LegacyGuard(state)
            guard.transact('observe','one-build','Credit balance is too low',now=1)
            guard.transact('observe','one-build','Credit balance is too low',now=3602)
            self.assertEqual(json.loads((Path(state)/'legacy-health/guard.json').read_text())['sessions']['one-build']['until'],3601)

    def test_contended_guard_lock_returns_without_waiting(self):
        import fcntl
        from pathlib import Path
        with tempfile.TemporaryDirectory() as state:
            guard=LegacyGuard(state)
            with (Path(state)/'legacy-health/guard.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                self.assertEqual(guard.transact('check','one-build'),(False,'guard_busy'))


    def test_observed_model_limit_after_tools_and_before_healthy_footer(self):
        banner="  ⎿  You've reached your Fable limit. Run /usage-credits to continue or switch models with /model."
        text='❯ Continue the builder task.\n● Bash(git status)\n'+banner+'\n\n✻ Sautéed for4m8s · done2:59PM\n\n❯\n────\n ⏵⏵ bypass permissions on'
        self.assertEqual(classify(text),'blocked_model_usage')
        with tempfile.TemporaryDirectory() as state:
            guard=LegacyGuard(state)
            self.assertFalse(guard.transact('observe','one-build',text,now=100)[0])
            self.assertTrue(guard.transact('check','other-audit',now=101)[0])
            guard.transact('start_failed','one-build',now=102)
            self.assertEqual(guard.transact('check','one-build',now=103)[1],'blocked_model_usage')

    def test_model_limit_above_later_response_or_quoted_is_not_current(self):
        banner="⎿ You've reached your Fable limit. Run /usage-credits to continue or switch models with /model."
        for text in [banner+'\n● Work resumed successfully.\n❯\n────\n ⏵⏵ bypass permissions on',
                     '```text\n'+banner+'\n```', '> '+banner, 'Review summary:\n'+banner]:
            with self.subTest(text=text):self.assertIsNone(classify(text))


    def test_verified_progress_clears_only_no_progress_and_preserves_launches(self):
        import json
        from pathlib import Path
        with tempfile.TemporaryDirectory() as state:
            guard=LegacyGuard(state)
            path=Path(state)/'legacy-health/guard.json'
            for reason in ['no_progress','blocked_model_usage','blocked_auth','blocked_quota','blocked_permission','launch_budget_exhausted']:
                before={'sessions':{'one-build':{'reason':reason,'until':777,'launches':[1,2]}},'account_until':999,'account_reason':'blocked_auth'}
                path.write_text(json.dumps(before))
                guard.transact('progress','one-build',now=100)
                after=json.loads(path.read_text())
                self.assertEqual(after['account_until'],999)
                self.assertEqual(after['sessions']['one-build']['launches'],[1,2])
                if reason=='no_progress':self.assertEqual(after['sessions']['one-build'],{'until':0,'launches':[1,2]})
                else:self.assertEqual(after,before)
