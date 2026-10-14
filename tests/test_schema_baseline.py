"""The baseline schema and the one-time rebase of stores built by the 41 old migrations (#289)."""
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.store import baseline_rebase, db as storage, upgrade
from forge.store.schema_shape import compare, differences, shape

FIXTURES = Path(__file__).resolve().parent / 'fixtures/schema_v41'


def legacy_catalog():
    """The 41 migrations exactly as released, kept only for this test."""
    catalog = []
    for path in sorted(FIXTURES.glob('[0-9]*_*.sql')):
        payload = path.read_bytes()
        catalog.append((int(path.name[:3]), hashlib.sha256(payload).hexdigest(), payload.decode()))
    return catalog


SEED = """
INSERT INTO repositories(id,full_name) VALUES(1,'example/repo');
INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES('i',1,1,'open','r',1);
INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES('r','i','Title','Body','content','requirements','{}',1);
INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES('u',1,'main','building',1,1);
INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) VALUES('m','u','i',1,'main','r','pending',1);
INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('g1','u','unit','operator_pause',1);
INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at,resolved_at,resolution) VALUES('g2','u','unit','retry_delay',1,2,'time');
INSERT INTO events(transaction_id,unit_id,issue_id,kind,at,payload_json) VALUES('t1','u','i','unit_created',1,'{}');
INSERT INTO events(transaction_id,unit_id,issue_id,kind,at,payload_json) VALUES('t2','u','i','legacy_event',2,'{"legacy_kind":"merged"}');
INSERT INTO configuration(domain,subject,revision,active,settings_json,created_at) VALUES('runtime','instance',1,1,'{"enabled":true}',1);
INSERT INTO issue_action_requests(id,issue_id,action,expected_revision,status,reason,created_at) VALUES('q','i','close','r','rejected','test',1);
-- Rows in tables the baseline drops.
INSERT INTO import_runs(id,importer_version,source_revision,storage_generation,status,created_at,updated_at) VALUES('run','1','rev',1,'activated',1,1);
INSERT INTO notification_outbox(id,enqueue_key,channel,recipient_ref,payload_json,status,created_at) VALUES('n','k','mail','ops','{}','pending',1);
INSERT INTO imported_operator_requests(request_id,source_manifest,source_status,interrupted_at) VALUES('q','manifest','pending',1);
"""

KEPT = ('repositories', 'issues', 'issue_revisions', 'units', 'unit_issues', 'unit_gates', 'events',
        'configuration', 'issue_action_requests')


class SchemaBaselineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def legacy_store(self, name='legacy', seed=True):
        with patch('forge.store.db._migrations', return_value=legacy_catalog()):
            store = storage.Store.create(self.root / name)
        store = storage.Store(store.root)
        if seed:
            db = self.raw(store)
            db.execute('BEGIN')
            storage._execute_script(db, SEED)
            db.execute('COMMIT')
        return store

    def raw(self, store):
        db = sqlite3.connect(store.path, isolation_level=None)
        from forge.store.remote_time import register
        register(db)
        db.execute('PRAGMA foreign_keys=ON')
        self.addCleanup(db.close)
        return db

    def rows(self, store, tables=KEPT):
        db = self.raw(store)
        return {table: [tuple(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY rowid')] for table in tables}

    def test_frozen_manifest_matches_the_released_migrations(self):
        self.assertEqual([(version, checksum) for version, checksum, _ in legacy_catalog()],
                         list(baseline_rebase.LEGACY_HISTORY))

    def test_fresh_store_applies_only_the_baseline(self):
        catalog = storage._migrations()
        self.assertEqual([version for version, _, _ in catalog], [1])
        store = storage.Store.create(self.root / 'fresh')
        with store.read_snapshot() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT version,checksum FROM schema_migrations')],
                             [(1, catalog[0][1])])
        self.assertEqual(upgrade.inspect(store)['pending_migrations'], [])

    def test_baseline_is_the_old_schema_minus_the_dropped_objects(self):
        from forge.store.remote_time import register
        old = sqlite3.connect(':memory:')
        self.addCleanup(old.close)
        register(old)
        for _, _, script in legacy_catalog():
            storage._execute_script(old, script)
        new = sqlite3.connect(':memory:')
        self.addCleanup(new.close)
        register(new)
        storage._execute_script(new, storage._migrations()[0][2])
        found = differences(shape(old), shape(new))
        dropped = {f'unexpected table {table}' for table in baseline_rebase.DROPPED_TABLES}
        self.assertEqual(set(found), dropped | {
            'unexpected index notification_event_channels', 'unexpected index pending_notifications',
            'unexpected trigger import_documents_no_delete', 'unexpected trigger import_documents_no_update',
            'unexpected trigger import_records_no_delete', 'unexpected trigger import_records_no_update',
            'unexpected trigger migration_recovery_adoptions_no_delete',
            'unexpected trigger migration_recovery_adoptions_no_update',
            'different trigger recovery_assignment_valid: sql', 'different index unit_completion_events: sql',
            'different rows in gate_codes'})
        retired = {row[0] for row in old.execute('SELECT code FROM gate_codes')} - {row[0] for row in new.execute('SELECT code FROM gate_codes')}
        self.assertEqual(retired, set(baseline_rebase.RETIRED_GATE_CODES))

    def test_upgrade_rebases_the_legacy_store_onto_the_baseline_and_keeps_its_data(self):
        store = self.legacy_store()
        before = self.rows(store)
        status = upgrade.inspect(store)
        self.assertEqual((status['current_schema'], status['legacy_history'], status['pending_migrations']), (41, True, [1]))
        result = upgrade.apply(store)
        self.assertEqual((result['current_schema'], result['pending_migrations']), (1, []))
        self.assertEqual(self.rows(store), before)
        db = self.raw(store)
        self.assertEqual(compare(db, storage._migrations()[0][2]), [])
        self.assertEqual([tuple(row) for row in db.execute('SELECT version,checksum FROM schema_migrations')],
                         [(1, storage._migrations()[0][1])])
        for table in baseline_rebase.DROPPED_TABLES:
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name=?", (table,)).fetchone())
        # Normal open and validation accept the rebased store, and it equals a fresh one.
        opened = storage.Store.open(store.root)
        with opened.read_snapshot() as reader:
            self.assertEqual(reader.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()[0], '1')
        fresh = storage.Store.create(self.root / 'fresh')
        self.assertEqual(differences(shape(db), shape(self.raw(fresh))), [])

    def test_second_upgrade_is_a_no_op(self):
        store = self.legacy_store()
        upgrade.apply(store)
        history = [tuple(row) for row in self.raw(store).execute('SELECT * FROM schema_migrations')]
        snapshot = self.rows(store)
        self.assertEqual(upgrade.apply(storage.Store(store.root))['pending_migrations'], [])
        self.assertEqual([tuple(row) for row in self.raw(store).execute('SELECT * FROM schema_migrations')], history)
        self.assertEqual(self.rows(store), snapshot)

    def assert_still_legacy(self, store):
        db = self.raw(store)
        self.assertEqual(db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0], 41)
        self.assertEqual(db.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()[0], '41')
        self.assertEqual(db.execute('SELECT count(*) FROM import_runs').fetchone()[0], 1)

    def test_a_history_other_than_the_frozen_manifest_is_refused(self):
        store = self.legacy_store()
        db = self.raw(store)
        db.execute("UPDATE schema_migrations SET checksum='changed' WHERE version=17")
        with self.assertRaises(storage.StoreError) as raised:
            upgrade.apply(store)
        # Neither the frozen history nor a prefix of this release's catalog.
        self.assertEqual(raised.exception.code, 'schema_newer')
        self.assert_still_legacy(store)
        db.execute('DELETE FROM schema_migrations WHERE version=41')
        db.execute("UPDATE store_metadata SET value='40' WHERE key='schema_version'")
        with self.assertRaises(storage.StoreError):
            upgrade.apply(store)

    def test_any_difference_from_the_baseline_rolls_back_and_leaves_041(self):
        store = self.legacy_store()
        self.raw(store).execute('CREATE INDEX unexpected_index ON units(updated_at)')
        with self.assertRaises(storage.StoreError) as raised:
            upgrade.apply(store)
        self.assertEqual(raised.exception.code, 'schema_rebase_mismatch')
        self.assertIn('unexpected index unexpected_index', str(raised.exception))
        self.assert_still_legacy(store)

    def test_a_row_using_a_retired_gate_code_rolls_back(self):
        store = self.legacy_store()
        self.raw(store).execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('g3','u','unit','capacity',1)")
        with self.assertRaises(sqlite3.IntegrityError):
            upgrade.apply(store)
        self.assert_still_legacy(store)

    def test_normal_open_refuses_an_unrebased_store(self):
        store = self.legacy_store(seed=False)
        with self.assertRaises(storage.StoreError):
            storage.Store.open(store.root)


if __name__ == '__main__':
    unittest.main()
