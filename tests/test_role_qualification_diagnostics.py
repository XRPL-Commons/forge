"""Execution failures remain distinct from malformed model results."""
from copy import deepcopy
import json
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.harnesses.execution import fault
from forge.role_qualification import capture_evidence, validate_capture, QualificationError


class CaptureDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.job = {'id': 'qualification-job', 'role': 'build'}
        self.notes = {'id': self.job['id'], 'role': 'build'}

    def capture(self, status='ok', exit_code=0, **details):
        return dict(result=None, missing=['valid result artifact'], logs='PRIVATE LOG CONTENT',
            execution=dict(running=False, exit_code=exit_code, oom_killed=False),
            execution_envelope=dict(version=1, attempt=self.job['id'], harness_status=status,
                                    exit_code=exit_code, **details))

    def test_failed_execution_precedes_schema_check_and_keeps_known_cause(self):
        for status, details in [
                ('provider_error', {'provider_error': {'code': 'auth', 'status': 401}}),
                ('harness_error', {'harness_error': fault('MissingHarnessReceipt', phase='runner')}),
                ('timeout', {})]:
            with self.subTest(status=status):
                capture = self.capture(status, 1, **details)
                with self.assertRaisesRegex(QualificationError, '^provider_execution_not_confirmed$'):
                    validate_capture(capture, self.job, self.notes, None)
                evidence = capture_evidence(capture, self.job)
                self.assertEqual(evidence['execution_envelope']['harness_status'], status)
                self.assertEqual(evidence['missing'], ['result'])
                self.assertFalse(evidence['result_present'])
                if status == 'provider_error':
                    self.assertEqual(evidence['execution_envelope']['provider_error'], {'code': 'auth', 'status': 401})
                if status == 'harness_error':
                    self.assertEqual(evidence['execution_envelope']['harness_error'], {'phase': 'runner'})
                encoded = json.dumps(evidence)
                for forbidden in ('PRIVATE LOG CONTENT', '/opt/forge/', 'MissingHarnessReceipt'):
                    self.assertNotIn(forbidden, encoded)

    def test_confirmed_execution_keeps_missing_and_invalid_results_distinct(self):
        capture = self.capture()
        with self.assertRaisesRegex(QualificationError, '^result_missing$'):
            validate_capture(capture, self.job, self.notes, None)
        capture['result'] = {'summary': 'PRIVATE MODEL CONTENT', 'role': 'build'}
        with self.assertRaisesRegex(QualificationError, '^result_schema_invalid$'):
            validate_capture(capture, self.job, self.notes, None)
        self.assertNotIn('PRIVATE MODEL CONTENT', json.dumps(capture_evidence(capture, self.job)))

    def test_unmatched_or_invalid_envelope_never_supplies_a_cause(self):
        original = self.capture('provider_error', 1, provider_error={'code': 'auth'})
        for changed in ('attempt', 'exit_code', 'provider_error'):
            with self.subTest(changed=changed):
                capture = deepcopy(original)
                capture['execution_envelope'][changed] = {
                    'attempt': 'another-job', 'exit_code': 2,
                    'provider_error': {'code': 'auth', 'message': 'PRIVATE ERROR CONTENT'}}[changed]
                evidence = capture_evidence(capture, self.job)
                self.assertNotIn('execution_envelope', evidence)
                with self.assertRaisesRegex(QualificationError, '^provider_execution_not_confirmed$'):
                    validate_capture(capture, self.job, self.notes, None)

    def test_startup_failure_retains_only_validated_enum_cause(self):
        from tests.test_forge_startup_problem import receipt
        capture = self.capture(exit_code=78)
        capture['execution']['startup_protocol'] = '1'
        capture['forge_startup'] = receipt(self.job['id'])
        evidence = capture_evidence(capture, self.job)
        self.assertEqual(evidence['startup'],
                         {'reason': 'harness_startup', 'phase': 'before_agent', 'exception': 'ModuleNotFoundError'})
        self.assertNotIn('/opt/forge/', json.dumps(evidence))
        for changed in ('attempt', 'exit', 'protocol'):
            with self.subTest(changed=changed):
                invalid = deepcopy(capture)
                if changed == 'attempt': invalid['forge_startup']['attempt'] = 'another-job'
                if changed == 'exit': invalid['execution']['exit_code'] = 1
                if changed == 'protocol': invalid['execution']['startup_protocol'] = '0'
                self.assertNotIn('startup', capture_evidence(invalid, self.job))

    def test_failed_role_report_survives_confirmed_cleanup(self):
        from forge.factory.config import DEFAULT
        from forge.factory.contracts import assignment_note
        from forge.role_qualification import qualify
        from forge.store.configuration import Configuration
        from forge.store.db import Store
        from forge.worker_qualification import Fixture
        from tests.test_source_preparation import Access
        with tempfile.TemporaryDirectory(prefix='qd-', dir='/tmp') as temp:
            root = Path(temp)
            store = Store.create(root/'live')
            config = {**DEFAULT, 'image': 'sha256:'+'a'*64, 'provider_environment': []}
            Configuration(store).replace('setup/runtime.json', config, expected_revision=0)
            git = Fixture(root)
            runner = Mock()
            runner.inspect.return_value = {'running': False, 'status': 'exited'}

            def create_runner(*args, source, **kwargs):
                def prepare(job):
                    prepared = source.prepare(job, assignment_note(job))
                    job['source_input'] = prepared.source_input
                    runner.capture.return_value = dict(result=None, missing=['valid result artifact'],
                        logs='PRIVATE LOG CONTENT', execution=dict(running=False, exit_code=1, oom_killed=False),
                        execution_envelope=dict(version=1, attempt=job['id'], harness_status='provider_error',
                                                exit_code=1, provider_error={'code': 'unknown'}))
                runner.prepare_source.side_effect = prepare
                return runner

            with ExitStack() as stack:
                for target, value in (
                        ('forge.store.runtime.selected', store),
                        ('forge.role_qualification.configuration', config),
                        ('forge.role_qualification.idle_logins', None),
                        ('forge.mirror_access.MirrorAccess', Access()),
                        ('forge.mirror.git_store.GitStore', git.cache),
                        ('forge.factory.github.GitHub.issue_inputs', (dict(number=10,title='Issue',body='Body'),'requirements',[])),
                        ('forge.credentials.Credentials.environment', {})):
                    stack.enter_context(patch(target, return_value=value))
                settings = stack.enter_context(patch('forge.settings.Settings'))
                settings.return_value.load.return_value = {'connections': {}, 'repositories': []}
                daemon = stack.enter_context(patch('forge.mirror_daemon.MirrorDaemon'))
                stack.enter_context(patch('forge.role_qualification.ContainerRunner', side_effect=create_runner))
                report = qualify(root/'live', 'acme/project', 10, apply=True)
            self.assertEqual(report['reason'], 'provider_execution_not_confirmed')
            self.assertEqual(report['current_role'], 'build')
            self.assertEqual(report['failure']['execution_envelope'],
                             {'harness_status': 'provider_error', 'provider_error': {'code': 'unknown'}})
            self.assertEqual(report['cleanup'], 'confirmed')
            self.assertEqual(report['automation_after'], 'disabled')
            self.assertEqual(report['results'], [])
            self.assertNotIn('PRIVATE LOG CONTENT', json.dumps(report))
            runner.reset.assert_called_once()
            daemon.return_value.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
