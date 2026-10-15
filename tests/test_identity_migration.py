"""The whole move is resumable and keeps identities, grants and history."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from forge.app_access import APP_PERMISSIONS, ROLE_PERMISSIONS
from forge.app_access import shared_app
from forge.identity_migration import IdentityMigration, MigrationError, main
from forge.operator_auth import FLOW_COOKIE, SESSION_COOKIE, OperatorAuth, read_cookie
from forge.settings import ROLES, Settings
from tests.test_settings import configured
from tests.test_sqlite_identity import selected_store


class IdentityMigrationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.database = selected_store(self.root)
        self.now = 1000
        self.client = Mock()
        self.client.jwt.return_value = 'jwt'
        self.client.request.side_effect = lambda method, path, token, payload=None: (
            {'id': 4, 'client_id': 'Iv23liForgeApp'} if path == '/app' else None)
        def discover(connection):
            role = ROLES[connection['app_id'] - 1]
            return {'repositories': [{'id': 10, 'name': 'example/project'}], 'account': 'example',
                    'permissions': APP_PERMISSIONS if role == 'controller' else ROLE_PERMISSIONS[role],
                    'app_slug': 'forge-' + role, 'installation_url': 'https://github.com/settings/installations/1'}
        self.client.discover.side_effect = discover
        self.settings = Settings(self.root, client=self.client, clock=lambda: self.now)
        config = configured()
        config['repositories'] = [{'id': 10, 'name': 'example/project', 'enabled': True}]
        self.settings.save_config(config)
        self.settings.refresh()
        self.auth = OperatorAuth(self.root, clock=lambda: self.now, identity=lambda *a: {'id': 123, 'login': 'operator'})
        self.auth.configure('https://forge.example', 'legacy-oauth', 'o' * 40, [123])
        url, cookie = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.session = read_cookie(self.auth.callback(state, 'code', read_cookie(cookie, FLOW_COOKIE)), SESSION_COOKIE)
        self.migration = IdentityMigration(self.root, settings=self.settings, auth=self.auth)

    def history(self):
        with self.database.read_snapshot() as db:
            return {table: db.execute(f'SELECT count(*) FROM {table}').fetchone()[0]
                    for table in ('operator_identities', 'operator_audit', 'external_effects', 'client_connections')}

    def test_plan_names_every_step_and_the_live_approvals(self):
        plan = self.migration.plan(app_client_id=self.migration.app_client_id())
        self.assertEqual(plan['steps'], {'apps': 'ready', 'sign_in': 'pending', 'tailscale': 'pending'})
        self.assertEqual({a['code'] for a in plan['approvals']}, {'app_client_secret', 'tailscale_serve'})

    def test_steps_resume_in_any_order_and_preserve_history(self):
        before = self.history()
        plan = self.migration.plan(app_client_id='Iv23liForgeApp')
        # A crash after the sign-in step: the apps step still applies later.
        self.migration.apply_sign_in('a' * 40)
        self.assertEqual(self.migration.apply_sign_in('a' * 40), {'status': 'unchanged'})
        with patch('forge.repository_setup.RepositorySetup') as setup:
            setup.return_value.check.return_value = True
            self.assertEqual(self.migration.apply_apps(plan['revision'])['protection_check'], 'complete')
            self.migration.apply_apps(plan['revision'])
            self.assertEqual(setup.return_value.check.call_count, 2)
        self.migration.apply_tailscale()
        self.assertEqual(self.migration.apply_tailscale(), {'mode': 'tailscale', 'changed': False})
        after = self.migration.plan(app_client_id='Iv23liForgeApp')
        self.assertEqual(after['steps'], {'apps': 'complete', 'sign_in': 'complete', 'tailscale': 'complete'})
        self.assertIsNotNone(shared_app(self.settings.load()['connections']))
        self.assertEqual({row['app_id'] for row in self.settings.load()['retired_apps']}, {1, 2, 3})
        # Operators, sessions and every earlier record survive; only audit rows were added.
        self.assertEqual(self.auth.session(self.session)['operator']['id'], 123)
        current = self.history()
        for table in ('operator_identities', 'external_effects', 'client_connections'):
            self.assertEqual(current[table], before[table])
        self.assertGreater(current['operator_audit'], before['operator_audit'])

    def test_skipped_protection_refresh_is_reported_pending_and_repeatable(self):
        plan = self.migration.plan(app_client_id='Iv23liForgeApp')
        with patch.object(self.settings, 'refresh', return_value=False), \
                patch('forge.repository_setup.RepositorySetup') as setup:
            self.assertEqual(self.migration.apply_apps(plan['revision'])['protection_check'], 'pending')
            setup.assert_not_called()
        with patch('forge.repository_setup.RepositorySetup') as setup:
            setup.return_value.check.return_value = True
            self.assertEqual(self.migration.apply_apps(plan['revision'])['protection_check'], 'complete')

    def test_sign_in_only_moves_to_the_forge_apps_own_client(self):
        with self.assertRaises(MigrationError) as raised:
            self.migration.apply_sign_in('a' * 40, client_id='some-other-oauth-app')
        self.assertEqual(raised.exception.code, 'client_mismatch')
        self.assertEqual(self.auth.config()['client_id'], 'legacy-oauth')

    def test_cli_reads_the_secret_from_stdin_only(self):
        with patch('forge.identity_migration.IdentityMigration', return_value=self.migration), \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['sign-in', '--root', str(self.root)], stdin=io.StringIO('a' * 40 + '\n')), 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result, {'ok': True, 'status': 'rotated'})
        self.assertNotIn('a' * 40, output.getvalue())


if __name__ == '__main__':
    unittest.main()
