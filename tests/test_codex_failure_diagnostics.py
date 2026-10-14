"""Provider diagnostics retain protocol facts without interpreting error prose."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.execution_problems import classify
from forge.harnesses.execution import codex_provider_error, finalize, valid, write_receipt
from forge.harnesses.registry import adapter
from forge.role_qualification import capture_evidence
from forge_client.role_qualification import RoleQualificationFailed


class CodexFailureDiagnosticsTests(unittest.TestCase):
    def failed(self, error, before=()):
        return codex_provider_error([*before, {'type': 'turn.failed', 'error': error}])

    def test_message_only_error_retains_progress_but_never_guesses_cause(self):
        event = {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': 'PRIVATE MODEL TEXT'}}
        errors = ['invalid_json_schema', 'rate limit 429', 'refresh_token_reused', 'PRIVATE TOKEN AND URL']
        for message in errors:
            with self.subTest(message=message):
                provider = self.failed({'message': message}, [{'type': 'turn.started'}, event])
                self.assertEqual(provider, {'code': 'unknown', 'diagnostic': {
                    'kind': 'unclassified_terminal_error', 'event_scope': 'retained_tail',
                    'observed': ['agent_message', 'turn.started'],
                    'error_fields': {'message': 'string'}, 'extra_error_fields': False}})
                self.assertNotIn(message, json.dumps(provider))

    def test_only_final_terminal_structured_fields_are_observed(self):
        provider = self.failed({'message': 'PRIVATE', 'code': 'invalid_json_schema', 'status': 400,
                                'type': 'invalid_request_error', 'param': 'PRIVATE PARAM', 'PRIVATE KEY': 'PRIVATE'},
                               [{'type': 'error', 'message': 'PRIVATE'},
                                {'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'PRIVATE'}}])
        diagnostic = provider['diagnostic']
        self.assertEqual(diagnostic['structured_code'], 'invalid_json_schema')
        self.assertEqual(diagnostic['http_status'], 400)
        self.assertEqual(diagnostic['observed'], ['command_execution', 'error'])
        self.assertEqual(diagnostic['error_fields'], {'message': 'string', 'code': 'string', 'status': 'number',
                                                     'type': 'string', 'param': 'string'})
        self.assertTrue(diagnostic['extra_error_fields'])
        self.assertNotIn('PRIVATE', json.dumps(provider))
        self.assertIsNone(codex_provider_error([{'type': 'turn.failed', 'error': {'code': 'invalid_json_schema'}},
                                                {'type': 'turn.completed'}]))
        final = self.failed({'message': 'unknown'}, [{'type': 'turn.failed', 'error': {'code': 'invalid_json_schema'}}])
        self.assertNotIn('structured_code', final['diagnostic'])

    def test_tool_model_text_and_unknown_typed_values_cannot_supply_diagnostics(self):
        provider = self.failed({'message': {'code': 'invalid_json_schema'}, 'code': 'PRIVATE SECRET', 'status': True},
            [{'type': 'item.completed', 'item': {'type': 'command_execution',
               'aggregated_output': '{"type":"turn.failed","error":{"code":"invalid_json_schema"}}'}},
             {'type': 'PRIVATE EVENT', 'item': {'type': 'PRIVATE ITEM'}}, None])
        self.assertEqual(provider['diagnostic']['error_fields'], {'message': 'object', 'code': 'string', 'status': 'boolean'})
        self.assertNotIn('structured_code', provider['diagnostic'])
        self.assertNotIn('http_status', provider['diagnostic'])
        self.assertNotIn('PRIVATE', json.dumps(provider))
        self.assertIsNone(codex_provider_error([{'type': 'item.completed', 'item': {'type': 'agent_message',
              'text': '{"type":"turn.failed","error":{"code":"invalid_json_schema"}}'}}]))

    def test_private_receipt_survives_capture_and_cli_with_retry_policy_unchanged(self):
        job = {'id': '12345678-1234-1234-1234-123456789abc', 'role': 'triage', 'container_id': 'container'}
        provider = self.failed({'message': 'PRIVATE', 'code': 'invalid_json_schema', 'status': 400})
        with tempfile.TemporaryDirectory() as temp:
            receipt, envelope_path = Path(temp)/'receipt', Path(temp)/'envelope'
            write_receipt(job, 'provider_error', provider_error=provider, path=receipt)
            envelope = finalize(job, 1, receipt_path=receipt, envelope_path=envelope_path)
        captured = dict(result=None, logs='PRIVATE LOGS', execution_envelope=envelope,
                        execution=dict(id='container', image_id='sha256:'+'a'*64, exit_protocol='1',
                                       running=False, exit_code=1, oom_killed=False))
        decision = classify(job, captured, 100)
        self.assertEqual((decision['kind'], decision['details']['reason'], decision['details']['retry_at']),
                         ('provider_error', 'provider_unknown', 130))
        report = RoleQualificationFailed(dict(status='failed', failure=capture_evidence(captured, job))).report
        self.assertEqual(report['failure']['execution_envelope']['provider_error'], provider)
        self.assertNotIn('PRIVATE', json.dumps(report))
        for field, value in [('observed', ['PRIVATE']), ('error_fields', {'message': 'PRIVATE'}),
                             ('extra_error_fields', 'PRIVATE'), ('http_status', True),
                             ('structured_code', 'PRIVATE'), ('message', 'PRIVATE')]:
            invalid = deepcopy(envelope)
            invalid['provider_error']['diagnostic'][field] = value
            self.assertFalse(valid(invalid, job['id'], final=True), field)
            hostile = dict(status='failed', failure={'execution_envelope': invalid})
            self.assertNotIn('PRIVATE', json.dumps(RoleQualificationFailed(hostile).report))

    def test_codex_adapter_writes_diagnostic_receipt_from_terminal_protocol(self):
        module = adapter('codex')
        namespace = module['run'].__globals__
        events = [{'type': 'turn.failed', 'error': {'message': 'PRIVATE', 'code': 'invalid_json_schema'}}]
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.dict(namespace, {'Path': lambda path: root/Path(path).name,
                                        'prepare_tool_environment': lambda *args: {}}), \
                 patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
                 patch('runpy.run_path', return_value={}), \
                 patch.dict(module['EXECUTION'], {'stream': lambda *args: (1, events)}), \
                 patch.dict(module['RESULTS'], {'publish_result': lambda *args: None}), \
                 patch.dict(module['EXECUTION'], {'write_receipt': lambda note, status, **fields:
                      write_receipt(note, status, path=root/'receipt', **fields)}):
                code = module['run']({'id': 'job', 'role': 'triage'}, root, {}, {})
            receipt = json.loads((root/'receipt').read_text())
        self.assertEqual(code, 1)
        self.assertTrue(valid(receipt, 'job'))
        self.assertEqual(receipt['provider_error']['diagnostic']['structured_code'], 'invalid_json_schema')
        self.assertNotIn('PRIVATE', json.dumps(receipt))
