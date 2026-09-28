import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge import host_setup
from forge.onboarding import Onboarding


class TerminalCheckTests(unittest.TestCase):
    def test_service_owned_check_reports_drain_heartbeat_and_partial_account_failure(self):
        waiting = {'phase': 'active', 'operation': {'id': 'existing', 'action': 'harness_check', 'step': 'prepare'}}
        probing = {**waiting, 'operation': {**waiting['operation'], 'step': 'probe'},
                   'probe_jobs': [{'kind': 'provider', 'fenced': False, 'runner_config': {'provider': 'codex'}}]}
        finished = {'operation': None, 'provider_checks': {'codex': 'passed', 'claude': 'failed'},
                    'provider_errors': {'claude': 'Account unavailable'}}
        clock = [0]
        def sleep(_): clock[0] += 31
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(host_setup, 'Onboarding') as setup, \
                patch.object(host_setup, 'Service') as service, \
                patch.object(host_setup.fcntl, 'flock', side_effect=BlockingIOError), \
                patch.object(host_setup.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(host_setup.time, 'sleep', side_effect=sleep), \
                patch('sys.stdout', output):
            setup.return_value.runtime.return_value = {}
            setup.return_value.queue.return_value = 'existing'
            setup.return_value.read.side_effect = [waiting, waiting, probing, finished]
            setup.return_value.activity.side_effect = Onboarding.activity
            host_setup.check(Path(directory))
            service.return_value.tick.assert_not_called()
            setup.return_value.queue.assert_called_once()
        text = output.getvalue()
        self.assertIn('Ctrl+C stops watching; verification continues', text)
        self.assertIn('[0m 00s] Waiting for active workers', text)
        self.assertIn('[0m 31s] Waiting for active workers', text)
        self.assertIn('Checking Codex', text)
        self.assertIn('claude: failed. Account unavailable', text)
        self.assertIn('codex: passed', text)

    def test_scoped_check_reports_only_the_named_account_and_leaves_others_alone(self):
        operation = {'id': 'scoped', 'action': 'harness_check', 'step': 'prepare',
                     'harness': 'claude', 'providers': ['claude-1', 'claude-2']}
        waiting = {'phase': 'active', 'operation': operation}
        finished = {'operation': None, 'probe_jobs': [{'kind': 'provider'}],
                    'provider_checks': {'codex': 'failed', 'claude-1': 'passed', 'claude-2': 'passed'},
                    'provider_errors': {'codex': 'Checked last week'}}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(host_setup, 'Onboarding') as setup, \
                patch.object(host_setup, 'Service'), \
                patch.object(host_setup.fcntl, 'flock', side_effect=BlockingIOError), \
                patch.object(host_setup.time, 'monotonic', side_effect=lambda: 0), \
                patch.object(host_setup.time, 'sleep', side_effect=lambda _: None), \
                patch('sys.stdout', output):
            setup.return_value.runtime.return_value = {}
            setup.return_value.queue.return_value = 'scoped'
            setup.return_value.read.side_effect = [waiting, waiting, finished]
            setup.return_value.activity.side_effect = Onboarding.activity
            host_setup.check(Path(directory), 'claude')
            self.assertEqual(setup.return_value.queue.call_args.kwargs['harness'], 'claude')
        text = output.getvalue()
        self.assertIn('Waiting for workers using the Claude Code login to finish; other work continues', text)
        self.assertIn('claude-1: passed', text)
        self.assertNotIn('codex', text)
        self.assertIn('Claude Code verified. Other accounts unchanged.', text)
        self.assertNotIn('Worker isolation, termination and reuse verified', text)

    def test_scoped_check_reports_runner_probes_when_they_ran(self):
        operation = {'id': 'scoped', 'action': 'harness_check', 'step': 'prepare',
                     'harness': 'claude', 'providers': ['claude']}
        finished = {'operation': None, 'provider_checks': {'claude': 'passed'},
                    'probe_jobs': [{'kind': 'provider'}, {'kind': 'termination'}, {'kind': 'reuse'}]}
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(host_setup, 'Onboarding') as setup, \
                patch.object(host_setup, 'Service'), \
                patch.object(host_setup.fcntl, 'flock', side_effect=BlockingIOError), \
                patch.object(host_setup.time, 'monotonic', side_effect=lambda: 0), \
                patch.object(host_setup.time, 'sleep', side_effect=lambda _: None), \
                patch('sys.stdout', output):
            setup.return_value.runtime.return_value = {}
            setup.return_value.queue.return_value = 'scoped'
            setup.return_value.read.side_effect = [{'phase': 'active', 'operation': operation}, finished]
            setup.return_value.activity.side_effect = Onboarding.activity
            host_setup.check(Path(directory), 'claude')
        self.assertIn('Worker isolation, termination and reuse verified', output.getvalue())

    def test_status_exposes_pending_verification(self):
        data = {'phase': 'active', 'operation': {'action': 'harness_check', 'step': 'prepare'}}
        output = io.StringIO()
        with patch.object(host_setup, 'Onboarding') as setup, patch('sys.stdout', output):
            setup.return_value.view.return_value = {'harnesses': []}
            setup.return_value.read.return_value = data
            setup.return_value.activity.side_effect = Onboarding.activity
            host_setup.status(Path('/unused'))
        self.assertIn('Waiting for active workers', output.getvalue())

    def test_terminal_timeout_keeps_service_operation_and_explains_reattachment(self):
        clock = [0]
        def sleep(_): clock[0] = 901
        data = {'phase': 'active', 'operation': {'id': 'existing', 'action': 'harness_check', 'step': 'prepare'}}
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(host_setup, 'Onboarding') as setup, \
                patch.object(host_setup, 'Service') as service, \
                patch.object(host_setup.fcntl, 'flock', side_effect=BlockingIOError), \
                patch.object(host_setup.time, 'monotonic', side_effect=lambda: clock[0]), \
                patch.object(host_setup.time, 'sleep', side_effect=sleep), \
                patch('sys.stdout', io.StringIO()):
            setup.return_value.runtime.return_value = {}
            setup.return_value.queue.return_value = 'existing'
            setup.return_value.read.return_value = data
            setup.return_value.activity.side_effect = Onboarding.activity
            with self.assertRaisesRegex(ValueError, 'Forge service continues it. Rerun setup check to reattach'):
                host_setup.check(Path(directory))
            service.return_value.tick.assert_not_called()
            self.assertEqual(data['operation']['id'], 'existing')
