"""The indexed blocker lookup preserves canonical classification across upgrade."""
import tempfile
import unittest
from unittest.mock import patch

from forge.store import codes, db as storage, upgrade
from forge.factory.store_views import COMPLETION_SQL
from tests import test_operator_status_matrix as matrix


class IssueBlockerLookupTests(unittest.TestCase):
    @staticmethod
    def rows(store):
        with store.read_snapshot() as db:
            return [dict(row) for row in db.execute('SELECT * FROM public_issue_index ORDER BY id')]

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
                fixture = matrix.OperatorStatusMatrixTests()
                with patch('forge.store.db._migrations', return_value=migrations[:27]):
                    fixture.prepare(root, case['name'])
                    before = self.rows(fixture.store)
                    data = self.data(fixture.store)
                    board = fixture.views.factory(issue='1:1')
                fixture.store = storage.Store(fixture.store.root)  # Target release handle.
                fixture.views.store = fixture.store
                result = upgrade.apply(fixture.store)
                self.assertEqual(result['current_schema'], len(migrations))
                # Only 030's transitional reclassification may change a row (#241).
                expected = [{**row, 'public_status': 'working'} if codes.transitional(row['reason_code']) else row
                            for row in before]
                self.assertEqual(self.rows(fixture.store), expected)
                self.assertEqual(self.data(fixture.store), data)
                self.assertEqual(fixture.views.factory(issue='1:1'), board)
                self.assertEqual(upgrade.apply(fixture.store), result)
                self.assertEqual(self.rows(fixture.store), expected)

    def test_issue_lookup_uses_its_indexed_gates_and_retains_precedence(self):
        with tempfile.TemporaryDirectory() as root:
            fixture = matrix.OperatorStatusMatrixTests()
            fixture.prepare(root, 'operator_pause')
            fixture.life.open_gate(fixture.command(), 'environment', details={'reason':'database'})
            # Lower numeric priority wins, regardless of newer creation time.
            with fixture.store.read_snapshot() as db:
                expected = db.execute("SELECT g.code FROM unit_gates g JOIN gate_codes c ON c.code=g.code WHERE g.unit_id='u' AND g.resolved_at IS NULL ORDER BY c.priority,c.rowid,g.created_at,g.id LIMIT 1").fetchone()[0]
                row = db.execute("SELECT reason_code FROM public_issue_index WHERE id='1:1'").fetchone()
                self.assertEqual(row[0], expected)
                plan = '\n'.join(row[3] for row in db.execute("EXPLAIN QUERY PLAN SELECT reason_code FROM public_issue_index WHERE id='1:1'"))
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
