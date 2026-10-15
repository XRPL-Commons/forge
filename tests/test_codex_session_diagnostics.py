"""Native Codex metadata is projected through a fixed credential-free vocabulary."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from forge.harnesses.execution import (codex_provider_error, codex_session_diagnostic, finalize,
                                      valid, valid_codex_session, write_receipt)


class CodexSessionDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.thread = '12345678-1234-1234-1234-123456789abc'
        self.events = [{'type': 'thread.started', 'thread_id': self.thread},
                       {'type': 'turn.failed', 'error': {'message': 'PRIVATE FAILURE'}}]
        self.path = self.root / '2026/10/15' / ('rollout-2026-10-15T12-00-00-' + self.thread + '.jsonl')
        self.path.parent.mkdir(parents=True)
        # Shapes from native protocol.rs: SessionMeta, TurnContextItem, and
        # EventMsg::TokenCount(TokenCountEvent), not model-generated output.
        self.rows = [
            {'type': 'session_meta', 'payload': {'id': self.thread, 'session_id': self.thread,
                'cwd': '/PRIVATE', 'base_instructions': 'PRIVATE INSTRUCTIONS'}},
            {'type': 'turn_context', 'payload': {'model': 'gpt-6-astra', 'effort': 'low',
                'cwd': '/PRIVATE', 'summary': 'PRIVATE'}},
            {'type': 'response_item', 'payload': {'text': 'PRIVATE RESPONSE', 'model': 'PRIVATE'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'rate_limits': {'PRIVATE': 'PRIVATE'},
                'info': {'total_token_usage': {'input_tokens': 100, 'cached_input_tokens': 80,
                    'cache_write_input_tokens': 0, 'output_tokens': 20, 'reasoning_output_tokens': 12, 'total_tokens': 120},
                    'last_token_usage': {'input_tokens': 90, 'output_tokens': 10, 'total_tokens': 100},
                    'model_context_window': 258400, 'PRIVATE': 'PRIVATE'}}}]

    def save(self, rows=None):
        self.path.write_text('\n'.join(map(json.dumps, self.rows if rows is None else rows))+'\n')

    def collect(self):
        return codex_session_diagnostic(self.events, self.root)

    def test_exact_thread_retains_only_enum_and_numeric_metadata(self):
        self.save(); result = self.collect()
        self.assertEqual(result, {'source': 'native_session', 'usage_scope': 'last_recorded', 'truncated': False,
            'model': 'gpt-6-astra', 'reasoning_effort': 'low',
            'total_token_usage': self.rows[-1]['payload']['info']['total_token_usage'],
            'last_token_usage': self.rows[-1]['payload']['info']['last_token_usage'], 'model_context_window': 258400})
        self.assertTrue(valid_codex_session(result))
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn(self.thread, json.dumps(result))

    def test_missing_usage_stays_missing_and_unknown_strings_never_escape(self):
        self.rows[1]['payload'].update(model='PRIVATE MODEL TOKEN', effort='PRIVATE')
        self.save(self.rows[:2]); result = self.collect()
        self.assertEqual(result['model'], 'unrecognized')
        self.assertEqual(result['reasoning_effort'], 'unrecognized')
        self.assertNotIn('last_token_usage', result)
        self.assertNotIn('total_token_usage', result)
        self.assertNotIn('model_context_window', result)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_non_numeric_unknown_and_oversized_usage_fields_are_omitted(self):
        self.rows[-1]['payload']['info'].update(model_context_window=True,
            last_token_usage={'input_tokens': True, 'output_tokens': -1, 'total_tokens': 10**13,
                              'reasoning_output_tokens': 'PRIVATE', 'PRIVATE': 4})
        self.save(); result = self.collect()
        self.assertEqual(result['last_token_usage'], {})
        self.assertNotIn('model_context_window', result)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_mismatched_missing_or_ambiguous_thread_is_not_read(self):
        self.save()
        self.events[0]['thread_id'] = '../PRIVATE'
        self.assertIsNone(self.collect())
        self.events[0]['thread_id'] = self.thread
        self.rows[0]['payload']['id'] = 'another-thread'; self.save()
        self.assertIsNone(self.collect())
        self.rows[0]['payload']['id'] = self.thread; self.save()
        duplicate = self.path.with_name('rollout-duplicate-'+self.thread+'.jsonl')
        duplicate.write_bytes(self.path.read_bytes())
        self.assertIsNone(self.collect())

    def test_symlinks_and_excessive_directory_entries_are_not_followed(self):
        self.save(); actual = self.path.with_name('private-auth')
        self.path.rename(actual); self.path.symlink_to(actual)
        self.assertIsNone(self.collect())
        self.path.unlink(); actual.rename(self.path)
        for i in range(129): (self.root / str(i)).touch()
        self.assertIsNone(self.collect())

    def test_bounded_head_and_tail_keep_latest_metadata_without_reading_middle(self):
        self.save(self.rows[:2])
        with self.path.open('a') as stream:
            stream.write(json.dumps({'type': 'response_item', 'payload': {'text': 'PRIVATE'*300000}})+'\n')
            stream.write(json.dumps(self.rows[-1])+'\n')
        result = self.collect()
        self.assertTrue(result['truncated'])
        self.assertEqual(result['model'], 'gpt-6-astra')
        self.assertEqual(result['last_token_usage']['output_tokens'], 10)
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_receipt_and_final_envelope_preserve_only_valid_metadata(self):
        self.save(); provider = codex_provider_error(self.events)
        provider['diagnostic']['session'] = self.collect()
        note = {'id': 'job'}; receipt = self.root/'receipt'; envelope = self.root/'envelope'
        write_receipt(note, 'provider_error', provider_error=provider, path=receipt)
        result = finalize(note, 1, receipt_path=receipt, envelope_path=envelope)
        self.assertTrue(valid(result, 'job', final=True))
        self.assertEqual(result['provider_error'], provider)
        self.assertNotIn('PRIVATE', json.dumps(result))
        for key, value in [('model', 'PRIVATE'), ('reasoning_effort', 'PRIVATE'),
                           ('last_token_usage', {'PRIVATE': 1}), ('model_context_window', True),
                           ('usage_scope', 'failed_response'), ('raw', 'PRIVATE')]:
            invalid = deepcopy(result); invalid['provider_error']['diagnostic']['session'][key] = value
            self.assertFalse(valid(invalid, 'job', final=True), key)

    def test_independent_cli_projection_rejects_untrusted_session_text(self):
        from forge_client.role_qualification import provider_session
        value = {'source': 'native_session', 'usage_scope': 'last_recorded', 'truncated': False,
                 'model': 'PRIVATE', 'reasoning_effort': 'PRIVATE', 'raw': 'PRIVATE',
                 'last_token_usage': {'input_tokens': 'PRIVATE', 'PRIVATE': 1, 'output_tokens': 20}}
        result = provider_session(value)
        self.assertEqual(result['last_token_usage'], {'output_tokens': 20})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_corrupt_native_records_are_diagnostic_absence_not_another_failure(self):
        self.path.write_text('not json\nPRIVATE')
        self.assertIsNone(self.collect())
        self.save(self.rows[:2])
        with self.path.open('ab') as stream: stream.write(b'{bad}\n\xff\n')
        self.assertEqual(self.collect()['model'], 'gpt-6-astra')
