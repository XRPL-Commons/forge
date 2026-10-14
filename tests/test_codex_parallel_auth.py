import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import io
import tarfile
import unittest
from unittest.mock import Mock, patch

from forge.credentials import Credentials
from forge.factory.codex_auth import snapshot, expires_at, renew_in_container, renewable
from forge.factory.config import validate
from forge.factory.runner import ContainerRunner
from forge.settings import write_json


REF = 'FORGE_SETUP_CODEX_AUTH'


def login(expiry=10000):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': expiry}).encode()).decode().rstrip('=')
    return {'auth_mode': 'chatgpt', 'tokens': {'access_token': 'header.' + payload + '.signature',
            'id_token': 'synthetic-id', 'refresh_token': 'synthetic-refresh', 'account_id': 'account-1'}}


class SharedAuthTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.credentials = Credentials(self.root)
        self.credentials.put(REF, json.dumps(login()), file=True)

    def current(self):
        return json.loads(Path(self.credentials.environment()[REF]).read_text())

    def test_parallel_snapshots_never_contain_refresh_authority(self):
        renew = Mock()
        with ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(lambda _: snapshot(self.root, REF, 5000, renew), range(3)))
        for value in values:
            self.assertEqual(value['auth_mode'], 'chatgptAuthTokens')
            self.assertEqual(value['tokens']['refresh_token'], '')
            self.assertEqual(value['tokens']['access_token'], login()['tokens']['access_token'])
        self.assertTrue(all(c.kwargs['reconcile'] for c in renew.call_args_list))
        self.assertEqual(self.current(), login())

    def test_concurrent_expiring_workers_refresh_only_once(self):
        calls = []
        def renew(directory, *, reconcile):
            if not reconcile:
                calls.append(1)
                write_json(directory / 'auth.json', login(20000))
        with ThreadPoolExecutor(max_workers=3) as pool:
            values = list(pool.map(lambda _: snapshot(self.root, REF, 10000, renew), range(3)))
        self.assertEqual(len(calls), 1)
        self.assertEqual(expires_at(self.current()), 20000)
        self.assertTrue(all(expires_at(v) == 20000 for v in values))

    def test_crash_after_rotation_recovers_without_reusing_old_refresh(self):
        def crash(directory, *, reconcile):
            if not reconcile:
                write_json(directory / 'auth.json', login(20000))
                raise RuntimeError('controller stopped')
        with self.assertRaises(RuntimeError):
            snapshot(self.root, REF, 10000, crash)
        self.assertEqual(expires_at(self.current()), 10000)
        renew = Mock()
        value = snapshot(self.root, REF, 10000, renew)
        self.assertEqual(expires_at(value), 20000)
        self.assertEqual(expires_at(self.current()), 20000)
        self.assertTrue(all(c.kwargs['reconcile'] for c in renew.call_args_list))

    def test_rotated_refresh_with_same_expiry_is_preserved_after_restart(self):
        def crash(directory, *, reconcile):
            if not reconcile:
                value = login(); value['tokens']['refresh_token'] = 'rotated-refresh'
                write_json(directory / 'auth.json', value)
                raise RuntimeError()
        with self.assertRaises(RuntimeError): snapshot(self.root, REF, 10000, crash)
        snapshot(self.root, REF, 5000, Mock())
        self.assertEqual(self.current()['tokens']['refresh_token'], 'rotated-refresh')

    def test_runner_only_delivers_access_snapshot_to_worker(self):
        config = validate({'provider_auth_file_env': REF, 'provider_environment': [],
                           'codex_parallel_auth': True})
        runner = ContainerRunner(config, environ=self.credentials.environment())
        runner.inspect = Mock(return_value={'status': 'created', 'id': 'worker'})
        runner.call = Mock()
        job = {'id': 'job', 'fenced': False, 'deadline': 5000}
        with patch('forge.factory.runner.assignment_note', return_value={}), \
                patch('forge.factory.codex_auth.renew_in_container'):
            runner.prepare(job, 'synthetic-github-token')
        raw = runner.call.call_args.kwargs['input']
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            delivered = json.load(archive.extractfile('credentials.json'))
        self.assertEqual(delivered['FORGE_HARNESS_AUTH']['tokens']['refresh_token'], '')
        self.assertEqual(delivered['GH_TOKEN'], 'synthetic-github-token')
        self.assertNotIn(b'synthetic-refresh', raw)

    def test_reconnect_does_not_restore_old_staged_credentials(self):
        def crash(directory, *, reconcile):
            if not reconcile:
                write_json(directory / 'auth.json', login(30000))
                raise RuntimeError()
        with self.assertRaises(RuntimeError): snapshot(self.root, REF, 10000, crash)
        new = login(20000); new['tokens']['account_id'] = 'account-2'
        self.credentials.put(REF, json.dumps(new), file=True)
        value = snapshot(self.root, REF, 10000, Mock())
        self.assertEqual(value['tokens']['account_id'], 'account-2')
        self.assertEqual(expires_at(value), 20000)

    def test_changed_account_or_insufficient_lifetime_fail_closed(self):
        for value in (login(10001), {**login(20000), 'tokens': {**login(20000)['tokens'], 'account_id': 'other'}}):
            def renew(directory, *, reconcile):
                if not reconcile: write_json(directory / 'auth.json', value)
            with self.assertRaises(ValueError): snapshot(self.root, REF, 10000, renew)
            self.assertEqual(self.current(), login())

    def test_invalid_expiry_is_never_dispatched(self):
        for token in ('opaque', 'x.e30.y', 'x.invalid.y'):
            value = login(); value['tokens']['access_token'] = token
            self.credentials.put(REF, json.dumps(value), file=True)
            with self.assertRaises(ValueError): snapshot(self.root, REF, 5000, Mock())

    def test_access_only_workers_never_persist_back_into_login(self):
        runner = ContainerRunner(validate({'codex_parallel_auth': True}))
        runner.call = Mock(side_effect=AssertionError('must not read worker auth'))
        self.assertEqual(runner.refresh_auth({'id': 'worker'}), 'controller_managed')

    def test_an_access_only_copy_never_replaces_the_managed_login(self):
        # Live incident: parallel auth was switched off while access-only workers ran;
        # their copy was saved back and the login could no longer renew.
        import hashlib
        stored = Path(self.credentials.environment()[REF]).read_bytes()
        copy = {'auth_mode': 'chatgptAuthTokens', 'tokens': {**login()['tokens'], 'refresh_token': ''}}
        def tar(text):
            content = io.BytesIO()
            with tarfile.open(fileobj=content, mode='w') as archive:
                raw = text.encode(); member = tarfile.TarInfo('f'); member.size = len(raw)
                archive.addfile(member, io.BytesIO(raw))
            return Mock(stdout=content.getvalue())
        def call(argv, **kwargs):
            return tar(hashlib.sha256(stored).hexdigest()) if argv[1].endswith('.forge-auth-origin') else tar(json.dumps(copy))
        config = validate({'provider': 'codex', 'provider_auth_file_env': REF, 'provider_environment': []})
        runner = ContainerRunner(config, environ=self.credentials.environment())
        runner.call = Mock(side_effect=call)
        self.assertEqual(runner.refresh_auth({'id': 'worker'}), 'access_only')
        self.assertEqual(self.current(), login())

    def test_renewable_logins(self):
        self.assertIsNone(renewable(login()))
        self.assertEqual(renewable({'auth_mode': 'chatgptAuthTokens', 'tokens': login()['tokens']}), 'access_only')
        self.assertEqual(renewable({'auth_mode': 'chatgpt', 'tokens': {**login()['tokens'], 'refresh_token': ''}}), 'access_only')
        self.assertEqual(renewable(None), 'unreadable')

    def test_refresh_container_uses_no_repository_or_secret_arguments(self):
        runner = Mock()
        runner.config = validate({'image': 'sha256:' + 'a'*64})
        runner.call.side_effect = [Mock(stdout=b''), Mock(), Mock(),
                                  Mock(stdout=json.dumps([{'State': {'Running': False, 'ExitCode': 0}}]).encode()), Mock()]
        renew_in_container(runner, self.root, reconcile=False)
        argv = runner.call.call_args_list[1].args[0]
        self.assertIn('--read-only', argv)
        self.assertIn('--cap-drop', argv)
        self.assertIn('none', argv)
        self.assertNotIn('synthetic-refresh', str(argv))
        self.assertNotIn('/work/repository', str(argv))


if __name__ == '__main__':
    unittest.main()
