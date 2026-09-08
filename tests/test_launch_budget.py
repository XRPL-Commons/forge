import fcntl
import json
from pathlib import Path
import tempfile
import unittest

from forge.activity import record
from forge.launch_budget import plan, refund


class LaunchBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.state = root / 'state'
        self.config = root / 'config'
        self.config.mkdir()
        (self.config / 'app.repo').write_text('REPO=owner/app\nOWNER_LABEL=auto:owned-by-forge\n')
        (self.state / 'legacy-health').mkdir(parents=True)
        self.guard_path = self.state / 'legacy-health/guard.json'
        self.guard = {'account_until': 0, 'sessions': {'app-build': {'launches': [100.2], 'until': 0}}}
        self.save()
        self.lock = (root / 'state-supervisor.lock').open('w')
        self.addCleanup(self.lock.close)

    def save(self):
        self.guard_path.write_text(json.dumps(self.guard))

    def event(self, event, at, metadata=None, session='app-build'):
        record(self.state, event, session, repo='owner/app', role=session.rsplit('-', 1)[1], metadata=metadata, now=at)

    def generation(self, started=100, stopped=200, reason='verified_handoff', dispatch=True, observed=True):
        self.event('session_created', started, {'reason': 'launch'})
        if observed:
            self.event('activity_changed', started+10, {'state': 'busy', 'session_started_at': started})
        if dispatch:
            self.event('dispatch', started+20, {'reason': 'initial_task'})
        self.event('session_stopped', stopped, {'reason': reason})

    def apply(self):
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        return refund(self.config, self.state, lock_fd=self.lock.fileno(), now=1000)

    def test_successful_generation_refund_is_atomic_and_idempotent(self):
        self.generation()
        before = self.guard_path.read_text()
        proposal = plan(self.config, self.state, now=1000)
        self.assertEqual(self.guard_path.read_text(), before)
        self.assertEqual(proposal['refunds'][0]['launch_at'], 100.2)
        result = self.apply()
        self.assertEqual(len(result['refunds']), 1)
        guard = json.loads(self.guard_path.read_text())
        self.assertEqual(guard['sessions']['app-build']['launches'], [])
        self.assertEqual(len(guard['sessions']['app-build']['successful_launch_refunds']), 1)
        self.assertEqual(self.apply()['refunds'], [])

    def test_failed_starts_crashes_and_other_roles_keep_charges(self):
        self.guard['sessions']['app-build']['launches'] = [100.2, 300.2, 500.2]
        self.guard['sessions']['app-review'] = {'launches': [100.2], 'until': 0}
        self.save()
        self.generation()
        self.generation(300, 350, 'verified_interrupted_startup', dispatch=False)
        self.generation(500, 550, 'crashed')
        self.apply()
        guard = json.loads(self.guard_path.read_text())
        self.assertEqual(guard['sessions']['app-build']['launches'], [300.2, 500.2])
        self.assertEqual(guard['sessions']['app-review']['launches'], [100.2])

    def test_global_and_nonbudget_cooldowns_preserved(self):
        self.guard.update(account_until=5000, account_reason='blocked_quota')
        self.guard['sessions']['app-build'].update(until=4000, reason='blocked_auth')
        self.save()
        self.generation()
        self.apply()
        guard = json.loads(self.guard_path.read_text())
        self.assertEqual((guard['account_until'], guard['account_reason']), (5000, 'blocked_quota'))
        self.assertEqual(guard['sessions']['app-build']['until'], 4000)
        self.assertEqual(guard['sessions']['app-build']['reason'], 'blocked_auth')

    def test_only_refunded_launch_budget_cooldown_can_clear(self):
        self.guard['sessions']['app-build'].update(until=4000, reason='launch_budget_exhausted')
        self.save()
        self.generation()
        self.apply()
        guard = json.loads(self.guard_path.read_text())
        self.assertEqual(guard['sessions']['app-build']['until'], 0)
        self.assertNotIn('reason', guard['sessions']['app-build'])

    def test_ambiguous_launch_charge_never_refunded(self):
        self.guard['sessions']['app-build']['launches'] = [100.1, 100.2]
        self.save()
        self.generation()
        self.assertEqual(self.apply()['refunds'], [])

    def test_generation_and_dispatch_are_required(self):
        self.generation(observed=False)
        self.assertEqual(self.apply()['refunds'], [])

    def test_no_dispatch_never_refunded_even_with_success_reason(self):
        self.generation(dispatch=False)
        self.assertEqual(self.apply()['refunds'], [])

    def test_mixed_generation_observations_reject_refund(self):
        self.event('session_created', 100)
        self.event('activity_changed', 110, {'state': 'busy', 'session_started_at': 100})
        self.event('activity_changed', 115, {'state': 'busy', 'session_started_at': 101})
        self.event('dispatch', 120)
        self.event('session_stopped', 200, {'reason': 'verified_handoff'})
        self.assertEqual(self.apply()['refunds'], [])

    def test_replacement_generation_cannot_borrow_prior_dispatch(self):
        self.event('session_created', 100)
        self.event('dispatch', 120)
        self.event('session_created', 130)
        self.event('activity_changed', 140, {'state': 'busy', 'session_started_at': 130})
        self.event('session_stopped', 200, {'reason': 'verified_handoff'})
        self.assertEqual(self.apply()['refunds'], [])

    def test_supervisor_lock_required_for_mutations(self):
        self.generation()
        before = self.guard_path.read_text()
        value = refund(self.config, self.state, lock_fd=self.lock.fileno(), now=1000)
        self.assertEqual(value['status'], 'supervisor_lock_required')
        self.assertEqual(self.guard_path.read_text(), before)

    def test_callback_receipt_supplies_exact_generation_when_observer_missed_it(self):
        self.generation(observed=False)
        fcntl.flock(self.lock, fcntl.LOCK_EX)
        proof = {'released': True, 'reason': 'verified_handoff', 'observed_at': 200.1,
                 'evidence': {'repo': 'owner/app', 'role': 'build', 'session_created': 100, 'session_id': '$9'}}
        value = refund(self.config, self.state, session='app-build', proof=proof,
                       lock_fd=self.lock.fileno(), now=1000)
        self.assertEqual(value['refunds'][0]['session_id'], '$9')

    def test_success_reason_for_another_role_cannot_refund(self):
        self.generation(reason='verified_awaiting_human_approval')
        self.assertEqual(self.apply()['refunds'], [])
