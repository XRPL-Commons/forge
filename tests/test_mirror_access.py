from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.app_access import AccessError, AppClient
from forge.credentials import Credentials
from forge.mirror_daemon import MirrorDaemon
from forge.mirror.policy import MirrorError
from forge.mirror_access import MirrorAccess
from forge.mirror.policy import Repository


class MirrorAppAccessTests(unittest.TestCase):
    def setUp(self):
        self.settings = Mock()
        self.settings.load.return_value = {'connections': {'controller': {
            'app_id': 30, 'private_key_env': 'CONTROLLER_KEY', 'installation_ids': [20]}}}
        self.client = Mock()
        self.client.jwt.return_value = 'synthetic-jwt'
        self.access = MirrorAccess(self.settings, client=self.client)
        self.installation = {'id': 20, 'app_id': 30, 'suspended_at': None,
            'account': {'id': 10, 'type': 'Organization'}, 'permissions': {'contents': 'write'}}
        self.repository = {'id': 12, 'full_name': 'example/project', 'private': True,
            'owner': {'id': 10, 'type': 'Organization'}, 'default_branch': 'main'}
        self.calls = []
        self.client.request.side_effect = self.request

    def request(self, method, path, token, payload=None):
        self.calls.append((method, path, token, payload))
        if path == '/app/installations/20':
            return self.installation
        if path == '/app/installations/20/access_tokens':
            return {'token': 'synthetic-read-token', 'permissions': {'contents': 'read', 'metadata': 'read'}}
        if path == '/installation/token':
            return None
        if path == '/repos/example/project':
            return self.repository
        raise AssertionError((method, path))

    def daemon(self, root):
        self.credentials = Credentials(root)
        self.settings.load.return_value['connections']['controller']['private_key_env'] = 'FORGE_SETUP_APP_KEY'
        def sign(argv, **kwargs):
            # Exercise real AppClient.jwt key lookup without creating real keys.
            self.assertEqual(Path(argv[-1]).read_text(), self.expected_key)
            return SimpleNamespace(returncode=0, stdout=b'fixture-signature')
        def client(*, environ=None):
            return AppClient(environ=self.credentials.environment() if environ is None else environ,
                             run=sign, request=self.request)
        # Settings.client is cached in production. Retain this startup snapshot
        # so the test reproduces a daemon created before onboarding/rotation.
        settings = SimpleNamespace(authority=SimpleNamespace(root=root),
                                   load=self.settings.load, client=client())
        patched = patch('forge.mirror_access.AppClient', side_effect=client)
        patched.start(); self.addCleanup(patched.stop)
        return MirrorDaemon(root, Mock(), settings=settings, git_store=Mock())

    def test_daemon_constructed_before_onboarding_uses_new_app_key(self):
        with TemporaryDirectory() as directory:
            daemon = self.daemon(Path(directory))
            with self.assertRaisesRegex(MirrorError, 'mirror_unavailable'):
                daemon.resolve('example/project')
            self.expected_key = 'synthetic-initial-private-key'
            self.credentials.put('FORGE_SETUP_APP_KEY', self.expected_key, file=True)
            self.assertEqual(daemon.resolve('example/project').repository_id, 12)
            self.assertEqual(len([c for c in self.calls if c[0]=='POST']),
                             len([c for c in self.calls if c[0]=='DELETE']))

    def test_daemon_survives_key_rotation_and_still_refuses_revoked_installation(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            self.expected_key = 'synthetic-original-private-key'
            Credentials(root).put('FORGE_SETUP_APP_KEY', self.expected_key, file=True)
            daemon = self.daemon(root)
            repo = daemon.resolve('example/project')
            self.expected_key = 'synthetic-replacement-private-key'
            self.credentials.put('FORGE_SETUP_APP_KEY', self.expected_key, file=True)
            self.assertEqual(daemon.resolve('example/project'), repo)
            with daemon.credentials(repo) as token:
                self.assertEqual(token, 'synthetic-read-token')
            self.assertEqual(len([c for c in self.calls if c[0]=='POST']),
                             len([c for c in self.calls if c[0]=='DELETE']))
            self.installation['suspended_at'] = 'now'
            count = len(self.calls)
            with self.assertRaises(MirrorError): daemon.resolve('example/project')
            self.assertFalse(any(c[0]=='POST' for c in self.calls[count:]))
            self.installation['suspended_at'] = None
            self.settings.load.return_value['connections'] = {}
            self.repository['private'] = True
            with self.assertRaises(MirrorError): daemon.resolve('example/project')

    def test_fetch_credential_is_repo_scoped_read_only_and_revoked_on_failure(self):
        repo = self.access.resolve('example/project')
        self.assertEqual(repo.repository_id, 12)
        with self.assertRaisesRegex(RuntimeError, 'fetch stopped'):
            with self.access.credentials(repo) as token:
                self.assertEqual(token, 'synthetic-read-token')
                raise RuntimeError('fetch stopped')
        minted = [call[3] for call in self.calls if call[0] == 'POST']
        self.assertTrue(minted)
        self.assertTrue(all(row['permissions'] == {'contents': 'read', 'metadata': 'read'} for row in minted))
        self.assertEqual(minted[-1]['repository_ids'], [12])
        self.assertEqual(len(minted), len([call for call in self.calls if call[:2] == ('DELETE', '/installation/token')]))

    def test_public_repo_does_not_claim_an_installation_grant(self):
        self.repository.update(private=False, owner={'id': 999, 'type': 'User'})
        repo = self.access.resolve('example/project')
        self.assertIsNone(repo.installation_id)
        with self.access.credentials(repo) as token:
            self.assertIsNone(token)

    def test_personal_namespace_never_becomes_organization_default(self):
        self.installation['account']['type'] = 'User'
        self.assertEqual(self.access.organizations(), [])

    def test_transfer_between_resolution_and_fetch_is_refused(self):
        repo = self.access.resolve('example/project')
        self.repository['id'] = 13
        with self.assertRaisesRegex(AccessError, 'access changed'):
            with self.access.credentials(repo):
                self.fail('Changed repository must not receive a fetch credential')

    def test_unconfirmed_revocation_is_not_treated_as_inaccessible_repository(self):
        original = self.client.request.side_effect
        def request(method, path, token, payload=None):
            if path == '/installation/token':
                raise AccessError('revocation unavailable', status=403)
            return original(method, path, token, payload)
        self.client.request.side_effect = request
        with self.assertRaisesRegex(AccessError, 'revocation unavailable'):
            self.access.resolve('example/project')
        self.assertFalse(any(call[2] is None for call in self.calls))

    def test_suspended_installation_is_not_served_from_cached_access(self):
        self.installation['suspended_at'] = 'now'
        with self.assertRaisesRegex(AccessError, 'access changed'):
            self.access.resolve('example/project')
        self.assertFalse(any(call[0] == 'POST' for call in self.calls))


if __name__ == '__main__':
    unittest.main()
