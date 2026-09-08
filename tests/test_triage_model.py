import copy
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace
import unittest
from forge.triage_model import propose, validate, TriageError


class TriageModelTests(unittest.TestCase):
    def setUp(self):
        self.issues = [
            {'number': 1, 'title': 'Auth fix', 'body': 'Current auth is broken.', 'labels': [{'name': 'p1'}]},
            {'number': 2, 'title': 'Follow up', 'body': 'This depends on #1. Coordinate with #3 for rollout.', 'labels': []},
            {'number': 3, 'title': 'Complementary work', 'body': 'Complements #1; operational rollout with #2.', 'labels': []},
        ]
        self.proposal = {'issues': [
            {'number': 1, 'priority': 'p1', 'reason': 'Preserve explicit priority', 'dependencies': []},
            {'number': 2, 'priority': 'p2', 'reason': 'Follow-up work', 'dependencies': [{'number': 1, 'evidence': 'depends on #1'}]},
            {'number': 3, 'priority': 'p2', 'reason': 'Independent improvement', 'dependencies': []},
        ]}

    def transport(self, value=None, **overrides):
        envelope = {'type': 'result', 'subtype': 'success', 'is_error': False,
                    'structured_output': self.proposal if value is None else value}
        return SimpleNamespace(returncode=0, stdout=json.dumps(envelope), stderr='', **overrides)

    def test_valid_proposal_uses_restricted_fresh_transport(self):
        directories = []
        def run(cmd, **kwargs):
            for flag in ('--restricted', '--safe-mode', '--strict-mcp-config', '--disable-slash-commands'):
                self.assertIn(flag, cmd)
            for flag, value in [('--tools', ''), ('--setting-sources', ''), ('--permission-prompts', 'none'), ('--output-format', 'json')]:
                self.assertEqual(cmd[cmd.index(flag) + 1], value)
            schema = json.loads(cmd[cmd.index('--json-schema') + 1])
            self.assertFalse(schema['additionalProperties'])
            self.assertTrue(Path(kwargs['cwd']).is_dir())
            self.assertEqual(list(Path(kwargs['cwd']).iterdir()), [])
            self.assertEqual(kwargs['timeout'], 120)
            self.assertIn('UNTRUSTED', kwargs['input'])
            directories.append(kwargs['cwd'])
            return self.transport()
        self.assertEqual(propose('X/y', self.issues, run=run), self.proposal)
        self.assertFalse(Path(directories[0]).exists())

    def test_rejects_human_priority_changes(self):
        self.proposal['issues'][0]['priority'] = 'p0'
        with self.assertRaisesRegex(TriageError, 'explicit priority'):
            validate('X/y', self.issues, self.proposal)

    def test_rejects_missing_duplicate_unknown_and_boolean_ids(self):
        for transform in (
                lambda p: p['issues'].pop(),
                lambda p: p['issues'].append(copy.deepcopy(p['issues'][0])),
                lambda p: p['issues'][0].update(number=99),
                lambda p: p['issues'][0].update(number=True)):
            value = copy.deepcopy(self.proposal)
            transform(value)
            with self.assertRaises(TriageError): validate('X/y', self.issues, value)

    def test_rejects_unsupported_and_fabricated_dependency_evidence(self):
        for number, evidence in [(3, 'Coordinate with #3'), (1, 'blocked by #1'),
                                 (99, 'depends on #99'), (2, 'depends on #2')]:
            value = copy.deepcopy(self.proposal)
            value['issues'][1]['dependencies'] = [{'number': number, 'evidence': evidence}]
            with self.assertRaises(TriageError): validate('X/y', self.issues, value)

    def test_rejects_complements_and_operational_rollout_as_dependencies(self):
        for number, evidence in [(1, 'Complements #1'), (2, 'operational rollout with #2')]:
            value = copy.deepcopy(self.proposal)
            value['issues'][2]['dependencies'] = [{'number': number, 'evidence': evidence}]
            with self.assertRaises(TriageError): validate('X/y', self.issues, value)

    def test_rejects_cross_repository_reference_and_negated_dependency(self):
        for evidence in ['depends on Other/repo#1', 'does not depend on #1']:
            issues = copy.deepcopy(self.issues)
            issues[1]['body'] = evidence
            value = copy.deepcopy(self.proposal)
            value['issues'][1]['dependencies'][0]['evidence'] = evidence
            with self.assertRaises(TriageError): validate('X/y', issues, value)

    def test_rejects_cycle(self):
        self.issues[0]['body'] = 'blocked by #2'
        self.proposal['issues'][0]['dependencies'] = [{'number': 2, 'evidence': 'blocked by #2'}]
        with self.assertRaisesRegex(TriageError, 'cyclic'): validate('X/y', self.issues, self.proposal)

    def test_transport_errors_never_return_partial_proposal(self):
        outputs = [SimpleNamespace(returncode=1, stdout='', stderr='secret details'),
                   SimpleNamespace(returncode=0, stdout='garbage', stderr=''),
                   SimpleNamespace(returncode=0, stdout=json.dumps({'type': 'result', 'subtype': 'error', 'is_error': True}), stderr='')]
        for result in outputs:
            with self.assertRaises(TriageError): propose('X/y', self.issues, run=lambda *a, **k: result)
        def timeout(*args, **kwargs): raise subprocess.TimeoutExpired('claude', 1)
        with self.assertRaises(TriageError): propose('X/y', self.issues, run=timeout)

    def test_rejects_extra_actions_and_non_schema_fields(self):
        self.proposal['issues'][0]['command'] = 'gh issue edit'
        with self.assertRaises(TriageError): validate('X/y', self.issues, self.proposal)

    def test_empty_input_has_no_model_call(self):
        self.assertEqual(propose('X/y', [], run=lambda *a, **k: self.fail('called')), {'issues': []})

    def test_cropped_negated_statement_does_not_create_dependency(self):
        self.issues[1]['body'] = 'This is not blocked by #1.'
        self.proposal['issues'][1]['dependencies'][0]['evidence'] = 'blocked by #1'
        with self.assertRaises(TriageError): validate('X/y', self.issues, self.proposal)

    def test_unsupported_explicit_priority_is_never_replaced(self):
        self.issues[0]['labels'] = ['p4']
        with self.assertRaises(TriageError): propose('X/y', self.issues, run=lambda *a, **k: self.fail('called'))

    def test_malformed_priority_type_is_rejected(self):
        self.proposal['issues'][0]['priority'] = ['p1']
        with self.assertRaises(TriageError): validate('X/y', self.issues, self.proposal)

    def test_explicit_described_dependency_list_and_build_on(self):
        issues = copy.deepcopy(self.issues)
        issues[2]['body'] = 'Depend on verified context (#1), and typed receipts (#2).'
        proposal = copy.deepcopy(self.proposal)
        proposal['issues'][2]['dependencies'] = [
            {'number': n, 'evidence': issues[2]['body']} for n in (1, 2)]
        self.assertEqual(validate('X/y', issues, proposal), proposal)
        issues[2]['body'] = 'Build on #1’s trusted context.'
        proposal['issues'][2]['dependencies'] = [{'number': 1, 'evidence': issues[2]['body']}]
        self.assertEqual(validate('X/y', issues, proposal), proposal)

    def test_dependency_clause_does_not_extend_into_coordination(self):
        issues = copy.deepcopy(self.issues)
        issues[2]['body'] = 'Depend on verified context (#1), coordinate with receipts (#2).'
        proposal = copy.deepcopy(self.proposal)
        proposal['issues'][2]['dependencies'] = [{'number': 2, 'evidence': issues[2]['body']}]
        with self.assertRaises(TriageError): validate('X/y', issues, proposal)


    def test_complete_positive_sentence_ignores_neighboring_negation(self):
        self.issues[1]['body'] = 'Depends on #1. This is not blocked by #3.'
        self.proposal['issues'][1]['dependencies'][0]['evidence'] = 'Depends on #1.'
        self.assertEqual(validate('X/y', self.issues, self.proposal), self.proposal)

    def test_complete_cropped_negation_still_rejected(self):
        self.issues[1]['body'] = 'This is not blocked by #1. Independent work follows.'
        self.proposal['issues'][1]['dependencies'][0]['evidence'] = 'blocked by #1.'
        with self.assertRaises(TriageError): validate('X/y', self.issues, self.proposal)
