import copy
import base64
import io
import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from forge.factory.config import validate
from forge.factory.contracts import result
from forge.factory.environments import choose, inspect_files, probe, worker_config
from forge.factory.github import GitHubUnavailable
from tests import test_factory_cycle as cycle
from tests.test_issue_worklist import Settings

FULL = {'image': 'sha256:' + 'b' * 64, 'memory': '6g', 'cpus': 3}
PROFILE = {'services': ['mongodb'], 'evidence': ['api/package.json: mongoose'],
           'head': 'a' * 40, 'confirmed_at': 1}


class SelectionTests(unittest.TestCase):
    def test_probe_has_no_effect_until_its_exact_revision_is_confirmed(self):
        from forge.factory import control
        github = Mock()
        github.target.return_value = 'main'
        github.call.side_effect = [
            {'sha': 'a' * 40},
            {'tree': [{'path': 'package.json', 'type': 'blob', 'size': 50}], 'truncated': False},
            {'encoding': 'base64', 'size': 50, 'content': base64.b64encode(b'{"dependencies":{"mongoose":"8"}}').decode()},
        ]
        with tempfile.TemporaryDirectory() as directory, patch.object(control, 'Settings', return_value=Settings()), \
                patch.object(control, 'GitHub', return_value=github), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            args = ['--root', directory, '--target', '1']
            self.assertEqual(control.main(['environment-probe', *args]), 0)
            self.assertFalse((Path(directory) / 'runtime.json').exists())
            proposal = json.loads((Path(directory) / 'state/environment-proposals.json').read_text())['1']
            self.assertEqual(proposal['services'], ['mongodb'])
            with self.assertRaises(SystemExit):
                control.main(['environment-confirm', *args, '--revision', 'stale'])
            self.assertFalse((Path(directory) / 'runtime.json').exists())
            self.assertEqual(control.main(['environment-confirm', *args, '--revision', proposal['revision']]), 0)
            config = validate(json.loads((Path(directory) / 'runtime.json').read_text()))
            self.assertEqual(config['repository_environments']['1']['services'], ['mongodb'])

    def test_confirmation_limits_services_and_explicit_labels_win(self):
        item = {'repo_id': 1}
        config = validate({'environment_tiers': {'full': FULL}})
        issue = {'title': 'Upgrade dependencies', 'labels': []}
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


class ServiceCycleTests(cycle.CycleTest):
    def setup_services(self, title='Copy-only: update wording', confirmed=True):
        self.config.update(environment_tiers={'full': FULL}, repository_environments={'1': copy.deepcopy(PROFILE)} if confirmed else {})
        self.labels = []
        issue = self.github.issue
        self.github.issue = lambda item: ({**issue(item)[0], 'title': title, 'labels': [{'name': l} for l in self.labels]}, 'requirements')
        self.github.environment_label = lambda item, value, label: setattr(self, 'labels', [label])

    def block_services(self, role='build', required=None):
        job = self.active()
        report = {'version': 1, 'attempt': job['id'], 'role': role, 'outcome': 'blocked_environment',
                  'blocker': 'services', 'required_services': required or ['mongodb'], 'summary': 'MongoDB is unavailable'}
        if role in ('review', 'audit'):
            report['candidate'] = job['candidate']['key']
        self.engine.finish(job, report)

    def test_one_escalation_keeps_branch_and_budget_then_holds_on_full(self):
        self.setup_services()
        self.assertFalse(self.cycle()['errors'])
        first = self.active()
        self.assertEqual(first['runner_config']['environment_tier'], 'standard')
        self.block_services()
        self.assertFalse(self.cycle()['errors'])
        second = self.active()
        self.assertEqual(second['branch'], first['branch'])
        self.assertEqual(second['runner_config']['image'], FULL['image'])
        self.assertEqual(second['runner_config']['memory'], '6g')
        self.assertEqual(second['runner_config']['services'], ['mongodb'])
        self.assertEqual(self.labels, ['env:full'])
        self.block_services()
        for _ in range(3):
            self.assertFalse(self.cycle()['errors'])
        data = self.ledger.read()
        self.assertEqual(len(data['jobs']), 2)
        self.assertEqual(data['issues'][self.key]['failures'], [])
        self.assertEqual(self.state(), 'hold')
        self.assertEqual(data['issues'][self.key]['environment_hold']['required_services'], ['mongodb'])

    def test_unconfirmed_or_unapproved_services_never_escalate(self):
        self.setup_services(confirmed=False)
        self.cycle(); self.block_services(); self.cycle()
        self.assertEqual(self.state(), 'hold')
        self.assertEqual(len(self.ledger.read()['jobs']), 1)

    def test_explicit_standard_label_prevents_automatic_escalation(self):
        self.setup_services(title='Upgrade dependencies')
        self.labels = ['env:standard']
        self.cycle(); self.block_services(); self.cycle()
        self.assertEqual(self.state(), 'hold')
        self.assertEqual(self.labels, ['env:standard'])

    def test_a_lost_label_write_resumes_before_reserving_any_job(self):
        self.setup_services(title='Upgrade dependencies')
        def lost(item, issue, label):
            self.labels = [label]
            raise GitHubUnavailable('Lost response after label was written')
        self.github.environment_label = lost
        self.cycle()
        self.assertFalse(self.ledger.read()['jobs'])
        self.github.environment_label = lambda item, issue, label: setattr(self, 'labels', [label])
        with self.ledger.transaction() as data:
            data['issues'][self.key]['retry_after'] = 0
        self.assertFalse(self.cycle()['errors'])
        self.assertEqual(self.active()['runner_config']['environment_tier'], 'full')
        self.assertTrue(self.ledger.read()['issues'][self.key]['environment_choice']['automatic'])

    def test_services_result_requires_a_nonempty_supported_list(self):
        self.cycle()
        job = self.active()
        report = {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'blocked_environment',
                  'blocker': 'services', 'summary': 'Database missing'}
        for value in [None, [], ['unknown'], ['mongodb', 'mongodb']]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                result({**report, 'required_services': value}, job)
        self.assertEqual(result({**report, 'required_services': ['mongodb']}, job)['blocker'], 'services')
