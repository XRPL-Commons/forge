"""031 changes the read contract, preserving stored authority and replay."""
import unittest
from unittest.mock import patch

from forge.store import db as storage, upgrade
from forge.factory.issue_selection import IssueSelection
from forge.factory.store_views import PipelineViews
from forge.factory.config import validate
from tests import test_issue_blocker_lookup as blocker


class IssueSelectionMigrationTests(unittest.TestCase):
    def test_030_upgrade_keeps_records_and_exposes_canonical_dependency_status(self):
        migrations = storage._migrations()
        import tempfile
        from tests.historical_store_fixtures import accepted_dependency
        with tempfile.TemporaryDirectory() as root:
            with patch('forge.store.db._migrations', return_value=migrations[:30]):
                source = storage.Store.create(root)
                accepted_dependency(source)
                before = blocker.IssueBlockerLookupTests.data(source)
                with source.read_snapshot() as db:
                    self.assertEqual(db.execute("SELECT public_status FROM public_issue_index WHERE id='issue-1'").fetchone()[0], 'blocked')
            with patch('forge.store.db._migrations', return_value=migrations[:31]):
                boundary = storage.Store(root)
                applied = upgrade.apply(boundary)
                self.assertEqual(applied['current_schema'], 31)
                self.assertEqual(blocker.IssueBlockerLookupTests.data(boundary), before)
                with boundary.read_snapshot() as db:
                    self.assertIsNone(db.execute("SELECT name FROM sqlite_schema WHERE name='public_issue_index'").fetchone())
                    self.assertEqual(db.execute("SELECT indexed_status FROM issue_selection_facts WHERE id='issue-1'").fetchone()[0], 'blocked')
                self.assertEqual(upgrade.apply(boundary), applied)
            # The current projector only runs against its supported current schema.
            store = storage.Store(root)
            applied = upgrade.apply(store)
            with store.read_snapshot() as db:
                self.assertEqual(IssueSelection(db).execute("SELECT public_status FROM public_issue_index WHERE id='issue-1'").fetchone()[0], 'waiting')
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(db.execute("SELECT task_id FROM part_outputs WHERE id='output'").fetchone()[0], 'v1:plan:0')
            after = blocker.IssueBlockerLookupTests.data(store)
            for table in ('issues', 'issue_revisions', 'attempts', 'job_members', 'delegation_plans'):
                self.assertEqual(after[table], before[table], table)
            view = PipelineViews(store, runtime=validate({}), clock=lambda:100)
            self.assertIn('issue-1', [row['key'] for row in view.issues(state='waiting')['issues']])
            self.assertEqual(upgrade.apply(store), applied)
            self.assertEqual(blocker.IssueBlockerLookupTests.data(store), after)
