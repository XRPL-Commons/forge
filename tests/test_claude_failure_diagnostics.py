"""Claude terminal diagnostics remain bounded, scrubbed, and non-authoritative."""
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.factory.config import validate
from forge.factory.runner import ContainerRunner, RunnerError
from forge.harnesses.registry import adapter
from forge.store.auxiliary import save_in, job_in
from forge.store.db import Store


class ClaudeFailureDiagnosticTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.module = adapter('claude')

    def invoke(self, code, events, *, stderr='', real_process=False):
        raw = '\n'.join(json.dumps(event) for event in events) + '\n' + stderr
        emitted = io.StringIO()
        collect = Mock(return_value=(code, raw))
        if real_process:
            original = self.module['collect']
            program = 'import sys; sys.stdout.write(' + repr(raw) + '); raise SystemExit(' + str(code) + ')'
            collect = lambda *args, **kwargs: original([sys.executable, '-c', program], 'private prompt', self.root, dict(os.environ))
        original_tempfile = tempfile.TemporaryFile
        with patch.dict(self.module['invoke'].__globals__, {'collect': collect, 'Path': lambda path: self.root / Path(path).name}), \
             patch('tempfile.TemporaryFile', side_effect=lambda **kw: original_tempfile(mode=kw['mode'], dir=self.root)), \
             patch('sys.stdout', emitted):
            result = self.module['invoke']({'role': 'triage'}, self.root, {}, {}, 'private prompt')
        lines = [json.loads(line) for line in emitted.getvalue().splitlines()]
        return result, lines, emitted.getvalue()

    def capture(self, logs, secrets=()):
        runner = ContainerRunner(validate({}))
        runner.inspect = Mock(return_value={'id': 'container', 'status': 'exited', 'running': False, 'exit_code': 1})
        runner._scrub_values = Mock(return_value=list(secrets))
        runner.call = Mock(side_effect=[RunnerError('No result file'), Mock(stdout=logs.encode())])
        return runner.capture({'id': 'diagnostic', 'role': 'triage'})

    def test_terminal_error_retains_metadata_and_complete_reason_without_model_text(self):
        event = dict(type='result', subtype='error_during_execution', is_error=True,
                     errors=['API Error: 400: The requested output schema exceeds the complexity limit.'],
                     result='private model result', session_id='private-session', usage={'private': 'usage'})
        result, lines, raw = self.invoke(1, [
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'private transcript'}]}}, event])
        self.assertIsNone(result[0]); self.assertEqual(len(lines), 1)
        diagnostic = lines[0]['provider_failure']
        self.assertEqual(diagnostic['exit_code'], 1)
        self.assertEqual(diagnostic['subtype'], 'error_during_execution')
        self.assertEqual(diagnostic['reason'], 'terminal_error')
        self.assertEqual(diagnostic['errors'], event['errors'])
        self.assertTrue(diagnostic['terminal_result']); self.assertTrue(diagnostic['is_error'])
        for private in ('private model result', 'private transcript', 'private-session', 'usage'):
            self.assertNotIn(private, raw)

    def test_missing_structured_output_is_distinct_from_missing_terminal_result(self):
        _, lines, _ = self.invoke(0, [dict(type='result', subtype='success', is_error=False, result='plain model text')])
        self.assertEqual(lines[0]['provider_failure']['reason'], 'structured_output_missing')
        self.assertEqual(lines[0]['provider_failure']['subtype'], 'success')
        _, lines, raw = self.invoke(1, [{'type': 'assistant', 'message': {'content': 'private incomplete stream'}}])
        self.assertEqual(lines[0]['provider_failure']['reason'], 'terminal_result_missing')
        self.assertFalse(lines[0]['provider_failure']['terminal_result'])
        self.assertNotIn('private incomplete stream', raw)

    def test_retry_exhaustion_keeps_errors_that_distinguish_validation_from_retraction(self):
        for reason in ('Structured output failed schema validation: required property rows missing',
                       'Structured output was retracted after model fallback without a replacement'):
            with self.subTest(reason=reason):
                _, lines, _ = self.invoke(1, [dict(type='result', subtype='error_max_structured_output_retries',
                                                  is_error=True, errors=[reason])])
                self.assertEqual(lines[0]['provider_failure']['reason'], 'structured_output_retries_exhausted')
                self.assertEqual(lines[0]['provider_failure']['errors'], [reason])

    def test_documented_schema_startup_error_is_selected_without_stderr_transcript(self):
        reason = 'Error: --json-schema is not a valid JSON Schema: data/properties/rows/type must be a valid type'
        _, lines, raw = self.invoke(1, [], stderr='private startup environment\n' + reason + '\nprivate remainder')
        self.assertEqual(lines[0]['provider_failure']['reason'], 'startup_schema_invalid')
        self.assertEqual(lines[0]['provider_failure']['errors'], [reason])
        self.assertNotIn('private startup environment', raw); self.assertNotIn('private remainder', raw)
        _, lines, raw = self.invoke(1, [], stderr=self.module['SCHEMA_ERROR'] + ': opaque-secret-' + 'x' * 2048)
        self.assertEqual(lines[0]['provider_failure']['reason'], 'startup_schema_invalid')
        self.assertEqual(lines[0]['provider_failure']['errors'], [])
        self.assertEqual(lines[0]['provider_failure']['omitted_errors'], 1)
        self.assertNotIn('opaque-secret', raw)

    def test_oversize_errors_are_omitted_whole_without_credential_fragments(self):
        secret = 'opaque-complete-secret-' + 's' * 1100
        errors = ['schema problem includes ' + secret, 'é' * 1024, *['bounded error '+str(i) for i in range(8)]]
        _, lines, raw = self.invoke(1, [dict(type='result', subtype='error_during_execution', is_error=True, errors=errors)])
        diagnostic = lines[0]['provider_failure']
        self.assertEqual(diagnostic['errors'], ['bounded error 0', 'bounded error 1', 'bounded error 2'])
        self.assertEqual(diagnostic['error_count'], 10); self.assertEqual(diagnostic['omitted_errors'], 7)
        self.assertLess(len(raw.encode()), 4096)
        self.assertNotIn('opaque-complete-secret', raw); self.assertNotIn('ssssssss', raw)

    def test_unknown_subtype_and_non_string_errors_do_not_become_log_payload(self):
        _, lines, raw = self.invoke(1, [dict(type='result', subtype='private-subtype-value', is_error=True,
                                            errors=[{'request': 'private prompt'}, 4, 'bounded useful reason'])])
        diagnostic = lines[0]['provider_failure']
        self.assertIsNone(diagnostic['subtype']); self.assertFalse(diagnostic['subtype_recognized'])
        self.assertEqual(diagnostic['errors'], ['bounded useful reason'])
        self.assertEqual(diagnostic['omitted_errors'], 2)
        self.assertNotIn('private-subtype-value', raw); self.assertNotIn('private prompt', raw)

    def test_existing_auth_quota_classifications_and_success_authority_stay_unchanged(self):
        for reason, expected in [('authentication_error', 'Provider authentication_error; reconnect the account'),
                                 ('rate_limit', 'Provider usage limit reached')]:
            result, lines, _ = self.invoke(1, [dict(type='result', subtype='error_during_execution', is_error=True, errors=[reason])])
            self.assertEqual(result, (None, expected)); self.assertEqual(lines[0]['message'], expected)
        result, lines, _ = self.invoke(0, [dict(type='result', subtype='success', is_error=False, structured_output={'ready': True})])
        self.assertEqual(result, ({'ready': True}, None)); self.assertEqual(lines, [])
        result, lines, _ = self.invoke(2, [dict(type='result', subtype='success', is_error=False, structured_output={'ready': True})])
        self.assertIsNone(result[0]); self.assertEqual(lines[0]['provider_failure']['reason'], 'cli_exit_nonzero')

    def test_real_stream_to_scrubbed_capture_and_retained_diagnostic_never_creates_a_result(self):
        secret = 'opaque-sensitive-credential-value'
        event = dict(type='result', subtype='error_during_execution', is_error=True,
                     errors=['Provider rejected credential '+secret+' for requested operation'])
        result, _, output = self.invoke(1, [event], real_process=True)
        self.assertIsNone(result[0])
        capture = self.capture(output, [secret])
        self.assertIsNone(capture['result']); self.assertIn('valid result artifact', capture['missing'])
        self.assertNotIn(secret, capture['logs'])
        store = Store.create(self.root / 'store')
        job = dict(id='diagnostic', slot='repository-triage', role='triage', deadline=1000, cleanup=False, fenced=False)
        with store.write() as db:
            save_in(db, 'triage', 'proposal', job, 100)
            save_in(db, 'triage', 'proposal', {**job, 'capture': capture, 'cleanup': True, 'fenced': True}, 101)
        with store.read_snapshot() as db:
            retained = db.execute('SELECT content FROM auxiliary_logs WHERE execution_id=?', ('diagnostic',)).fetchone()[0].decode()
            diagnostic = json.loads(retained)['provider_failure']
            self.assertEqual(diagnostic['errors'], ['Provider rejected credential [REDACTED] for requested operation'])
            self.assertEqual(diagnostic['exit_code'], 1)
            self.assertIsNone(job_in(db, 'diagnostic')['capture']['result'])

    def test_shared_scrubber_handles_json_escaped_credentials_before_tail_limit(self):
        for secret in ('opaque-"quoted"-secret', 'opaque-\\path-secret', 'opaque-é-secret'):
            _, _, output = self.invoke(1, [dict(type='result', subtype='error_during_execution', is_error=True,
                                                errors=['Failed with '+secret])])
            capture = self.capture(output, [secret])
            self.assertEqual(json.loads(capture['logs'])['provider_failure']['errors'], ['Failed with [REDACTED]'])
        secret = 'opaque-long-prefix-sensitive-suffix'
        output = 'prefix' + secret + 'z' * 65520
        capture = self.capture(output, [secret])
        self.assertNotIn('sensitive-suffix', capture['logs'])
        runner = ContainerRunner(validate({})); runner._scrub_values = Mock(return_value=[secret])
        runner.call = Mock(return_value=Mock(stdout=output.encode()))
        observed = runner.observe({'id': 'job'}, current={'id': 'container'})
        self.assertNotIn('sensitive-suffix', observed['logs'])
        self.assertLessEqual(len(observed['logs']), 65536)
