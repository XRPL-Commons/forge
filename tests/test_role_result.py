"""Published structured role results fail closed without external operations."""
import json
import unittest

from forge.role_result import parse_role_result


class RoleResultTests(unittest.TestCase):
    def setUp(self):
        self.record = {'schema': 1, 'role': 'review', 'outcome': 'fail',
                       'head': 'a' * 40, 'base': 'b' * 40,
                       'assignment_id': '11111111-1111-4111-8111-111111111111'}

    def body(self, record=None):
        return 'FORGE_RESULT: ' + json.dumps(self.record if record is None else record)

    def test_exact_results_accept_both_roles_and_outcomes_without_inventing_fields(self):
        for role in ('review', 'audit'):
            for outcome in ('pass', 'fail'):
                record = {**self.record, 'role': role, 'outcome': outcome}
                self.assertEqual(parse_role_result('Findings and evidence.\n' + self.body(record) + '\nChecks passed.'), record)

    def test_absent_marker_is_distinct_from_invalid_marker(self):
        self.assertIsNone(parse_role_result('Reviewer: FAIL\nChanges are requested.'))
        for body in ('FORGE_RESULT', 'FORGE_RESULT {}', 'FORGE_RESULT: broken',
                     ' FORGE_RESULT: {}', '> FORGE_RESULT: {}', '`FORGE_RESULT: {}`',
                     'FORGE_RESULT:\n{}', 'FORGE_RESULT: '):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_role_result(body)

    def test_duplicate_identical_or_conflicting_markers_are_invalid(self):
        for second in (self.body(), self.body({**self.record, 'outcome': 'pass'}), 'FORGE_RESULT: broken'):
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, 'multiple'):
                parse_role_result(self.body() + '\n' + second)

    def test_json_duplicate_keys_are_invalid_even_if_values_match(self):
        for duplicate in ('"schema":1', '"outcome":"pass"'):
            with self.subTest(duplicate=duplicate), self.assertRaisesRegex(ValueError, 'duplicate'):
                parse_role_result(self.body()[:-1] + ',' + duplicate + '}')

    def test_exact_keys_types_and_values_are_required(self):
        invalid = [[], None, {**self.record, 'extra': True},
                   {key: value for key, value in self.record.items() if key != 'base'}]
        for key, values in {'schema': [True, '1', 1.0, 2],
                            'role': ['build', 'Review', None, []],
                            'outcome': ['FAIL', 'blocked', False, {}],
                            'head': ['a' * 39, 'g' * 40, 'A' * 40, 123, None],
                            'base': ['b' * 41, 'b' * 40 + '\n', {}],
                            'assignment_id': ['not-a-uuid', None, 1, '11111111111141118111111111111111']}.items():
            invalid.extend({**self.record, key: value} for value in values)
        for record in invalid:
            with self.subTest(record=record), self.assertRaises(ValueError):
                parse_role_result('FORGE_RESULT: ' + json.dumps(record))

    def test_full_revision_and_assignment_bindings_must_match(self):
        expected = {'expected_head': self.record['head'], 'expected_base': self.record['base'],
                    'expected_assignment_id': self.record['assignment_id'], 'expected_role': 'review'}
        self.assertEqual(parse_role_result(self.body(), **expected), self.record)
        for key in expected:
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'mismatch'):
                parse_role_result(self.body(), **{**expected, key: 'different'})

    def test_multiline_json_trailing_payload_or_nonobject_is_invalid(self):
        for body in ('FORGE_RESULT: ' + json.dumps(self.record, indent=2),
                     self.body() + ' trailing text', 'FORGE_RESULT: []',
                     'FORGE_RESULT: ' + '[' * 5000):
            with self.subTest(body=body[:80]), self.assertRaises(ValueError):
                parse_role_result(body)

    def test_sha256_repository_revisions_are_supported(self):
        record = {**self.record, 'head': 'a' * 64, 'base': 'b' * 64}
        self.assertEqual(parse_role_result(self.body(record)), record)

    def test_nontext_body_is_invalid(self):
        for body in (None, [], {}, 1):
            with self.subTest(body=body), self.assertRaises(ValueError):
                parse_role_result(body)
