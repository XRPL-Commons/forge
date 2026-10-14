import copy
import unittest
import tempfile
from pathlib import Path

from forge.factory.config import load, validate
from forge.factory.contracts import result, assignment_note
from forge.factory.harness import schema
from forge.factory.verification import ci_result
from forge.factory.verification import configure
from forge.settings import DEFAULT, Settings, digest
from tests.sqlite_fixture import selected_store
from tests.test_factory_adapters import WorkerContractTests

VERIFY = {'what': 'Build the release image, POST synthetic input to /api/export-pdf, and confirm %PDF-.',
          'why': 'Workers have no Docker socket.', 'checks': []}


class VerificationContractTests(unittest.TestCase):
    def test_operator_ci_configuration_requires_a_connected_repository_and_current_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected_store(root)
            Settings(root).save_config({**DEFAULT, 'repositories': [{'id': 1, 'name': 'example/project', 'enabled': True}]})
            runtime = validate({})
            checks = [{'name': 'release-smoke', 'app_id': 123}]
            configured = configure(root, '1', checks, digest(runtime))
            self.assertEqual(configured['checks'], checks)
            self.assertEqual(load(root)['repository_verification'], {'example/project': checks})
            with self.assertRaises(ValueError): configure(root, '1', [], digest(runtime))
            with self.assertRaises(ValueError): configure(root, '2', checks, configured['runtime_revision'])
            self.assertEqual(configure(root, '1', [], configured['runtime_revision'])['checks'], [])

    def test_all_worker_roles_can_report_exact_steps_but_no_other_outcome_can(self):
        for role in ('build', 'review', 'audit', 'recovery'):
            job = WorkerContractTests().note(role)
            report = {'version': 1, 'attempt': job['id'], 'role': role, 'outcome': 'needs_human_verification',
                      'summary': 'External acceptance check remains.', 'verification': VERIFY}
            if role in ('review', 'audit'): report['candidate'] = 'candidate-key'
            if role == 'audit': report.update(human_required='required', human_reason=VERIFY['what'])
            if role == 'recovery': report.update(pattern='verification', evidence=[], uncertainty='', alternative=None)
            self.assertEqual(result(copy.deepcopy(report), job)['verification'], VERIFY)
            self.assertIn('needs_human_verification', schema(job)['properties']['outcome']['enum'])
            for invalid in (None, {}, {**VERIFY, 'what': ''}, {**VERIFY, 'checks': ['smoke', 'smoke']},
                            {**VERIFY, 'why': 'x' * 4001}, {**VERIFY, 'command': 'arbitrary'}):
                with self.subTest(role=role, invalid=invalid), self.assertRaises(ValueError):
                    result({**report, 'verification': invalid}, job)
            with self.assertRaises(ValueError): result({**report, 'outcome': 'pass'}, job)
            self.assertIn('history.verified_checks', assignment_note(job)['instructions'])

    def test_ci_requires_exact_revision_publisher_name_and_success(self):
        selected = {'head': 'head', 'base': 'base'}
        profile = [{'name': 'release-smoke', 'app_id': 123}]
        row = {'id': 1, 'name': 'release-smoke', 'app_id': 123, 'status': 'completed', 'conclusion': 'success'}
        snapshot = {'head': 'head', 'base': 'base', 'checks': {'head': [row]}}
        self.assertTrue(ci_result(snapshot, selected, profile))
        for changed in ({'app_id': 124}, {'name': 'other'}, {'status': 'in_progress'}, {'conclusion': 'skipped'}, {'conclusion': 'failure'}):
            self.assertFalse(ci_result({**snapshot, 'checks': {'head': [{**row, **changed}]}}, selected, profile))
        self.assertFalse(ci_result({**snapshot, 'checks': {'head': [row, row]}}, selected, profile))
        self.assertFalse(ci_result({**snapshot, 'base': 'old'}, selected, profile))
        self.assertFalse(ci_result(None, selected, profile))
        for profile in ({'example/project': [{'name': 'smoke', 'app_id': True}]}, {'example/project': []}):
            with self.assertRaises(ValueError): validate({'repository_verification': profile})
