"""Probe guidance comes from terminal provider fields, never transcript prose."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.harnesses.registry import adapter


class ProbeErrorAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def invoke(self, provider, events, *, probe=True, code=1):
        module = adapter(provider)
        collect = Mock(return_value=(code, '\n'.join(json.dumps(event) for event in events)))
        receipt = Mock()
        named = tempfile.NamedTemporaryFile
        def temporary(*args, **kwargs):
            return named(*args, **{**kwargs, 'dir': self.root})
        with patch.dict(module['invoke'].__globals__, {'collect': collect,
                'Path': lambda value: self.root / Path(value).name}), \
                patch.dict(module['EXECUTION'], {'write_receipt': receipt}), \
                patch('tempfile.NamedTemporaryFile', side_effect=temporary), \
                patch('sys.stdout', io.StringIO()):
            answer, failure = module['invoke']({'id': 'probe', 'role': 'probe'}, self.root, {}, {}, 'check',
                                             canary=('canary', 'nonce') if probe else None)
        return module['probe_failure_reason'](answer, failure, None, 'nonce'), receipt

    def test_transcript_and_error_messages_do_not_determine_probe_reason(self):
        misleading = '401 429 authentication_error rate_limit quota not signed in'
        cases = [
            [{'type': 'result', 'is_error': True, 'errors': [misleading]}],
            [{'type': 'assistant', 'message': misleading}],
            [{'type': 'system', 'subtype': 'api_retry', 'error_status': 429},
             {'type': 'result', 'is_error': True}],
            [{'type': 'result', 'is_error': False, 'api_error_status': 401,
              'structured_output': {'ready': True}}],
            [{'type': 'result', 'is_error': True, 'api_error_status': '429'}],
        ]
        for provider in ('claude', 'grok'):
            for events in cases:
                with self.subTest(provider=provider, events=events):
                    reason, receipt = self.invoke(provider, events)
                    self.assertEqual(reason, 'provider_connection_failed')
                    receipt.assert_not_called()

    def test_terminal_typed_error_selects_probe_guidance(self):
        for provider in ('claude', 'grok'):
            for status, expected in ((401, 'provider_login_expired'), (403, 'provider_login_expired'),
                                     (429, 'provider_usage_limit'), (500, 'provider_connection_failed')):
                with self.subTest(provider=provider, status=status):
                    reason, receipt = self.invoke(provider, [dict(type='result', is_error=True,
                                                                 api_error_status=status)])
                    self.assertEqual(reason, expected)
                    receipt.assert_not_called()

    def test_ordinary_job_receipts_keep_existing_classification(self):
        for provider in ('claude', 'grok'):
            for status, expected in ((401, 'auth'), (429, 'rate_limit')):
                with self.subTest(provider=provider, status=status):
                    _, receipt = self.invoke(provider, [dict(type='result', is_error=True,
                        subtype='error_during_execution', api_error_status=status)], probe=False)
                    detail = receipt.call_args.kwargs['provider_error']
                    self.assertEqual(detail, {'code': expected, 'status': status}
                                     if provider == 'claude' else {'code': 'unknown'})
