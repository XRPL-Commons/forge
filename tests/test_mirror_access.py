from contextlib import contextmanager
import unittest
from unittest.mock import Mock

from forge.app_access import AccessError
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
