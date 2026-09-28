import unittest
from unittest.mock import Mock, patch

from forge.repository_environments import RepositoryEnvironments, SCAN_TIMEOUT
from forge.repository_setup import RepositorySetup
from forge.settings import digest, read_json, write_json
from forge.factory.config import load
from tests import test_settings as settings_tests
from tests.test_environment_tiers import FULL


class RepositoryEnvironmentTests(unittest.TestCase):
    def setUp(self):
        settings_tests.SettingsTests.setUp(self)
        self.settings.refresh()
        self.service = RepositoryEnvironments(self.settings, github_class=Mock())
        self.proposal = {'repo_id': 10, 'repo': 'example/project', 'head': 'a' * 40,
                         'services': ['mongodb'], 'evidence': ['package.json: mongoose'], 'revision': 'proposal-1'}
        write_json(self.root / 'runtime.json', {'environment_tiers': {'full': FULL}})

    def action(self, action, revision=None):
        self.service.act(10, digest(self.settings.load()), revision or self.service.view(10)['revision'], action)

    def start(self):
        with patch('forge.repository_environments.threading.Thread') as thread:
            self.action('scan')
            thread.return_value.start.assert_called_once()
        return read_json(self.root / 'state/environment-scans.json')['10']['id']

    def finish(self, identifier, proposal=None):
        with patch('forge.repository_environments.probe', return_value=proposal or self.proposal):
            self.service.scan({'repo_id': 10, 'repo': 'example/project'}, digest(self.settings.load()), identifier)

    def proposed(self):
        self.finish(self.start())

    def test_scan_is_async_read_only_until_saved_and_profile_survives_reload(self):
        identifier = self.start()
        self.assertEqual(self.service.view(10)['status'], 'running')
        self.assertEqual(load(self.root)['repository_environments'], {})
        self.finish(identifier)
        row = RepositorySetup(self.settings).view(10)['repositories'][0]['environment']
        self.assertEqual(row['proposal']['services'], ['mongodb'])
        self.assertEqual(row['proposal']['evidence'], self.proposal['evidence'])
        self.assertEqual(load(self.root)['repository_environments'], {})
        self.action('confirm')
        reloaded = RepositoryEnvironments(self.settings).view(10)
        self.assertEqual(reloaded['profile']['services'], ['mongodb'])
        self.assertIsNone(reloaded['proposal'])
        self.assertEqual(load(self.root)['environment_tiers']['full'], FULL)

    def test_stale_confirmation_cannot_overwrite_newer_scan_or_saved_profile(self):
        self.proposed()
        old = self.service.view(10)['revision']
        identifier = self.start()
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.action('confirm', old)
        with self.assertRaisesRegex(ValueError, 'Scan this repository'):
            self.action('confirm')
        self.finish(identifier, {**self.proposal, 'services': ['redis'], 'revision': 'proposal-2'})
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.action('confirm', old)
        current = self.service.view(10)['revision']
        self.action('confirm')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.action('confirm', current)
        self.assertEqual(load(self.root)['repository_environments']['10']['services'], ['redis'])

    def test_failed_scan_preserves_saved_requirements_and_can_retry(self):
        self.proposed(); self.action('confirm')
        identifier = self.start()
        with patch('forge.repository_environments.probe', side_effect=ValueError('private diagnostics')):
            self.service.scan({'repo_id': 10, 'repo': 'example/project'}, digest(self.settings.load()), identifier)
        row = self.service.view(10)
        self.assertEqual(row['status'], 'failed')
        self.assertNotIn('private', row['error'])
        self.assertEqual(row['profile']['services'], ['mongodb'])
        self.assertIsNone(row['proposal'])
        self.finish(self.start())
        self.assertEqual(self.service.view(10)['status'], 'complete')

    def test_interrupted_scan_can_be_replaced_and_old_thread_cannot_publish(self):
        old = self.start()
        with self.assertRaisesRegex(ValueError, 'already running'):
            self.start()
        self.now += SCAN_TIMEOUT
        self.assertEqual(self.service.view(10)['status'], 'failed')
        new = self.start()
        self.finish(old)
        self.assertIsNone(self.service.view(10)['proposal'])
        self.finish(new)
        self.assertEqual(self.service.view(10)['proposal'], self.proposal)

    def test_confirm_checks_access_identity_and_host_support(self):
        self.proposed()
        write_json(self.root / 'runtime.json', {})
        with self.assertRaisesRegex(ValueError, 'Database workers are unavailable'):
            self.action('confirm')
        self.assertEqual(load(self.root)['repository_environments'], {})
        with self.assertRaisesRegex(ValueError, 'Refresh GitHub access'):
            self.service.act(99, digest(self.settings.load()), 'x', 'scan')
        with self.assertRaisesRegex(ValueError, 'settings changed'):
            self.service.act(10, 'old', self.service.view(10)['revision'], 'scan')
        write_json(self.root / 'state/factory-paused.json', {})
        with self.assertRaisesRegex(ValueError, 'reset'):
            self.action('confirm')

    def test_no_databases_can_be_saved_and_replaces_previous_profile(self):
        self.proposed(); self.action('confirm')
        self.finish(self.start(), {**self.proposal, 'services': [], 'evidence': []})
        write_json(self.root / 'runtime.json', {'repository_environments': load(self.root)['repository_environments']})
        self.action('confirm')
        self.assertEqual(self.service.view(10)['profile']['services'], [])
        self.assertIsNone(self.service.view(10)['proposal'])

    def test_settings_change_during_scan_discards_result(self):
        identifier = self.start()
        revision = digest(self.settings.load())
        config = self.settings.load(); config['connections']['build']['app_id'] = 99
        write_json(self.settings.path, config)
        with patch('forge.repository_environments.probe', return_value=self.proposal):
            self.service.scan({'repo_id': 10, 'repo': 'example/project'}, revision, identifier)
        self.assertEqual(self.service.view(10)['status'], 'failed')
        self.assertFalse((self.root / 'state/environment-proposals.json').exists())

    def test_existing_cli_proposal_is_reviewable_without_rescanning(self):
        write_json(self.root / 'state/environment-proposals.json', {'10': self.proposal})
        self.assertEqual(self.service.view(10)['proposal'], self.proposal)
        self.action('confirm')
        self.assertEqual(self.service.view(10)['profile']['services'], ['mongodb'])

    def test_poll_does_not_create_scan_state_or_contact_github(self):
        for _ in range(3):
            row = RepositorySetup(self.settings).view(10)['repositories'][0]
            self.assertIsNone(row['environment']['profile'])
        self.assertFalse((self.root / 'state/environment-scans.json').exists())
        self.service.github_class.assert_not_called()
