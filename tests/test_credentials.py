import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.credentials import Credentials
from tests.test_dashboard import dashboard, signed_in, OPERATOR_CSRF


def secrets(store):
    return sorted(p.name for p in store.root.iterdir() if p.name not in ('index.json', 'lock'))


class CredentialStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.store = Credentials(directory.name)

    def test_replacing_a_secret_deletes_the_old_copy(self):
        self.store.put('FORGE_SETUP_BUILDER_KEY', 'first-synthetic-key')
        self.store.put('FORGE_SETUP_REVIEWER_KEY', 'reviewer-synthetic-key')
        self.store.put('FORGE_SETUP_BUILDER_KEY', 'second-synthetic-key')
        self.assertEqual(len(secrets(self.store)), 2)
        stored = [(self.store.root / name).read_text() for name in secrets(self.store)]
        self.assertNotIn('first-synthetic-key', stored)
        self.assertEqual(self.store.environment()['FORGE_SETUP_BUILDER_KEY'], 'second-synthetic-key')

    def test_a_rotated_login_leaves_one_file_and_keeps_its_compare_and_swap(self):
        self.store.put('FORGE_SETUP_CODEX_AUTH', '{"token":"one"}', file=True)
        old = hashlib.sha256(b'{"token":"one"}').hexdigest()
        self.assertTrue(self.store.refresh('FORGE_SETUP_CODEX_AUTH', old, '{"token":"two"}'))
        self.assertFalse(self.store.refresh('FORGE_SETUP_CODEX_AUTH', old, '{"token":"stale"}'))
        self.assertEqual(len(secrets(self.store)), 1)
        path = Path(self.store.environment()['FORGE_SETUP_CODEX_AUTH'])
        self.assertEqual(path.read_text(), '{"token":"two"}')

    def test_claim_login_identity_changes_on_reconnect_but_not_refresh(self):
        reference = 'FORGE_SETUP_CODEX_AUTH'
        self.store.put(reference, '{"token":"one"}', file=True)
        original = self.store.login_generations()
        self.assertTrue(self.store.refresh(reference, hashlib.sha256(b'{"token":"one"}').hexdigest(),
                                           '{"token":"two"}'))
        self.assertEqual(self.store.login_generations(), original)
        self.store.put(reference, '{"token":"three"}', file=True)
        self.assertNotEqual(self.store.login_generations(), original)
        self.assertEqual(self.store.login_generations(only=['OPENAI_API_KEY']), {})

    def test_legacy_login_identity_survives_its_first_refresh(self):
        reference = 'FORGE_SETUP_CODEX_AUTH'
        self.store.put(reference, '{"token":"one"}', file=True)
        path = self.store.root / 'index.json'
        index = json.loads(path.read_text())
        del index[reference]['login_generation']
        path.write_text(json.dumps(index))
        original = self.store.login_generations()
        self.assertNotEqual(original[reference], index[reference]['name'])
        self.store.refresh(reference, hashlib.sha256(b'{"token":"one"}').hexdigest(), '{"token":"two"}')
        self.assertEqual(self.store.login_generations(), original)

    def test_cycle_publishes_only_selected_account_generations_without_refresh_churn(self):
        from tests.test_sqlite_identity import selected_store
        from forge.store.configuration import Configuration
        database = selected_store(self.store.root.parent)
        reference = 'FORGE_SETUP_CODEX_AUTH'
        self.store.put(reference, '{"token":"one"}', file=True)
        self.store.put('FORGE_SETUP_BUILDER_KEY', 'private-app-value')
        config = {'provider_pool': [{'provider_auth_file_env': reference}]}
        self.store.publish_login_generations(database, config)
        before = Configuration(database).get('provider-logins.json')
        self.assertEqual(before['settings'], {'generations': self.store.login_generations(only=[reference])})
        self.assertNotIn('private-app-value', json.dumps(before))
        self.store.refresh(reference, hashlib.sha256(b'{"token":"one"}').hexdigest(), '{"token":"two"}')
        self.store.publish_login_generations(database, config)
        self.assertEqual(Configuration(database).get('provider-logins.json'), before)
        self.store.put(reference, '{"token":"three"}', file=True)
        self.store.publish_login_generations(database, config)
        after = Configuration(database).get('provider-logins.json')
        self.assertEqual(after['revision'], before['revision'] + 1)
        self.assertNotEqual(after['settings'], before['settings'])

    def test_leftovers_from_earlier_versions_are_removed_on_the_next_write(self):
        self.store.put('FORGE_SETUP_BUILDER_KEY', 'synthetic-key')
        orphan = self.store.root / ('a' * 32); orphan.write_text('old-synthetic-secret'); orphan.chmod(0o600)
        pending = self.store.root / 'pending-login-codex.json'; pending.write_text('{}')
        self.store.put('FORGE_SETUP_REVIEWER_KEY', 'synthetic-key-2')
        self.assertFalse(orphan.exists())
        self.assertTrue(pending.exists())

    def test_scoped_environment_reads_only_the_named_reference(self):
        self.store.put('FORGE_SETUP_BUILDER_KEY', 'builder-synthetic')
        self.store.put('FORGE_SETUP_REVIEWER_KEY', 'reviewer-synthetic')
        env = self.store.environment(only=['FORGE_SETUP_REVIEWER_KEY'])
        self.assertEqual(env['FORGE_SETUP_REVIEWER_KEY'], 'reviewer-synthetic')
        self.assertNotIn('FORGE_SETUP_BUILDER_KEY', env)

    def test_stored_credentials_take_precedence_over_the_process_environment(self):
        self.store.put('FORGE_SETUP_BUILDER_KEY', 'stored-synthetic')
        with patch.dict('os.environ', {'FORGE_SETUP_BUILDER_KEY': 'environment-synthetic'}):
            self.assertEqual(self.store.environment()['FORGE_SETUP_BUILDER_KEY'], 'stored-synthetic')

    def test_role_keys_never_fall_back_to_the_process_environment(self):
        with patch.dict('os.environ', {'FORGE_SETUP_REVIEWER_KEY': 'environment-synthetic', 'PATH': '/usr/bin'}):
            env = self.store.environment()
            self.assertNotIn('FORGE_SETUP_REVIEWER_KEY', env)
            self.assertEqual(env['PATH'], '/usr/bin')
            self.store.put('FORGE_SETUP_BUILDER_KEY', 'stored-synthetic')
            self.assertNotIn('FORGE_SETUP_REVIEWER_KEY', self.store.environment())


