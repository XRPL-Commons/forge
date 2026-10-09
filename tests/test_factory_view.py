import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.control import view
from forge.factory.ledger import Ledger


class FactoryViewTests(unittest.TestCase):
    def test_ledger_reads_keep_validation_and_decision_fences_without_rewrites(self):
        from forge.factory.ledger import DecisionChanged
        from forge.state import StateError
        with tempfile.TemporaryDirectory() as directory:
            ledger = Ledger(directory)
            self.assertEqual(ledger.read()['version'], 1)
            self.assertTrue(ledger.store.marker.exists())
            key = ledger.discover({'id': 1, 'name': 'example/project'}, 1, 'requirements')
            before = ledger.store.path.stat()
            ledger.read()
            self.assertEqual(ledger.store.path.stat().st_mtime_ns, before.st_mtime_ns)
            ledger.decision_fence = (key, -1)
            with self.assertRaises(DecisionChanged): ledger.read()
            ledger.decision_fence = None
            ledger.store.path.write_text('{}')
            with self.assertRaises(StateError): ledger.read()

    def test_large_reports_do_not_break_activity_or_erase_merged_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = Ledger(root / 'state/factory')
            key = ledger.discover({'id': 1, 'name': 'example/project'}, 1, 'requirements')
            large = 'report-only-evidence-' * 60000
            with ledger.transaction() as data:
                item = data['issues'][key]
                item.update(state='merged', evidence={'audit': {'summary': large}},
                            github_observation={'issue': {'body': large}}, retry_plan=large,
                            candidate={'pr': 2, 'head': 'a'*40, 'summary': large})
                item['failures'] = [{'charged': True, 'evidence': [large], 'findings': large}]
                ledger.event(data, item, 'merged', sha='b'*40, evidence=large)
                for _ in range(10):
                    ledger.event(data, item, 'deployment_checked', evidence=large)
                data['jobs']['test-job'] = {'id': 'test-job', 'issue_key': key, 'role': 'build',
                    'cleanup': False, 'outcome': None, 'phase': 'running', 'slot': 'build-1',
                    'ci_evidence': large, 'runner_config': {'unused': large},
                    'observation': {'at': 10, 'execution': {'started_at': 5}, 'logs': large}}
                data['slots']['build-1'] = 'test-job'
            original = ledger.read()
            with patch('forge.factory.control.Settings') as settings, \
                    patch('forge.factory.operator_status.overview', return_value={}):
                settings.return_value.view.return_value = {'repositories': []}
                result = view(root)
            self.assertLess(len(json.dumps(result)), 20000)
            self.assertNotIn('report-only-evidence', json.dumps(result))
            row = result['issues'][0]
            self.assertEqual(row['failure_count'], 1)
            self.assertEqual(row['candidate']['pr'], 2)
            self.assertEqual([e['kind'] for e in row['history']], ['merged'] + ['deployment_checked']*3)
            self.assertEqual(result['jobs'][0]['observed_at'], 10)
            self.assertEqual(result['jobs'][0]['started_at'], 5)
            self.assertEqual(ledger.read(), original)


