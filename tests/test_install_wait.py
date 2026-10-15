"""A running wizard follows browser approvals without repeating prompts or tabs."""
from contextlib import nullcontext, redirect_stderr
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge_client.installation import Install, execute
from forge_client.cli import parser
from forge_client.contracts import SetupError


class InstallWaitTests(unittest.TestCase):
    def test_retry_flag_requires_resume(self):
        args = parser().parse_args(['install', '--ssh', 'fixture', '--apply', '--retry-smoke', 'failed-proposal'])
        with self.assertRaisesRegex(SetupError, 'with --resume'):
            execute(args)
        args = parser().parse_args(['install', '--ssh', 'fixture', '--resume', '--retry-smoke', 'failed-proposal'])
        self.assertEqual(args.retry_smoke, 'failed-proposal')

    def run_install(self, receipts, *, wait=60, json_mode=False, events=()):
        args = SimpleNamespace(wait=wait, intent_file=None, status=False, resume=True,
                               preflight=False, apply=False, json=json_mode, ssh='fixture', waive=[])
        workflow = Mock(key='fixture', journal=SimpleNamespace(root='/unused'))
        results = iter(receipts)
        def construct(*args, **kwargs):
            def apply(**options):
                for event in events: kwargs['notify'](event)
                return next(results)
            workflow.apply.side_effect = apply
            return workflow
        output = StringIO()
        with patch('forge_client.installation.GitHub'), \
             patch('forge_client.installation.Install', side_effect=construct), \
             patch('forge_client.installation.connection_lock', return_value=nullcontext()), \
             patch('forge_client.installation.time.monotonic', return_value=0), \
             patch('forge_client.installation.time.sleep') as sleep, \
             patch('sys.stdin.isatty', return_value=True), redirect_stderr(output):
            result = execute(args)
        return result, workflow.apply.call_count, sleep.call_count, output.getvalue()

    def pending(self, *rows):
        return {'status': 'installation_pending', 'pending': list(rows)}

    def complete(self):
        return {'status': 'installation_complete', 'handover_complete': True, 'pending': []}

    def test_browser_approval_and_device_code_continue_to_completion(self):
        login = {'status': 'account_login_required', 'url': 'https://provider.example/device', 'code': 'ABCD-12345'}
        complete = self.complete()
        result, calls, sleeps, _ = self.run_install([
            self.pending({'status': 'app_installation_required'}),
            self.pending({'status': 'account_login_pending'}), self.pending(login), self.pending(login), complete])
        self.assertEqual(result, complete)
        self.assertEqual((calls, sleeps), (5, 4))

    def test_wait_zero_runs_one_apply_without_sleep(self):
        pending = self.pending({'status': 'app_installation_required'})
        result, calls, sleeps, _ = self.run_install([pending], wait=0)
        self.assertEqual(result, pending)
        self.assertEqual((calls, sleeps), (1, 0))

    def test_manual_or_failed_steps_return_without_polling(self):
        for row in ({'status': 'account_login_required'},
                    {'status': 'account_login_required', 'url': 'https://provider.example'},
                    {'status': 'account_login_failed'}, {'status': 'write_unconfirmed'},
                    {'status': 'smoke_triage_failed', 'error': 'steward_cycle_limit',
                     'next_action': {'instruction': 'Request new repository triage, then resume forge install.'}}):
            with self.subTest(row=row):
                pending = self.pending(row)
                result, calls, sleeps, _ = self.run_install([pending])
                self.assertEqual(result, pending)
                self.assertEqual((calls, sleeps), (1, 0))

    def test_human_prompt_shows_instruction_url_and_code_once(self):
        event = {'instruction': 'Complete provider sign-in.', 'url': 'https://provider.example/device', 'code': 'ABCD-12345'}
        _, _, _, output = self.run_install([
            self.pending({'status': 'account_login_pending'}), self.complete()], events=[event, event])
        for text in (event['instruction'], event['url'], 'One-time code: ' + event['code']):
            self.assertEqual(output.count(text), 1)

    def test_json_events_keep_structured_fields_and_emit_new_code_once(self):
        first = {'event': 'provider_login', 'url': 'https://provider.example/device', 'code': 'ABCD-12345'}
        second = {**first, 'code': 'EFGH-67890'}
        _, _, _, output = self.run_install([self.complete()], json_mode=True, events=[first, first, second])
        events = [json.loads(line) for line in output.splitlines()]
        self.assertEqual([row for row in events if row.get('event') == 'provider_login'], [first, second])

    def test_browser_urls_open_once_per_install_invocation(self):
        workflow = Install('fixture', github=Mock(), installation=Mock(), journal=Mock(read=Mock(return_value=None)))
        with patch('webbrowser.open') as open_browser:
            workflow.open_browser('https://github.example/install/1')
            workflow.open_browser('https://github.example/install/1')
            workflow.open_browser('https://github.example/install/2')
        self.assertEqual([call.args[0] for call in open_browser.call_args_list],
                         ['https://github.example/install/1', 'https://github.example/install/2'])
