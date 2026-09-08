import unittest
from forge.triage_rules import propose
from forge.ordering import order_issues


class LocalTriageTests(unittest.TestCase):
    def test_existing_priority_wins_and_explicit_dependencies_are_extracted(self):
        data = [{'number': 1, 'title': 'Foundation', 'labels': ['ready', 'security'], 'body': ''},
                {'number': 2, 'title': 'Feature', 'labels': ['ready', 'p3'], 'body': 'Build on #1. Coordinate with #3.'},
                {'number': 3, 'title': 'Unrelated', 'labels': ['ready'], 'body': ''}]
        result = propose('o/r', data)['issues']
        self.assertEqual([i['priority'] for i in result], ['p1', 'p3', 'p2'])
        self.assertEqual(result[1]['dependencies'], [{'number': 1, 'evidence': 'Build on #1.'}])

    def test_no_cropped_negation_or_cycle(self):
        data = [{'number': 1, 'labels': [], 'body': 'Depends on #2.'},
                {'number': 2, 'labels': [], 'body': 'Depends on #1. This is not blocked by #3.'},
                {'number': 3, 'labels': [], 'body': ''}]
        with self.assertRaises(ValueError):
            propose('o/r', data)
        data[1]['body'] = "This is not blocked by #3."
        self.assertEqual(propose('o/r', data)['issues'][1]['dependencies'], [])

    def test_numeric_ready_age_is_used(self):
        rows = [{'number': n, 'state': 'open', 'labels': ['ready', 'p1']} for n in (1, 2)]
        self.assertEqual(order_issues(rows, {}, first_ready={'1': 200, '2': 100})['eligible'], [2, 1])
