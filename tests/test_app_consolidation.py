import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from forge.app_access import APP_PERMISSIONS, ROLE_PERMISSIONS, shared_app
from forge.app_consolidation import Consolidation, ConsolidationError, plan
from forge.settings import Settings, ROLES, digest, retired_for, validate
from tests.test_settings import configured
from tests.test_sqlite_identity import selected_store


class SingleAppValidationTests(unittest.TestCase):
    def test_every_role_may_share_the_controller_connection(self):
        config = configured()
        config['connections'] = {role: copy.deepcopy(config['connections']['controller']) for role in ROLES}
        validate(config)
        self.assertEqual(shared_app(config['connections']), config['connections']['controller'])

    def test_partial_sharing_with_controller_is_a_resumable_state(self):
        config = configured()
        config['connections']['build'] = copy.deepcopy(config['connections']['controller'])
        validate(config)
        self.assertIsNone(shared_app(config['connections']))

    def test_sharing_any_app_but_the_controller_or_a_different_installation_is_refused(self):
        config = configured()
        config['connections']['review'] = copy.deepcopy(config['connections']['build'])
        with self.assertRaisesRegex(ValueError, 'one Forge App'):
            validate(config)
        config = configured()
        config['connections']['build'] = {**config['connections']['controller'], 'installation_id': 999}
        with self.assertRaisesRegex(ValueError, 'one Forge App'):
            validate(config)

    def test_retired_apps_are_history_never_active(self):
        config = configured()
        config['retired_apps'] = [{'app_id': 77, 'slug': 'forge-review', 'roles': ['review'], 'retired_at': 1}]
        validate(config)
        self.assertEqual([row['app_id'] for row in retired_for(config, 'review')], [77])
        self.assertEqual(retired_for(config, 'audit'), [])
        config['retired_apps'][0]['app_id'] = config['connections']['build']['app_id']
        with self.assertRaisesRegex(ValueError, 'retired App'):
            validate(config)
        config['retired_apps'] = [{'app_id': 77, 'slug': 'x', 'roles': ['steward'], 'retired_at': 1}]
        with self.assertRaises(ValueError):
            validate(config)


class ConsolidationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        selected_store(self.root)
        self.now = 1000
        self.client = Mock()
        self.permissions = {role: dict(ROLE_PERMISSIONS[role]) for role in ROLES}
        self.permissions['controller'] = dict(APP_PERMISSIONS)
        def discover(connection):
            role = ROLES[connection['app_id'] - 1]
            return {'repositories': [{'id': 10, 'name': 'example/project', 'archived': False}],
                    'permissions': self.permissions[role], 'account': 'example', 'app_slug': 'forge-' + role,
                    'installation_url': 'https://github.com/settings/installations/1'}
        self.client.discover.side_effect = discover
        self.settings = Settings(self.root, client=self.client, clock=lambda: self.now)
        config = configured()
        config['repositories'] = [{'id': 10, 'name': 'example/project', 'enabled': True}]
        self.settings.save_config(config)
        self.settings.refresh()
        self.service = Consolidation(self.settings, clock=lambda: self.now)

    def test_plan_retires_each_role_app_and_keeps_its_slug(self):
        result = self.service.plan()
        self.assertEqual(result['status'], 'ready')
        self.assertEqual([(row['app_id'], row['slug'], row['roles']) for row in result['retire']],
                         [(1, 'forge-build', ['build']), (2, 'forge-review', ['review']), (3, 'forge-audit', ['audit'])])

    def test_apply_is_resumable_and_keeps_admission_without_a_new_sync(self):
        before = self.settings.view()['repositories'][0]['access']
        self.assertEqual(set(before.values()), {'ready'})
        result = self.service.apply(self.service.plan()['revision'])
        self.assertEqual(result['status'], 'complete')
        config = self.settings.load()
        self.assertIsNotNone(shared_app(config['connections']))
        self.assertEqual({row['app_id'] for row in config['retired_apps']}, {1, 2, 3})
        # The shared observation is reused immediately: no dispatch pause.
        self.assertEqual(set(self.settings.view()['repositories'][0]['access'].values()), {'ready'})
        # Replaying the stale plan or planning again is a no-op, never a second retirement.
        stale = digest(configured() | {'repositories': config['repositories']})
        self.assertEqual(self.service.apply(stale)['status'], 'complete')
        self.assertEqual(self.service.plan()['status'], 'complete')
        self.assertEqual(self.settings.load(), config)
        # The next sync observes the single App once, not once per role.
        self.client.discover.reset_mock()
        self.settings.refresh()
        self.assertEqual(self.client.discover.call_count, 1)

    def test_missing_app_permissions_block_with_the_live_approval_named(self):
        self.permissions['controller'] = dict(ROLE_PERMISSIONS['controller'])
        self.permissions['controller'].pop('checks')
        self.settings.refresh()
        result = self.service.plan()
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(result['blockers'][0]['code'], 'app_permissions_required')
        self.assertIn('checks: write', result['blockers'][0]['missing'])
        with self.assertRaises(ConsolidationError) as raised:
            self.service.apply(result['revision'])
        self.assertEqual(raised.exception.code, 'app_permissions_required')
        self.assertNotIn('retired_apps', self.settings.load())

    def test_uncovered_enabled_repository_blocks(self):
        config = self.settings.load()
        observation = self.settings.observation()
        observation['roles']['controller']['repositories'] = []
        result = plan(config, observation, now=self.now)
        self.assertEqual([b['code'] for b in result['blockers']], ['installation_coverage'])

    def test_changed_settings_need_a_new_plan(self):
        revision = self.service.plan()['revision']
        config = self.settings.load()
        config['sync_interval_seconds'] = 600; config['access_max_age_seconds'] = 1200
        self.settings.save_config(config)
        with self.assertRaises(ConsolidationError) as raised:
            self.service.apply(revision)
        self.assertEqual(raised.exception.code, 'plan_changed')


if __name__ == '__main__':
    unittest.main()
