"""The indexed blocker lookup preserves canonical classification across upgrade."""
import tempfile
import unittest
from unittest.mock import patch

from forge.store import codes, db as storage, upgrade
from forge.factory.store_views import COMPLETION_SQL
from forge.factory.issue_selection import IssueSelection
from tests import test_operator_status_matrix as matrix
from tests.historical_store_fixtures import status_case


class IssueBlockerLookupTests(unittest.TestCase):
    @staticmethod
    def rows(store, *, legacy=False):
        with store.read_snapshot() as db:
            rows = db.execute('SELECT * FROM public_issue_index ORDER BY id') if legacy else IssueSelection(db).execute('SELECT * FROM public_issue_index ORDER BY id')
            return [{key: row[key] for key in row.keys() if key not in {'indexed_status','indexed_reason_code'}} for row in rows]

    @staticmethod
    def data(store):
        with store.read_snapshot() as db:
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT IN ('schema_migrations','store_metadata') ORDER BY name")]
            return {name: [tuple(row) for row in db.execute('SELECT * FROM "'+name+'" ORDER BY rowid')]
                    for name in tables}

    def test_populated_upgrade_preserves_all_matrix_classifications_and_data(self):
        migrations = storage._migrations()
        for case in matrix.MATRIX:
            with self.subTest(case=case['name']), tempfile.TemporaryDirectory() as root:
                with patch('forge.store.db._migrations', return_value=migrations[:27]):
                    store = storage.Store.create(root)
                    status_case(store, case['name'])
                    before = self.rows(store, legacy=True)
                    data = self.data(store)
                # 028 changes a selector/index only: every stored row must be exact.
                with patch('forge.store.db._migrations', return_value=migrations[:28]):
                    boundary = storage.Store(root)
                    result = upgrade.apply(boundary)
                    self.assertEqual(result['current_schema'], 28)
                    self.assertEqual(self.rows(boundary, legacy=True), before)
                    self.assertEqual(self.data(boundary), data)
                    self.assertEqual(upgrade.apply(boundary), result)
                # Today's schema intentionally adds tables and normalizes process
                # gates. Legacy idle rows have no genuine schedule to migrate.
                store = storage.Store(root)
                result = upgrade.apply(store)
                self.assertEqual(result['current_schema'], len(migrations))
                expected = [{**row, 'public_status': 'working'} if codes.transitional(row['reason_code']) else row
                            for row in before]
                if case['name'] == 'queued':
                    expected = [{**row, 'public_status': 'waiting'} for row in expected]
                    with store.read_snapshot() as db:
                        self.assertEqual(db.execute('SELECT count(*) FROM next_actions').fetchone()[0], 0)
                self.assertEqual(self.rows(store), expected)
                self.assertEqual(upgrade.apply(store), result)
                self.assertEqual(self.rows(store), expected)
                after = self.data(store)
                for table in ('issues', 'issue_revisions', 'attempts', 'candidates',
                              'job_members', 'budget_entries', 'events'):
                    self.assertEqual(after[table], data[table], table)
                from forge.factory.store_views import PipelineViews
                from forge.factory.config import validate
                view = PipelineViews(store, runtime=validate({}), clock=lambda:100)
                self.assertEqual(view.issues(key='1:1')['issue']['status'], 'waiting' if case['name'] == 'queued' else case['expected']['code'])

    def test_issue_lookup_uses_its_indexed_gates_and_retains_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = matrix.OperatorStatusMatrixTests()
            fixture.prepare(root, 'operator_pause')
            fixture.life.open_gate(fixture.command(), 'environment', details={'reason':'database'})
            # Lower numeric priority wins, regardless of newer creation time.
            with fixture.store.read_snapshot() as db:
                expected = db.execute("SELECT g.code FROM unit_gates g JOIN gate_codes c ON c.code=g.code WHERE g.unit_id='u' AND g.resolved_at IS NULL ORDER BY c.priority,c.rowid,g.created_at,g.id LIMIT 1").fetchone()[0]
                row = IssueSelection(db).execute("SELECT reason_code FROM public_issue_index WHERE id='1:1'").fetchone()
                self.assertEqual(row[0], expected)
                plan = '\n'.join(row[3] for row in IssueSelection(db).execute("SELECT reason_code FROM public_issue_index WHERE id='1:1'", explain=True))
                self.assertIn('SEARCH g USING INDEX unresolved_gates', plan)
                self.assertNotIn('MATERIALIZE ranked', plan)

    def test_terminal_event_lookup_uses_completion_index(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = matrix.OperatorStatusMatrixTests()
            fixture.prepare(root, 'merged')
            with fixture.store.read_snapshot() as db:
                query = 'SELECT '+COMPLETION_SQL+" AS at FROM units u WHERE u.state='merged'"
                self.assertEqual(db.execute(query).fetchone()[0], 100)
                plan = '\n'.join(row[3] for row in db.execute('EXPLAIN QUERY PLAN '+query))
                self.assertIn('unit_completion_events', plan)
