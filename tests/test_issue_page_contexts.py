"""Bounded page facts preserve current and historical membership selection."""
import unittest

from forge.store.queries import Queries
from tests.test_completed_selection import CompletedSelectionTests


class IssuePageContextTests(unittest.TestCase):
    setUp = CompletedSelectionTests.setUp

    def test_page_reuses_bounded_facts_without_losing_former_or_current_members(self):
        identifiers = ['1:' + str(number) for number in range(13, 0, -1)]
        statements = []
        with self.store.read_snapshot() as db:
            db.set_trace_callback(statements.append)
            contexts = Queries._issue_contexts(db, identifiers)
            db.set_trace_callback(None)
            self.assertEqual(list(contexts), identifiers)
            for identifier, (issue, unit) in contexts.items():
                with self.subTest(issue=identifier):
                    member = db.execute('SELECT * FROM unit_issues WHERE issue_id=? '
                        'ORDER BY (left_at IS NULL) DESC,joined_at DESC,id DESC LIMIT 1', (identifier,)).fetchone()
                    self.assertEqual(unit['id'] if unit else None, member['unit_id'] if member else None)
                    self.assertNotIn('body', issue)
                    self.assertNotIn('requirements', issue)
                    if member and member['left_at'] is None and unit['state'] not in {'merged', 'closed', 'dissolved'}:
                        self.assertEqual(issue['lifecycle']['member_id'], member['id'])
                    else:
                        self.assertIsNone(issue['lifecycle']['member_id'])
            # Reopened and removed issues keep history without inheriting old work.
            self.assertEqual(contexts['1:5'][0]['lifecycle']['state'], 'untracked')
            self.assertEqual(contexts['1:13'][0]['lifecycle']['state'], 'untracked')
            self.assertEqual(contexts['1:8'][1]['id'], 'batch')
            self.assertNotEqual(contexts['1:9'][1]['id'], 'batch')
            for number in (11, 12):
                self.assertNotEqual(contexts['1:' + str(number)][1]['id'], 'split')
            self.assertIsNone(contexts['1:6'][1])
            self.assertIsNone(contexts['1:7'][1])
        for prefix in ('SELECT i.*, r.title', 'SELECT m.* FROM issues', 'SELECT * FROM units WHERE id IN'):
            self.assertEqual(sum(sql.startswith(prefix) for sql in statements), 1, prefix)
        for field in ('r.body', 'requirements_json', 'job_artifacts', 'import_records'):
            self.assertFalse(any(field in sql for sql in statements), field)

    def test_empty_missing_and_bounded_pages_are_explicit(self):
        with self.store.read_snapshot() as db:
            self.assertEqual(Queries._issue_contexts(db, []), {})
            with self.assertRaises(KeyError):
                Queries._issue_contexts(db, ['1:1', '1:999'])
            with self.assertRaises(ValueError):
                Queries._issue_contexts(db, [str(number) for number in range(101)])
            detail = Queries._issue_contexts(db, ['1:1'], include_body=True)['1:1'][0]
            self.assertIn('body', detail)
            self.assertIn('requirements', detail)