class FactorySectionsTests(unittest.TestCase):
    def payload(self):
        return {'issues': [
            {'key': '1:1', 'repo_id': 1, 'state': 'ready', 'active': 'build', 'title': 'Current', 'history': []},
            {'key': '1:2', 'repo_id': 1, 'state': 'merged', 'active': None, 'title': 'Completed', 'history': [{'kind': 'merged', 'at': 100}]},
            {'key': '1:3', 'repo_id': 1, 'state': 'investigating', 'active': 'recovery', 'title': 'Investigation', 'history': []}],
            'jobs': [{'id': 'build', 'issue_key': '1:1', 'role': 'build', 'phase': 'running'},
                     {'id': 'recovery', 'issue_key': '1:3', 'role': 'recovery', 'phase': 'running'},
                     {'id': 'orphan', 'issue_key': '1:2', 'role': 'recovery', 'phase': 'stopping'}],
            'repositories': [{'id': 1, 'name': 'example/project', 'enabled': True,
                              'protection': {'large': 'unneeded'}, 'environment': {'evidence': 'unneeded'},
                              'deployment': {'profile': {'strategy': 'health'}, 'proposal': 'unneeded'}}]}

    def test_board_keeps_metrics_without_loading_collapsed_cards(self):
        from forge.factory.control import select_view
        payload = self.payload()
        result = select_view(payload, section='board', now=101)
        self.assertEqual([row['key'] for row in result['issues']], ['1:1'])
        self.assertEqual([row['id'] for row in result['jobs']], ['build'])
        summary = result['summary']
        self.assertEqual((summary['completed_count'], summary['runner_count']), (1, 2))
        self.assertTrue(summary['runners_running'])
        self.assertEqual((summary['merged_day'], summary['merged_week'], summary['waiting']), (1, 1, 1))
        self.assertEqual(len(summary['workers']), 3)
        self.assertNotIn('unneeded', json.dumps(result))
        self.assertEqual(payload, self.payload(), 'projection must not mutate durable input')

    def test_sections_and_issue_views_only_carry_the_requested_rows(self):
        from forge.factory.control import select_view
        completed = select_view(self.payload(), section='completed')
        self.assertEqual([row['key'] for row in completed['issues']], ['1:2'])
        self.assertEqual(completed['jobs'], [])
        runners = select_view(self.payload(), section='runners')
        self.assertEqual([row['key'] for row in runners['issues']], ['1:3'])
        self.assertEqual([row['id'] for row in runners['jobs']], ['recovery', 'orphan'])
        detail = select_view(self.payload(), issue='1:1')
        self.assertEqual([row['id'] for row in detail['jobs']], ['build'])
        for kwargs in ({'section': 'secret'}, {'section': 'board', 'issue': '1:1'}):
            with self.assertRaises(ValueError): select_view(self.payload(), **kwargs)

    def completed_payload(self, count=25):
        payload = self.payload()
        rows = [{'key': f'2:{number}', 'repo_id': 2, 'state': 'merged' if number % 4 else 'cancelled', 'active': None,
                 'title': f'Done {number}', 'history': [{'kind': 'merged' if number % 4 else 'cancelled', 'at': 1000 + number},
                                                        {'kind': 'deployment_checked', 'at': 5000}]}
                for number in range(count)]
        # One completed issue with no completion event sorts last.
        rows.append({'key': '2:999', 'repo_id': 2, 'state': 'merged', 'active': None, 'title': 'Old', 'history': []})
        return {**payload, 'issues': [*payload['issues'], *rows]}

    def test_completed_pages_newest_first_with_a_stable_cursor(self):
        from forge.factory.control import select_view
        payload = self.completed_payload()
        first = select_view(payload, section='completed', limit='10')
        self.assertEqual([row['key'] for row in first['issues']], [f'2:{n}' for n in range(24, 14, -1)])
        self.assertEqual(first['issues'][0]['completed_at'], 1024)
        self.assertEqual(first['jobs'], [])
        self.assertEqual(first['next'], "1015:2:15")
        # A newer completion between two page reads does not shift the next page.
        newer = {'key': '2:500', 'repo_id': 2, 'state': 'merged', 'active': None, 'title': 'New',
                 'history': [{'kind': 'merged', 'at': 9999}]}
        grown = {**payload, 'issues': [*payload['issues'], newer]}
        second = select_view(grown, section='completed', limit=10, before=first['next'])
        self.assertEqual([row['key'] for row in second['issues']], [f'2:{n}' for n in range(14, 4, -1)])
        third = select_view(payload, section='completed', limit=10, before=second['next'])
        self.assertEqual([row['key'] for row in third['issues']], [*(f'2:{n}' for n in range(4, -1, -1)), '1:2', '2:999'])
        self.assertIsNone(third['next'])
        self.assertIsNone(third['issues'][-1]['completed_at'])
        exact = select_view(self.completed_payload(8), section='completed', limit=10)
        self.assertEqual(len(exact['issues']), 10)
        self.assertIsNone(exact['next'])
        self.assertEqual(payload, self.completed_payload(), 'paging must not mutate durable input')

    def test_unpaged_completed_view_is_unchanged_and_bad_paging_is_refused(self):
        from forge.factory.control import select_view
        unpaged = select_view(self.completed_payload(), section='completed')
        self.assertEqual(len(unpaged['issues']), 27)
        self.assertNotIn('next', unpaged)
        for kwargs in ({'section': 'completed', 'limit': 0}, {'section': 'completed', 'limit': 51},
                       {'section': 'completed', 'limit': 'ten'}, {'section': 'completed', 'before': '1:2:3'},
                       {'section': 'completed', 'limit': 10, 'before': 'later:2:1'},
                       {'section': 'completed', 'limit': 10, 'before': '1015'},
                       {'section': 'board', 'limit': 10}, {'issue': '1:1', 'limit': 10}):
            with self.assertRaises(ValueError, msg=kwargs): select_view(self.completed_payload(), **kwargs)

    def test_view_keeps_the_cancellation_time_in_the_history_tail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ledger = Ledger(root / 'state/factory')
            key = ledger.discover({'id': 1, 'name': 'example/project'}, 1, 'requirements')
            ledger.cancel(key, 'Closed on GitHub')
            with ledger.transaction() as data:
                for _ in range(4):
                    ledger.event(data, data['issues'][key], 'deployment_checked')
            with patch('forge.factory.control.Settings') as settings, \
                    patch('forge.factory.operator_status.overview', return_value={}):
                settings.return_value.view.return_value = {'repositories': []}
                result = view(root)
            self.assertEqual([e['kind'] for e in result['issues'][0]['history']], ['cancelled'] + ['deployment_checked'] * 3)
