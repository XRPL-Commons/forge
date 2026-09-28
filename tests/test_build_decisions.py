import copy
import unittest

from forge.factory.contracts import result, assignment_note
from forge.factory.harness import schema
from tests.test_factory_adapters import WorkerContractTests


class DecisionContractTests(unittest.TestCase):
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
