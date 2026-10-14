"""The indexed blocker and completion lookups use their indexes."""
import tempfile
import unittest

from forge.factory.store_views import COMPLETION_SQL
from forge.factory.issue_selection import IssueSelection
from tests import test_operator_status_matrix as matrix


class IssueBlockerLookupTests(unittest.TestCase):
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