class BrowserSecretTests(unittest.TestCase):
    def test_onboarding_api_no_longer_accepts_provider_keys(self):
        session = signed_in(self)
        with tempfile.TemporaryDirectory() as directory, patch.object(dashboard, 'WD', directory), \
                patch('forge.onboarding.Onboarding') as setup:
            handler = object.__new__(dashboard.H)
            handler.server = Mock(server_address=('127.0.0.1', 8787))
            payload = json.dumps({'action': 'provider', 'api_key': 'synthetic'}).encode()
            handler.headers = {'Host': '127.0.0.1:8787', 'Origin': 'http://127.0.0.1:8787', 'X-Forge-Action': OPERATOR_CSRF,
                               'Content-Type': 'application/json', 'Content-Length': str(len(payload)), **session}
            handler.path = '/api/onboarding'; handler.rfile = io.BytesIO(payload)
            handler._send = Mock(); handler.send_error = Mock()
            handler.do_POST()
            result = json.loads(handler._send.call_args.args[0])
            self.assertFalse(result['ok'])
            self.assertIn('Invalid setup action', result['reason'])
            setup.return_value.connect.assert_not_called()


if __name__ == '__main__':
    unittest.main()


class SeccompDefaultTests(unittest.TestCase):
    def runner(self, environ):
        from forge.factory.runner import ContainerRunner
        runner = object.__new__(ContainerRunner)
        runner.environ = environ
        return runner

    def test_runner_uses_the_installed_policy_without_an_environment_variable(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {'seccomp_profile_env': 'FORGE_RUNNER_SECCOMP'}
            self.assertEqual(self.runner({'FORGE_CREDENTIAL_ROOT': directory}).seccomp_policy(config),
                             Path(directory) / 'runner-seccomp.json')
            self.assertEqual(self.runner({'FORGE_CREDENTIAL_ROOT': directory, 'FORGE_RUNNER_SECCOMP': '/custom.json'}).seccomp_policy(config),
                             Path('/custom.json'))
            self.assertEqual(self.runner({}).seccomp_policy(config), Path(''))
