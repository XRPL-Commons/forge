import json
import unittest

from forge.factory.accounts import cooldown_since, current_cooldown, health, status_label
from tests.test_factory_cycle import CycleTest
from tests.test_harnesses import pool


NOW = 10_000
POOL = [{'id': 'codex', 'provider': 'codex', 'model': None},
        {'id': 'claude', 'provider': 'claude', 'model': None},
        {'id': 'claude-opus', 'provider': 'claude', 'model': 'opus'}]
NAMES = {'codex': 'Codex', 'claude': 'Claude Code'}


def ledger(cooldowns=None, jobs=None, issues=None, events=None):
    return {'provider_cooldowns': cooldowns or {}, 'jobs': jobs or {}, 'issues': issues or {}, 'events': events or []}


def waiting(key, provider, created_at, reason='blocked_auth'):
    """An issue whose latest attempt is waiting on `provider`."""
    return ({'id': key + '-job', 'issue_key': key, 'provider': provider, 'outcome': 'waiting', 'created_at': created_at},
            {'key': key, 'state': 'ready', 'active': None, 'reason': reason})


def row(rows, provider):
    return next(r for r in rows if r['provider'] == provider)


class HealthTests(unittest.TestCase):
    def test_healthy_pool_is_one_ok_row_per_provider(self):
        rows = health(ledger(), {'provider_pool': POOL, 'provider_ready': None}, NOW, NAMES)
        self.assertEqual([r['provider'] for r in rows], ['codex', 'claude'])
        self.assertEqual(row(rows, 'claude'), {'provider': 'claude', 'name': 'Claude Code', 'ready': True, 'status': 'ok',
                                               'since': None, 'until': None, 'reason_code': None, 'detail': None,
                                               'waiting_issues': 0})

    def test_blocked_auth_is_sign_in_expired_with_since_and_waiting_issues(self):
        jobs, issues = {}, {}
        for n in range(5):
            job, issue = waiting(f'r:{n}', 'codex', 9000 + n)
            jobs[job['id']], issues[issue['key']] = job, issue
        # An issue Claude already picked up is not waiting on Codex.
        jobs['old'] = {'id': 'old', 'issue_key': 'r:9', 'provider': 'codex', 'outcome': 'waiting', 'created_at': 1}
        jobs['new'] = {'id': 'new', 'issue_key': 'r:9', 'provider': 'claude', 'outcome': None, 'created_at': 2}
        issues['r:9'] = {'key': 'r:9', 'state': 'built', 'active': 'new', 'reason': 'blocked_auth'}
        data = ledger({'codex': {'until': NOW + 600, 'reason': 'blocked_auth', 'attempt': 'r:4-job', 'since': 8100}}, jobs, issues)
        rows = health(data, {'provider_pool': POOL, 'provider_ready': None}, NOW, NAMES)
        codex = row(rows, 'codex')
        self.assertEqual((codex['status'], codex['ready'], codex['reason_code']), ('sign_in_expired', False, 'blocked_auth'))
        self.assertEqual((codex['since'], codex['until'], codex['waiting_issues']), (8100, NOW + 600, 5))
        self.assertEqual(row(rows, 'claude')['status'], 'ok')

    def test_blocked_auth_with_detail_is_still_sign_in_expired(self):
        data = ledger({'codex': {'until': NOW + 60, 'reason': 'blocked_auth', 'detail': 'login_access_only'}})
        codex = row(health(data, {'provider_pool': POOL}, NOW), 'codex')
        self.assertEqual((codex['status'], codex['detail']), ('sign_in_expired', 'login_access_only'))

    def test_blocked_quota_is_quota(self):
        data = ledger({'claude': {'until': NOW + 60, 'reason': 'blocked_quota', 'attempt': 'x', 'since': 9000}})
        self.assertEqual(row(health(data, {'provider_pool': POOL}, NOW), 'claude')['status'], 'quota')

    def test_lapsed_auth_cooldown_stays_signed_out_until_an_attempt_runs(self):
        cooldown = {'until': NOW - 5, 'reason': 'blocked_auth', 'attempt': 'a', 'since': 8000}
        jobs = {'a': {'id': 'a', 'issue_key': 'r:1', 'provider': 'codex', 'outcome': 'waiting', 'created_at': 8500}}
        data = ledger({'codex': cooldown}, jobs)
        self.assertEqual(row(health(data, {'provider_pool': POOL}, NOW), 'codex')['status'], 'sign_in_expired')
        # A later attempt that got past sign-in clears it.
        jobs['b'] = {'id': 'b', 'issue_key': 'r:1', 'provider': 'codex', 'outcome': 'accepted', 'created_at': 9000}
        self.assertEqual(row(health(data, {'provider_pool': POOL}, NOW), 'codex')['status'], 'ok')

    def test_lapsed_quota_cooldown_with_nothing_waiting_is_ok(self):
        data = ledger({'codex': {'until': NOW - 5, 'reason': 'blocked_quota', 'attempt': 'a', 'since': 8000}},
                      {'a': {'id': 'a', 'issue_key': 'r:1', 'provider': 'codex', 'outcome': 'waiting', 'created_at': 8500}},
                      {'r:1': {'key': 'r:1', 'state': 'merged', 'active': None, 'reason': None}})
        self.assertEqual(row(health(data, {'provider_pool': POOL}, NOW), 'codex')['status'], 'ok')

    def test_unverified_account_is_unavailable(self):
        rows = health(ledger(), {'provider_pool': POOL, 'provider_ready': ['claude']}, NOW)
        self.assertEqual((row(rows, 'codex')['status'], row(rows, 'codex')['reason_code']), ('unavailable', 'verification_required'))
        # One verified model is enough for the account.
        self.assertEqual(row(rows, 'claude')['status'], 'ok')

    def test_since_falls_back_to_the_first_wait_of_the_current_streak(self):
        events = [{'at': 7000, 'provider': 'codex', 'kind': 'availability_wait', 'reason': 'blocked_quota'},
                  {'at': 7500, 'provider': 'codex', 'kind': 'launched'},
                  {'at': 8000, 'provider': 'codex', 'kind': 'availability_wait', 'reason': 'blocked_auth'},
                  {'at': 8600, 'provider': 'claude', 'kind': 'launched'},
                  {'at': 9200, 'provider': 'codex', 'kind': 'availability_wait', 'reason': 'blocked_auth'}]
        data = ledger({'codex': {'until': NOW + 600, 'reason': 'blocked_auth', 'attempt': 'a'}}, events=events)
        self.assertEqual(row(health(data, {'provider_pool': POOL}, NOW), 'codex')['since'], 8000)

    def test_no_pool_has_no_rows(self):
        self.assertEqual(health(ledger(), {'provider_pool': []}, NOW), [])


