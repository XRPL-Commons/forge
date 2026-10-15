"""Codex prompt transport preserves the complete result handoff contract."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.harnesses.registry import adapter
from forge.harnesses.schema import schema_check
from forge.role_qualification import QualificationError, schema_check as qualification_check


def object_schema(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


class CodexResultContractTests(unittest.TestCase):
    def test_image_modules_load_without_the_host_package(self):
        path = Path(__file__).resolve().parents[1]/'forge/harnesses/codex/adapter.py'
        subprocess.run([sys.executable, '-I', '-c',
            "import runpy, sys; m=runpy.run_path(sys.argv[1]); "
            "m['RESULTS']['SCHEMA']['schema_check']('ok', {'type':'string','minLength':1})", str(path)],
            check=True, capture_output=True, text=True)

    def test_prompt_carries_original_schema_without_native_schema_option(self):
        schema = object_schema({
            'minLength': {'type': 'string', 'minLength': 2, 'maxLength': 20,
                          'enum': ['minLength'], 'pattern': '^minLength$'},
            'choices': {'type': 'array', 'minItems': 1, 'maxItems': 2, 'items': {
                'anyOf': [{'type': 'null'}, object_schema({'reason': {'type': 'string', 'minLength': 1}})]}},
        })
        original = deepcopy(schema)
        for model in (None, 'gpt-6.1-sol'):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                module = adapter('codex')
                namespace = module['run'].__globals__
                stream = Mock(return_value=(0, []))
                note = {'id': 'job', 'role': 'triage', 'model': model}
                env = {'TEST_ENV': 'unchanged'}
                with patch.dict(namespace, {'Path': lambda path: root/Path(path).name,
                                            'prepare_tool_environment': lambda *args: {}}), \
                     patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
                     patch('runpy.run_path', return_value={}), \
                     patch.dict(module['EXECUTION'], {'stream': stream}):
                    self.assertEqual(module['run'](note, root, env, schema), 0)
                args, prompt, directory, passed_env = stream.call_args.args
                self.assertEqual(args, ['codex', '-a', 'never', 'exec', '--json',
                    *(['--model', model] if model else []),
                    '--output-last-message', '/work/final-result.json', '--cd', str(root), '-'])
                self.assertEqual(directory, root)
                self.assertIs(passed_env, env)
                self.assertEqual(json.loads(prompt.split('\n')[0]), note)
                self.assertEqual(json.loads(prompt.split('Result JSON Schema:\n', 1)[1]), original)
                self.assertIn('exactly one JSON value', prompt)
                self.assertIn('Do not include Markdown code fences or prose', prompt)
                self.assertFalse((root/'result-schema.json').exists())
                self.assertEqual(schema, original)

    def test_union_requires_complete_matching_branch_and_sibling_constraints(self):
        schema = {'anyOf': [object_schema({'action': {'type': 'string', 'enum': ['keep']},
                                           'reason': {'type': 'string', 'minLength': 1}}),
                            object_schema({'action': {'type': 'string', 'enum': ['wait']},
                                           'reason': {'type': 'null'}})]}
        for value in ({'action': 'keep', 'reason': 'Proceed'}, {'action': 'wait', 'reason': None}):
            schema_check(value, schema)
        for value in ({'action': 'keep', 'reason': ''}, {'action': 'keep', 'reason': None},
                      {'action': 'wait', 'reason': 'Proceed'}, {'action': 'keep'},
                      {'action': 'keep', 'reason': 'Proceed', 'extra': True}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                schema_check(value, schema)
        with self.assertRaises(ValueError):
            schema_check('', {'anyOf': [{'type': 'string'}, {'type': 'null'}], 'minLength': 1})

    def test_shared_checker_preserves_original_constraints_and_host_error(self):
        schema = object_schema({'rows': {'type': 'array', 'minItems': 1, 'maxItems': 2,
            'items': object_schema({'text': {'type': 'string', 'minLength': 1, 'maxLength': 3, 'pattern': '^a'},
                                   'score': {'type': 'integer', 'minimum': 1, 'maximum': 2}})}})
        schema_check({'rows': [{'text': 'abc', 'score': 1}]}, schema)
        for rows in ([], [{'text': 'abc', 'score': 1}]*3, [{'text': '', 'score': 1}],
                     [{'text': 'abcd', 'score': 1}], [{'text': 'xyz', 'score': 1}],
                     [{'text': 'abc', 'score': 0}], [{'text': 'abc', 'score': 3}],
                     [{'text': 'abc', 'score': True}]):
            with self.subTest(rows=rows), self.assertRaises(QualificationError):
                qualification_check({'rows': rows}, schema)

    def test_adapter_checks_original_contract_before_atomic_publish(self):
        schema = object_schema({'reason': {'anyOf': [{'type': 'null'}, {'type': 'string', 'minLength': 1}]}})
        for reason in ('', 'Proceed', None):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                result = {'reason': reason}
                (root/'final-result.json').write_text(json.dumps(result))
                module = adapter('codex')
                namespace = module['run'].__globals__
                with patch.dict(namespace, {'Path': lambda path: root/Path(path).name,
                                            'prepare_tool_environment': lambda *args: {}}), \
                     patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
                     patch('runpy.run_path', return_value={}), \
                     patch.dict(module['EXECUTION'], {'stream': lambda *args: (0, [])}):
                    if reason == '':
                        with self.assertRaisesRegex(ValueError, 'result_schema_invalid'):
                            module['run']({'id': 'job', 'role': 'triage'}, root, {}, schema)
                        self.assertFalse((root/'result.json').exists())
                        self.assertTrue((root/'final-result.json').exists())
                    else:
                        self.assertEqual(module['run']({'id': 'job', 'role': 'triage'}, root, {}, schema), 0)
                        self.assertEqual(json.loads((root/'result.json').read_text()), result)
                self.assertEqual(schema['properties']['reason']['anyOf'][1]['minLength'], 1)

    def test_adapter_rejects_prose_fences_and_multiple_json_values(self):
        schema = object_schema({'reason': {'type': 'string', 'minLength': 1}})
        for raw in ('```json\n{"reason":"Proceed"}\n```', 'Result: {"reason":"Proceed"}',
                    '{"reason":"Proceed"} {"reason":"Again"}'):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                (root/'final-result.json').write_text(raw)
                module = adapter('codex')
                with patch.dict(module['run'].__globals__, {'Path': lambda path: root/Path(path).name,
                                            'prepare_tool_environment': lambda *args: {}}), \
                     patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)), \
                     patch('runpy.run_path', return_value={}), \
                     patch.dict(module['EXECUTION'], {'stream': lambda *args: (0, [])}):
                    with self.assertRaises(json.JSONDecodeError):
                        module['run']({'id': 'job', 'role': 'triage'}, root, {}, schema)
                self.assertFalse((root/'result.json').exists())
                self.assertTrue((root/'final-result.json').exists())
