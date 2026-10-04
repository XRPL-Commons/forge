import copy
import unittest

from forge.factory.contracts import result, assignment_note
from forge.factory.harness import schema
from tests.test_factory_adapters import WorkerContractTests


class DecisionContractTests(unittest.TestCase):
    def test_dependency_wait_contract_and_rolling_upgrade(self):
        job = WorkerContractTests().note('build')
        report = {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'waiting_on_issues',
                  'summary': 'Foundation is still open.', 'issues': [90], 'evidence': 'Use the foundation from #90.'}
        self.assertEqual(result(copy.deepcopy(report), job)['issues'], [90])
        for invalid in ([], [1], [0], [True], ['90'], [90, 90], list(range(2, 23))):
            with self.subTest(issues=invalid), self.assertRaises(ValueError):
                result({**report, 'issues': invalid}, job)
        for evidence in (None, [], '', ' ' , 'x' * 2001):
            with self.subTest(evidence=evidence), self.assertRaises(ValueError):
                result({**report, 'evidence': evidence}, job)
        for outcome in ('pass', 'needs_decision', 'blocked_environment'):
            with self.subTest(outcome=outcome), self.assertRaises(ValueError):
                result({**report, 'outcome': outcome}, job)
        review = WorkerContractTests().note('review')
        with self.assertRaises(ValueError):
            result({**report, 'role': 'review', 'attempt': review['id'], 'candidate': 'candidate-key'}, review)
        self.assertIn('waiting_on_issues', schema(job)['properties']['outcome']['enum'])
        self.assertNotIn('waiting_on_issues', schema(review)['properties']['outcome']['enum'])
        instructions = assignment_note(job)['instructions']
        self.assertIn('resumes automatically', instructions)
        self.assertIn('Reserve dependency_access', instructions)
        result({**report, 'outcome': 'pass', 'issues': [], 'evidence': None}, job)

    def test_only_complete_build_questions_are_accepted(self):
        job = WorkerContractTests().note('build')
        decision = {'question': 'Wait for upstream?', 'options': ['Wait', 'Remove the dependency'],
                    'recommendation': 'Wait', 'evidence': 'No compatible fix exists.'}
        report = {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'needs_decision',
                  'summary': 'A policy choice is required.', 'decision': decision}
        self.assertEqual(result(copy.deepcopy(report), job)['decision'], decision)
        for invalid in [None, {**decision, 'options': []}, {**decision, 'question': ''},
                        {**decision, 'evidence': None}, {**decision, 'extra': 'unknown'}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                result({**report, 'decision': invalid}, job)
        with self.assertRaises(ValueError):
            result({**report, 'outcome': 'pass'}, job)
        review = WorkerContractTests().note('review')
        with self.assertRaises(ValueError):
            result({**report, 'role': 'review', 'attempt': review['id'], 'candidate': 'candidate-key'}, review)
        self.assertIn('needs_decision', schema(job)['properties']['outcome']['enum'])
        self.assertNotIn('needs_decision', schema(review)['properties']['outcome']['enum'])
        instructions = assignment_note(job)['instructions']
        self.assertIn('do not open a PR', instructions)
        self.assertIn('Never commit raw tool evidence', instructions)

    def test_previous_image_results_stay_readable(self):
        job = WorkerContractTests().note('build')
        report = {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'pass', 'summary': 'Done'}
        self.assertEqual(result(report, job)['outcome'], 'pass')
