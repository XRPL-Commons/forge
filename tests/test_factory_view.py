import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.control import view
from forge.factory.ledger import Ledger


class FactoryViewTests(unittest.TestCase):
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
