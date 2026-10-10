"""031 changes the read contract, preserving stored authority and replay."""
import unittest
from unittest.mock import patch

from forge.store import db as storage, upgrade
from forge.factory.issue_selection import IssueSelection
from forge.factory.store_views import PipelineViews
from forge.factory.config import validate
from tests import test_dependency_filter_parity as fixtures
from tests.test_issue_blocker_lookup import IssueBlockerLookupTests


class IssueSelectionMigrationTests(unittest.TestCase):
    def test_030_upgrade_keeps_records_and_exposes_canonical_dependency_status(self):
        migrations = storage._migrations()
        fixture = fixtures.DependencyFilterParityTests()
        try:
            with patch('forge.store.db._migrations', return_value=migrations[:30]):
                fixture.setUp()
                plan = fixture.prepare()
                child, manifest = fixture.child(plan, '0'); fixture.accept(child, manifest, '0')
                before = IssueBlockerLookupTests.data(fixture.store)
                with fixture.store.read_snapshot() as db:
                    self.assertEqual(db.execute("SELECT public_status FROM public_issue_index WHERE id='issue-1'").fetchone()[0], 'blocked')
            store = storage.Store(fixture.store.root)
            applied = upgrade.apply(store)
            self.assertEqual(applied['current_schema'], 31)
            self.assertEqual(IssueBlockerLookupTests.data(store), before)
            with store.read_snapshot() as db:
                self.assertIsNone(db.execute("SELECT name FROM sqlite_schema WHERE name='public_issue_index'").fetchone())
                self.assertEqual(db.execute("SELECT indexed_status FROM issue_selection_facts WHERE id='issue-1'").fetchone()[0], 'blocked')
                self.assertEqual(IssueSelection(db).execute("SELECT public_status FROM public_issue_index WHERE id='issue-1'").fetchone()[0], 'queued')
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            view = PipelineViews(store, runtime=validate({}), clock=lambda:100)
            self.assertIn('issue-1', [row['key'] for row in view.issues(state='queued')['issues']])
            self.assertEqual(upgrade.apply(store), applied)
            self.assertEqual(IssueBlockerLookupTests.data(store), before)
        finally:
            fixture.doCleanups()
