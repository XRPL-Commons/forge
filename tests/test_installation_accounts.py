"""Owned detached device login with actual setup credential handoff."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.installation_accounts import DeviceAccount, device_prompt
from forge.onboarding import Onboarding
from forge_client.contracts import SetupError
from tests.test_sqlite_identity import selected_store
from tests.test_codex_parallel_auth import login


class DeviceLoginTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = selected_store(self.root)
        self.setup = Onboarding(self.root)
        config = self.setup.runtime(); config['image'] = 'sha256:' + 'a' * 64
        self.setup.save_runtime(config, active=False)
        self.generation = self.setup.read()['generation']
        self.record = None; self.calls = []; self.now = 1000
        self.output = 'Other private provider output\nhttps://auth.openai.com/codex/device\nABCD-12345\n'
        def docker(argv, **kw):
            self.calls.append(argv)
            action = argv[1]
            out = ''; code = 0
            if action == 'inspect':
                if self.record is None: code = 1
                else: out = json.dumps([self.record])
            elif action == 'create':
                labels = {argv[i+1].split('=', 1)[0]: argv[i+1].split('=', 1)[1] for i, a in enumerate(argv) if a == '--label'}
                options = dict(argv[i+1].split('=', 1) for i, value in enumerate(argv) if value == '--log-opt')
                self.record = {'Id': 'docker-id', 'Config': {'Labels': labels},
                    'HostConfig': {'LogConfig': {'Type': argv[argv.index('--log-driver')+1], 'Config': options}},
                    'State': {'Status': 'created', 'Running': False, 'ExitCode': 0,
                              'StartedAt': '0001-01-01T00:00:00Z', 'Error': ''}}
            elif action == 'start':
                logging = self.record['HostConfig']['LogConfig']
                options = logging['Config']
                # Docker's local driver defaults compression to true and rejects
                # that combination with a single retained log file at task start.
                if (getattr(self, 'start_error', '') or logging['Type'] == 'local'
                        and options.get('max-file') == '1' and options.get('compress', 'true') != 'false'):
                    self.record['State'].update(ExitCode=128, Error=getattr(self, 'start_error', '') or 'invalid logging configuration')
                    code = 1
                else:
                    self.record['State'].update(Status='running', Running=True, StartedAt='2026-10-17T12:00:00Z')
            elif action == 'logs': out = self.output
            elif action == 'rm': self.record = None
            elif action == 'container': out = '' if self.record is None else 'docker-id'
            else: raise AssertionError(argv)
            return Mock(returncode=code, stdout=out, stderr='')
        self.client = DeviceAccount(self.root, setup=self.setup, run=docker, clock=lambda:self.now)

    def call(self, **kw):
        return self.client.call('codex', generation=self.generation, setup_id='a' * 32, **kw)

    def finish(self, auth=None):
        record = self.setup.read()['device_logins']['codex']
        path = self.client.home(record) / '.codex/auth.json'
        path.write_text(json.dumps(auth if auth is not None else login())); path.chmod(0o600)
        self.record['State'].update(Status='exited', Running=False, ExitCode=0)
        return path

    def test_failed_start_is_observed_and_explicit_retry_does_not_wait_for_expiry(self):
        self.start_error = 'private runtime failure detail'
        with self.assertRaises(SetupError) as caught: self.call(apply=True)
        self.assertEqual(caught.exception.code, 'account_runtime_failed')
        saved = self.setup.read()['device_logins']['codex']
        old_home = self.client.home(saved)
        before = len(self.calls)
        observed = self.call()
        self.assertEqual(observed['status'], 'account_login_failed')
        self.assertNotIn(self.start_error, json.dumps(observed))
        self.assertFalse(any(call[1] in ('rm', 'create', 'start') for call in self.calls[before:]))
        self.start_error = ''
        retried = self.call(apply=True, retry=True)
        self.assertEqual(retried['status'], 'account_login_pending')
        self.assertNotEqual(retried['session_id'], saved['id'])
        self.assertFalse(old_home.exists())
        self.assertLess(self.now, saved['expires_at'])

    def test_created_without_error_and_lost_start_reply_are_not_retryable(self):
        original = self.client.run
        def lost_create(argv, **kw):
            result = original(argv, **kw)
            if argv[1] == 'create': raise TimeoutError()
            return result
        self.client.run = lost_create
        with self.assertRaises(TimeoutError): self.call(apply=True)
        self.client.run = original
        before = len(self.calls)
        with self.assertRaises(SetupError) as caught: self.call(apply=True, retry=True)
        self.assertEqual(caught.exception.code, 'account_login_running')
        self.assertFalse(any(call[1] in ('rm', 'create', 'start') for call in self.calls[before:]))
        def lost_start(argv, **kw):
            result = original(argv, **kw)
            if argv[1] == 'start': raise TimeoutError()
            return result
        self.client.run = lost_start
        with self.assertRaises(TimeoutError): self.call(apply=True)
        self.client.run = original
        before = len(self.calls)
        with self.assertRaises(SetupError) as caught: self.call(apply=True, retry=True)
        self.assertEqual(caught.exception.code, 'account_login_running')
        self.assertFalse(any(call[1] in ('rm', 'create', 'start') for call in self.calls[before:]))

    def test_single_file_logs_start_with_compression_disabled(self):
        self.assertEqual(self.call(apply=True)['status'], 'account_login_pending')
        self.assertTrue(self.record['State']['Running'])
        self.assertEqual(self.record['HostConfig']['LogConfig'], {'Type': 'local',
            'Config': {'max-size': '64k', 'max-file': '1', 'compress': 'false'}})

    def test_survives_client_restart_and_imports_renewable_login_privately(self):
        self.assertEqual(self.call()['status'], 'account_login_required')
        self.assertEqual(self.call(apply=True)['status'], 'account_login_pending')
        prompt = self.call()
        self.assertEqual(prompt['code'], 'ABCD-12345')
        self.assertNotIn('Other private', json.dumps(prompt))
        created = next(call for call in self.calls if call[1] == 'create')
        self.assertNotIn('-it', created)
        self.assertIn('--read-only', created)
        self.assertFalse(any('docker.sock' in arg for arg in created))
        path = self.finish()
        self.assertEqual(self.call()['status'], 'account_login_saved')
        result = self.call(apply=True)
        self.assertEqual(result['status'], 'connected')
        self.assertFalse(path.exists())
        self.assertIsNone(self.record)
        self.assertEqual(self.call(apply=True), result)
        config = self.setup.runtime()
        self.assertEqual(config['provider_pool'][0]['provider_auth_file_env'], 'FORGE_SETUP_CODEX_AUTH')
        self.assertNotIn('synthetic-refresh', json.dumps(self.setup.read()))
        self.assertNotIn('synthetic-refresh', json.dumps(result))
        self.assertEqual(sum(call[1] == 'create' for call in self.calls), 1)

    def test_rejects_access_only_login_and_reset_generation(self):
        self.call(apply=True)
        self.finish({'auth_mode': 'chatgptAuthTokens', 'tokens': login()['tokens']})
        with self.assertRaises(SetupError) as error: self.call(apply=True)
        self.assertEqual(error.exception.code, 'renewable_login_required')
        self.assertEqual(self.setup.runtime()['provider_pool'], [])
        with self.setup.store.transaction() as data: data['generation'] = 'new-generation'
        with self.assertRaises(SetupError): self.call(apply=True)

    def test_container_ownership_is_checked_before_mutation(self):
        self.call(apply=True)
        self.record['Config']['Labels']['forge.login-session'] = 'other'
        count = len(self.calls)
        with self.assertRaises(SetupError): self.call(apply=True)
        self.assertFalse(any(call[1] in ('rm', 'start', 'create') for call in self.calls[count:]))

    def test_expired_session_requires_explicit_restart(self):
        first = self.call(apply=True)
        self.now += 901
        self.assertEqual(self.call(apply=True)['status'], 'account_login_failed')
        next_login = self.call(apply=True, retry=True)
        self.assertNotEqual(first['session_id'], next_login['session_id'])
        self.assertEqual(next_login['status'], 'account_login_pending')

    def test_explicit_retry_reconnects_a_completed_but_removed_login(self):
        self.call(apply=True); self.finish(); completed = self.call(apply=True)
        with patch.object(self.setup, 'harnesses', return_value=[{'id': 'codex', 'connected': False}]):
            self.assertEqual(self.call()['status'], 'account_login_failed')
            retried = self.call(apply=True, retry=True)
            self.assertEqual(retried['status'], 'account_login_pending')
            self.assertNotEqual(retried['session_id'], completed['session_id'])

    def test_lost_create_reply_reconciles_the_same_owned_container(self):
        original = self.client.run
        def lost(argv, **kw):
            result = original(argv, **kw)
            if argv[1] == 'create': raise TimeoutError()
            return result
        self.client.run = lost
        with self.assertRaises(TimeoutError): self.call(apply=True)
        self.client.run = original
        self.assertEqual(self.call(apply=True)['status'], 'account_login_pending')
        self.assertEqual(sum(call[1] == 'create' for call in self.calls), 1)

    def test_lost_cleanup_reply_does_not_reinstall_or_require_another_login(self):
        self.call(apply=True); self.finish()
        original = self.client.run
        def lost(argv, **kw):
            result = original(argv, **kw)
            if argv[1] == 'rm': raise TimeoutError()
            return result
        self.client.run = lost
        with self.assertRaises(TimeoutError): self.call(apply=True)
        self.client.run = original
        before = self.setup.credentials.login_generations()
        self.assertEqual(self.call(apply=True)['status'], 'connected')
        self.assertEqual(self.setup.credentials.login_generations(), before)

    def test_reset_removes_only_owned_device_homes(self):
        from forge.harnesses.accounts import reset_logins
        self.call(apply=True)
        home = self.client.home(self.setup.read()['device_logins']['codex'])
        other = self.root / 'credentials/unrelated'; other.mkdir()
        with patch('forge.harnesses.accounts.subprocess.run', return_value=Mock(stdout='')):
            reset_logins(self.root)
        self.assertFalse(home.exists())
        self.assertTrue(other.is_dir())

    def test_prompt_parser_never_exports_arbitrary_urls_or_token_lines(self):
        self.assertIsNone(device_prompt('https://evil.example\nABCD-12345'))
        self.assertIsNone(device_prompt('https://auth.openai.com/codex/device\nsecret ABCD-12345 extra'))
        result = device_prompt('https://auth.openai.com/codex/device\n\x1b[1mABCD-12345\x1b[0m\n')
        self.assertEqual(result['code'], 'ABCD-12345')


if __name__ == '__main__': unittest.main()
