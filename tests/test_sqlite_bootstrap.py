"""Host-only identity and provider setup use the selected runtime rows."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests import test_operator_bootstrap as legacy
from tests.test_sqlite_identity import selected_store
from forge.operator_bootstrap import Bootstrap
from forge.onboarding import Onboarding
from forge.store.configuration import Configuration


class SqliteHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = selected_store(self.root)
        Configuration(self.database).replace('host-runtime.json', {}, expected_revision=0)
        self.setup = Onboarding(self.root)
        self.setup.read()
        self.serve, self.commands, self.verify = {}, [], Mock()
        self.host = Bootstrap(self.root, command=self.command, check_https=self.verify)

    command = legacy.HostTests.command
    request = legacy.HostTests.request
    apply = legacy.HostTests.apply
    error = legacy.HostTests.error

    def test_bootstrap_uses_normalized_host_and_generation_without_json(self):
        self.assertFalse((self.root / 'host-runtime.json').exists())
        result = self.apply()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(result['generation'], self.setup.read()['generation'])
        self.assertEqual(self.host.auth.config()['client_secret'], legacy.SECRET)
        self.assertFalse((self.root / 'state/operator-auth').exists())
        self.assertNotIn(legacy.SECRET, json.dumps(self.commands))
        with self.database.read_snapshot() as db:
            self.assertNotIn(legacy.SECRET, '\n'.join(db.iterdump()))

    def test_provider_login_uses_sql_runtime_and_clears_sql_cooldown(self):
        from forge.host_setup import connect_login
        with self.database.write() as db:
            db.execute("INSERT INTO provider_cooldowns(account_ref,provider,reason_code,retry_at,observed_at) VALUES('codex','codex','blocked_auth',100,1)")
        with patch('forge.host_setup.getpass.getpass', return_value='synthetic-provider-credential'), patch('builtins.print'):
            connect_login(self.root, 'codex', api_key=True)
        self.assertEqual(self.setup.runtime()['provider_pool'][0]['provider'], 'codex')
        self.assertFalse((self.root / 'setup/runtime.json').exists())
        self.assertFalse((self.root / 'credentials/pending-login-codex.json').exists())
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM provider_cooldowns').fetchone()[0], 0)

    def test_browser_login_cleanup_reads_sql_intent(self):
        from forge.harnesses.accounts import mark_login, reset_logins
        mark_login(self.root)
        self.assertFalse((self.root / 'state/harness-login-enabled.json').exists())
        with patch('forge.harnesses.accounts.subprocess.run', return_value=Mock(stdout='')) as run:
            reset_logins(self.root)
        self.assertEqual(run.call_count, 2)

if __name__ == '__main__':
    unittest.main()