class SinceTests(unittest.TestCase):
    def test_renewing_the_same_reason_keeps_the_first_seen_time(self):
        data = ledger({'codex': {'until': NOW - 1, 'reason': 'blocked_auth', 'attempt': 'a', 'since': 8100}},
                      {'a': {'id': 'a', 'provider': 'codex', 'outcome': 'waiting', 'created_at': 9400}})
        self.assertEqual(cooldown_since(data, 'codex', 'blocked_auth', NOW), 8100)

    def test_a_different_reason_or_a_recovery_starts_over(self):
        data = ledger({'codex': {'until': NOW + 1, 'reason': 'blocked_quota', 'attempt': 'a', 'since': 8100}})
        self.assertEqual(cooldown_since(data, 'codex', 'blocked_auth', NOW), NOW)
        data = ledger({'codex': {'until': NOW - 1, 'reason': 'blocked_auth', 'attempt': 'a', 'since': 8100}},
                      {'b': {'id': 'b', 'provider': 'codex', 'outcome': 'accepted', 'created_at': 9000}})
        self.assertIsNone(current_cooldown(data, 'codex', NOW))
        self.assertEqual(cooldown_since(data, 'codex', 'blocked_auth', NOW), NOW)

    def test_wording_names_usage_limits_only_for_quota(self):
        self.assertEqual(status_label({'reason': 'blocked_auth'}), 'sign in again')
        self.assertEqual(status_label({'reason': 'blocked_quota'}), 'usage limit; retry pending')
        self.assertNotIn('usage', status_label({'reason': 'something else'}))


class CooldownCycleTests(CycleTest):
    def setUp(self):
        super().setUp()
        self.config['provider_pool'] = pool()

    def test_auth_failure_records_since_and_reaches_the_board(self):
        self.cycle(); job = self.active()
        self.engine.finish(job)
        self.engine.capture = lambda _: {'result': None, 'logs': json.dumps({'type': 'error', 'message': 'token expired'}),
                                        'execution': {'exit_code': 1}, 'missing': []}
        self.cycle()
        data = self.ledger.read()
        cooldown = data['provider_cooldowns']['codex']
        self.assertEqual(cooldown['reason'], 'blocked_auth')
        self.assertGreater(cooldown['since'], 0)
        rows = health(data, {'provider_pool': pool(), 'provider_ready': None}, cooldown['since'])
        self.assertEqual(row(rows, 'codex')['status'], 'sign_in_expired')
        self.assertEqual(row(rows, 'codex')['since'], cooldown['since'])


if __name__ == '__main__':
    unittest.main()
