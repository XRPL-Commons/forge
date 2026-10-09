"""Real selected setup state, with GitHub as the only fake boundary."""
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit

from forge.installation_apps import RoleAppHandoff
from forge.onboarding import Onboarding
from forge.app_access import ROLE_PERMISSIONS
from forge.operator_auth import sign_in_destination
from tests.test_sqlite_identity import selected_store


class RoleHandoffTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = selected_store(self.root)
        self.now = 1000
        self.owner = {'id': 99, 'login': 'example', 'type': 'Organization'}
        self.api = Mock()
        self.api.jwt.return_value = 'fixture-app-jwt'
        self.app = {'id': 44, 'slug': 'forge-fixture', 'pem': 'fixture PRIVATE KEY-----',
                    'owner': self.owner.copy(), 'permissions': ROLE_PERMISSIONS['build']}
        def request(method, path, token, *args):
            if path == '/users/example': return self.owner.copy()
            if path.startswith('/app-manifests/'): return self.app.copy()
            if path == '/app': return self.app.copy()
            raise AssertionError((method, path))
        self.api.request.side_effect = request
        self.setup = Onboarding(self.root, client=self.api, clock=lambda: self.now)
        self.auth = Mock()
        self.auth.config.return_value = {'origin': 'https://forge.example', 'operators': [7, 8]}
        self.handoff = RoleAppHandoff(self.root, setup=self.setup, auth=self.auth, clock=lambda: self.now)
        self.generation = self.setup.read()['generation']

    def begin(self, **overrides):
        args = dict(setup_id='a'*32, actor_id=7, owner=self.owner, role='build', generation=self.generation)
        return self.handoff.begin(**{**args, **overrides})

    def token(self, result):
        return parse_qs(urlsplit(result['url']).query)['state'][0]

    def test_idempotent_bound_registration_converts_once_with_no_public_secret(self):
        result = self.begin(); self.assertEqual(result, self.begin())
        state = self.token(result)
        page = self.handoff.page(state, 7)
        self.assertIn('Continue on GitHub', page)
        self.assertIn('https://github.com/organizations/example/settings/apps/new', page)
        self.assertIn('/onboarding/cli/github/callback', page)
        self.assertNotIn('PRIVATE KEY', page)
        self.assertNotIn('browser', str(result))
        self.assertEqual(self.handoff.callback(state, 'conversion-code', 7),
                         'https://github.com/apps/forge-fixture/installations/new')
        self.assertEqual(self.setup.read()['apps']['build']['connection']['app_id'], 44)
        self.assertEqual(self.begin()['status'], 'app_registered')
        with self.assertRaises(ValueError): self.handoff.callback(state, 'conversion-code', 7)
        self.assertEqual(sum(call.args[1].startswith('/app-manifests/') for call in self.api.request.call_args_list), 1)

    def test_other_operator_expiry_reset_and_replaced_challenge_are_rejected(self):
        state = self.token(self.begin())
        for operation in (lambda: self.handoff.page(state, 8), lambda: self.handoff.callback(state, 'code', 8)):
            with self.assertRaises(ValueError): operation()
        self.now += 601
        with self.assertRaises(ValueError): self.handoff.page(state, 7)
        state = self.token(self.begin())
        with self.setup.store.transaction() as data: data['generation'] = 'replacement'
        with self.assertRaises(ValueError): self.handoff.callback(state, 'code', 7)

    def test_owner_id_and_role_binding_cannot_be_substituted(self):
        state = self.token(self.begin())
        self.app['owner'] = {**self.owner, 'id': 100}
        with self.assertRaisesRegex(ValueError, 'owner or role'): self.handoff.callback(state, 'code', 7)
        self.assertEqual(self.setup.read()['apps'], {})
        # A failed conversion was consumed; an old code is not retried.
        with self.assertRaises(ValueError): self.handoff.callback(state, 'code', 7)
        replacement = self.token(self.begin())
        self.assertNotEqual(replacement, state)
        self.assertIn('Continue on GitHub', self.handoff.page(replacement, 7))

    def test_new_role_invalidates_previous_handoff_and_revocation_blocks_read(self):
        state = self.token(self.begin())
        self.begin(role='review')
        with self.assertRaises(ValueError): self.handoff.page(state, 7)
        new_state = self.token(self.begin(role='review'))
        self.auth.config.return_value['operators'] = [8]
        with self.assertRaises(ValueError): self.handoff.page(new_state, 7)

    def test_sign_in_return_target_is_exact_and_local(self):
        target = '/onboarding/cli/github/start?state=' + 'a'*43
        self.assertTrue(sign_in_destination(target))
        for value in [target+'&next=https://evil.example', '//evil.example', target.replace('start','callback'),
                      target+'#evil', target.replace('state=', 'state=%2f'), '/onboarding/cli/github/start']:
            self.assertFalse(sign_in_destination(value))
