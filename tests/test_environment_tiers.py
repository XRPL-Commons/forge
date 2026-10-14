import unittest
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.contracts import result
from forge.factory.environments import choose, inspect_files, probe, worker_config

FULL = {'image': 'sha256:' + 'b' * 64, 'memory': '6g', 'cpus': 3}
PROFILE = {'services': ['mongodb'], 'evidence': ['api/package.json: mongoose'],
           'head': 'a' * 40, 'confirmed_at': 1}


class SelectionTests(unittest.TestCase):
    def test_confirmation_limits_services_and_explicit_labels_win(self):
        item = {'repo_id': 1}
        config = validate({'environment_tiers': {'full': FULL}})
        issue = {'title': 'Run API tests', 'labels': []}
        self.assertEqual(choose(config, item, issue)['services'], [])
        config['repository_environments']['1'] = PROFILE
        selected = choose(config, item, issue)
        self.assertEqual((selected['tier'], selected['services']), ('full', ['mongodb']))
        self.assertEqual(choose(config, item, {**issue, 'title': 'Copy-only: fix the welcome wording'})['tier'], 'standard')
        explicit = {**issue, 'labels': [{'name': 'env:standard'}]}
        self.assertEqual(choose(config, item, explicit)['tier'], 'standard')
        item['environment_retry'] = {'services': ['mongodb']}
        self.assertEqual(choose(config, item, explicit)['tier'], 'standard')
        for labels in [['env:full', 'env:standard'], ['env:unknown']]:
            with self.assertRaises(ValueError):
                choose(config, item, {**issue, 'labels': [{'name': l} for l in labels]})
        with self.assertRaises(ValueError):
            choose(validate({}), item, {**issue, 'labels': [{'name': 'env:full'}]})

    def test_issue_requirements_choose_services_without_a_saved_profile(self):
        config = validate({'environment_tiers': {'full': FULL}})
        evidence = {'services': ['mongodb', 'redis'], 'head': 'a' * 40, 'evidence': ['package.json: mongodb']}
        item = {'repo_id': 1}
        for title, body, expected in [
                ('Fix navbar spacing', '', []),
                ('Docs: describe MongoDB setup', '', []),
                ('Fix MongoDB connection', '', ['mongodb']),
                ('Fix cache invalidation', 'Redis keys expire too early', ['redis']),
                ('Run API integration tests', '', ['mongodb', 'redis'])]:
            with self.subTest(title=title):
                selected = choose(config, item, {'title': title, 'body': body, 'labels': []}, evidence)
                self.assertEqual(selected['services'], expected)
                self.assertTrue(selected['automatic'])
        self.assertEqual(config['repository_environments'], {})

    def test_legacy_profiles_are_evidence_and_new_defaults_are_explicit(self):
        config = validate({'environment_tiers': {'full': FULL}, 'repository_environments': {'1': PROFILE}})
        item = {'repo_id': 1}
        ui_issue = {'title': 'Fix navigation spacing', 'labels': []}
        self.assertEqual(choose(config, item, ui_issue)['services'], [])
        self.assertEqual(choose(config, item, {'title': 'Fix API tests', 'labels': []})['services'], ['mongodb'])
        config['repository_environments']['1']['default'] = True
        self.assertEqual(choose(config, item, ui_issue)['services'], ['mongodb'])

    def test_issue_override_wins_over_requirements_and_defaults(self):
        config = validate({'environment_tiers': {'full': FULL}, 'repository_environments': {'1': PROFILE}})
        issue = {'title': 'Fix MongoDB', 'labels': []}
        item = {'repo_id': 1, 'environment_override': {'mode': 'standard', 'services': []}}
        self.assertEqual(choose(config, item, issue)['services'], [])
        self.assertFalse(choose(config, item, issue)['automatic'])
        item['environment_override'] = {'mode': 'full', 'services': ['redis']}
        self.assertEqual(choose(config, item, issue)['services'], ['redis'])

    def test_worker_uses_selected_image_and_limits(self):
        config = validate({'environment_tiers': {'full': FULL}})
        worker = worker_config(config, {'environment_choice': {'tier': 'full', 'services': ['mongodb']}})
        self.assertEqual({k: worker[k] for k in FULL}, FULL)
        self.assertEqual(worker['services'], ['mongodb'])

    def test_config_rejects_unpinned_images_unknown_services_and_unconfirmed_profiles(self):
        for value in [
            {'environment_tiers': {'full': {**FULL, 'image': 'runner:latest'}}},
            {'environment_tiers': {'privileged': FULL}},
            {'repository_environments': {'1': {**PROFILE, 'services': ['docker']}}},
            {'repository_environments': {'1': {**PROFILE, 'confirmed_at': 0}}},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate(value)

    def test_probe_cites_package_test_and_ci_sources_without_running_them(self):
        proposal = inspect_files({'api/package.json': '{"dependencies":{"mongoose":"^8"}}',
                                  'test/setup.ts': "import { MongoMemoryServer } from 'mongodb-memory-server'",
                                  '.github/workflows/test.yml': 'services:\n  cache:\n    image: redis:7',
                                  'compose.yml': 'image: postgres:17'})
        self.assertEqual(proposal['services'], ['mongodb', 'postgres', 'redis'])
        self.assertIn('api/package.json: declares or references mongodb', proposal['evidence'])
        github = Mock()
        github.target.return_value = 'main'
        github.call.side_effect = [{'sha': 'a' * 40}, {'truncated': True, 'tree': []}]
        with self.assertRaisesRegex(ValueError, 'incomplete'):
            probe(github, {'repo': 'example/project', 'repo_id': 1})


class ServiceResultTests(unittest.TestCase):
    def test_services_result_requires_a_nonempty_supported_list(self):
        job = {'id': 'attempt-1', 'role': 'build'}
        report = {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'blocked_environment',
                  'blocker': 'services', 'summary': 'Database missing'}
        for value in [None, [], ['unknown'], ['mongodb', 'mongodb']]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                result({**report, 'required_services': value}, job)
        self.assertEqual(result({**report, 'required_services': ['mongodb']}, job)['blocker'], 'services')
