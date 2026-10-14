import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.credentials import Credentials
from forge.factory.config import validate
from forge.factory.provider_logins import check_logins
from forge.store.executions import provider_available
from tests.sqlite_fixture import selected_store
from tests.test_codex_parallel_auth import login


BAD = 'FORGE_SETUP_CODEX_AUTH'
GOOD = 'FORGE_SETUP_CODEX_OTHER'


class ProviderLoginTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = selected_store(self.root)
        self.credentials = Credentials(self.root)
        self.now = 100
        self.config = validate({'provider_pool': [
            dict(id='first', provider='codex', model='one', provider_environment=[], provider_auth_file_env=BAD),
            dict(id='second', provider='codex', model='two', provider_environment=[], provider_auth_file_env=GOOD)]})
        self.credentials.put(GOOD, json.dumps(login()), file=True)

    def check(self):
        return check_logins(self.root, self.store, self.config, clock=lambda: self.now)

    def available(self, reference):
        with self.store.read_snapshot() as db:
            return provider_available(db, 'codex', None, {'provider_auth_file_env': reference}, self.now)

    def test_access_only_account_is_blocked_without_affecting_healthy_account_or_charging_work(self):
        value = login(); value['tokens']['refresh_token'] = ''
        self.credentials.put(BAD, json.dumps(value), file=True)
        self.assertEqual(self.check(), [{'account_ref': BAD, 'reason': 'login_access_only'}])
        self.assertFalse(self.available(BAD)); self.assertTrue(self.available(GOOD))
        with self.store.read_snapshot() as db:
            row = dict(db.execute('SELECT * FROM provider_cooldowns').fetchone())
            self.assertEqual((row['account_ref'], row['reason_code'], row['retry_at']), (BAD, 'blocked_auth', 400))
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)
            details = db.execute('SELECT details_json FROM provider_observations').fetchone()[0]
            self.assertNotIn('synthetic-refresh', details)
            self.assertNotIn('access_token', details)
        self.now += 10; self.check()
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT retry_at FROM provider_cooldowns').fetchone()[0], 410)
            self.assertEqual(db.execute('SELECT count(*) FROM provider_observations').fetchone()[0], 1)

    def test_reconnect_clears_only_our_auth_condition_immediately(self):
        self.check()
        self.assertFalse(self.available(BAD))
        self.credentials.put(BAD, json.dumps(login(expiry=1)), file=True)
        self.assertEqual(self.check(), [])
        # An expired access token with a managed refresh token is renewable;
        # actual renewal remains the existing runner's responsibility.
        self.assertTrue(self.available(BAD))
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM provider_cooldowns').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM provider_observations').fetchone()[0], 0)

    def test_other_cooldown_and_observation_survive_bad_login_then_reconnect(self):
        with self.store.write() as db:
            db.execute("INSERT INTO provider_cooldowns VALUES(?,'codex','blocked_quota',900,90,NULL)", (BAD,))
            db.execute("INSERT INTO provider_observations VALUES('external','codex',?,'unknown','unavailable',90,'{}')", (BAD,))
        self.check()
        self.credentials.put(BAD, json.dumps(login()), file=True)
        self.check()
        with self.store.read_snapshot() as db:
            self.assertEqual(tuple(db.execute('SELECT reason_code,retry_at FROM provider_cooldowns').fetchone()), ('blocked_quota', 900))
            self.assertEqual(db.execute('SELECT id FROM provider_observations').fetchone()[0], 'external')
        self.assertFalse(self.available(BAD))

    def test_replaced_auth_cooldown_is_not_cleared_by_local_health(self):
        self.check()
        with self.store.write() as db:
            db.execute("UPDATE provider_cooldowns SET reason_code='blocked_quota',retry_at=800 WHERE account_ref=?", (BAD,))
        self.credentials.put(BAD, json.dumps(login()), file=True)
        self.check()
        self.assertFalse(self.available(BAD))
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT reason_code FROM provider_cooldowns').fetchone()[0], 'blocked_quota')

    def test_missing_malformed_and_nonobject_tokens_are_bounded_local_reasons(self):
        self.assertEqual(self.check()[0]['reason'], 'login_missing')
        for value in ('not json', json.dumps({'tokens': 'private malformed content'})):
            self.credentials.put(BAD, value, file=True)
            self.assertEqual(self.check()[0]['reason'], 'login_unreadable')
            self.assertTrue(self.available(GOOD))

    def test_api_key_only_configuration_never_reads_subscription_credentials(self):
        with patch.object(Credentials, 'locked', side_effect=AssertionError('unneeded credential read')):
            self.assertEqual(check_logins(self.root, self.store, validate({})), [])

    def test_incomplete_managed_login_is_blocked_before_worker_preparation(self):
        from copy import deepcopy
        incomplete = []
        for field in ('account_id', 'id_token', 'access_token'):
            value = deepcopy(login())
            del value['tokens'][field]
            incomplete.append(value)
        incomplete.append({**login(), 'auth_mode': 'unsupported'})
        for token in ('malformed', None, 123, []):
            value = deepcopy(login()); value['tokens']['access_token'] = token
            incomplete.append(value)
        for value in incomplete:
            with self.subTest(value=list(value['tokens'])):
                self.credentials.put(BAD, json.dumps(value), file=True)
                self.assertEqual(self.check(), [{'account_ref': BAD, 'reason': 'login_unreadable'}])
                self.assertFalse(self.available(BAD))
                self.assertTrue(self.available(GOOD))
                with self.store.read_snapshot() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)
        self.credentials.put(BAD, json.dumps(login(expiry=1)), file=True)
        self.assertEqual(self.check(), [])
        self.assertTrue(self.available(BAD))

    def test_production_cycle_checks_login_before_any_supervisor_dispatch(self):
        from forge.factory.sqlite_runtime import cycle
        from types import SimpleNamespace
        parts = dict(store=self.store, config=self.config, settings=object(), github=object())
        for key in ('requirements','verification','execution','observations','results','recovery','merges',
                    'effects','dispatch','planning','triage_effects','steward_effects','scheduling'):
            parts[key] = object()
        def supervisor_cycle():
            self.assertFalse(self.available(BAD)); self.assertTrue(self.available(GOOD))
            return {'checked_before_dispatch': True}
        with patch('forge.factory.sqlite_runtime.components', return_value=parts), \
                patch('forge.factory.store_operator.OperatorActions'), \
                patch('forge.factory.store_inventory.DiscoveryController'), \
                patch('forge.factory.sqlite_runtime.SqliteSupervisor', return_value=SimpleNamespace(cycle=supervisor_cycle)):
            self.assertEqual(cycle(self.root, clock=lambda: self.now), {'checked_before_dispatch': True})
