"""Persistence and isolation checks for the inactive SQLite store."""

from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.store.db import Store, StoreError, qualified_sqlite


class StoreDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store.create(self.root, instance_id="test-instance")

    def assert_code(self, code, function, *arguments):
        with self.assertRaises(StoreError) as caught:
            function(*arguments)
        self.assertEqual(caught.exception.code, code)

    def put(self, value):
        with self.store.write() as connection:
            connection.execute("INSERT OR REPLACE INTO store_metadata VALUES ('test', ?)", (value,))

    def value(self):
        with self.store.read_snapshot() as connection:
            row = connection.execute("SELECT value FROM store_metadata WHERE key='test'").fetchone()
            return None if row is None else row[0]

    def test_explicit_initialization_reopens_without_persistent_handle(self):
        reopened = Store.open(self.root)
        self.assertEqual(reopened.instance_id, "test-instance")
        self.assert_code("store_exists", Store.create, self.root)
        with reopened.read_snapshot() as connection:
            self.assertIsInstance(connection.execute("SELECT 1").fetchone(), sqlite3.Row)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(connection.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA trusted_schema").fetchone()[0], 0)
            self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_catalog_is_loaded_once_per_handle_and_fresh_handles_load_installed_release(self):
        from forge.store import db as storage
        catalog = storage._migrations()
        with patch('forge.store.db._migrations', return_value=catalog) as installed:
            handle = Store.open(self.root)
            with handle.read_snapshot():
                pass
            with handle.read_snapshot():
                pass
            installed.assert_called_once_with()
            # The caller's list is not retained as mutable schema authority.
            catalog.append((len(catalog) + 1, 'next-release', '-- next release'))
            with handle.read_snapshot():
                pass
            self.assert_code('schema_incomplete', Store.open, self.root)
            self.assertEqual(installed.call_count, 2)

    def test_validation_reads_only_schema_and_instance_metadata(self):
        with self.store.write() as connection:
            connection.executemany("INSERT INTO store_metadata VALUES (?, ?)",
                                   [(f'sweep-receipt-{index}', 'x' * 4096) for index in range(100)])
        with self.store.read_snapshot() as connection:
            metadata = []
            def execute(query):
                rows = connection.execute(query)
                if 'FROM store_metadata' in query:
                    rows = list(rows)
                    metadata.extend(rows)
                return rows
            self.store._validate(Mock(execute=execute))
            self.assertEqual({row['key'] for row in metadata}, {'schema_version', 'instance_id'})

    def test_commit_and_exception_rollback(self):
        self.put("before")
        with self.assertRaisesRegex(RuntimeError, "abort"):
            with self.store.write() as connection:
                connection.execute("UPDATE store_metadata SET value='after' WHERE key='test'")
                raise RuntimeError("abort")
        self.assertEqual(self.value(), "before")
        self.put("committed")
        self.assertEqual(Store.open(self.root).instance_id, "test-instance")
        self.assertEqual(self.value(), "committed")

    def test_reader_snapshot_survives_concurrent_commit(self):
        self.put("before")
        with self.store.read_snapshot() as reader:
            self.put("after")
            self.assertEqual(reader.execute("SELECT value FROM store_metadata WHERE key='test'").fetchone()[0], "before")
        self.assertEqual(self.value(), "after")

    def test_read_session_reuses_snapshot_until_outer_projection_finishes(self):
        self.put("before")
        writer = Store.open(self.root)
        with patch.object(self.store, '_connect_path', wraps=self.store._connect_path) as connect:
            with self.store.read_session() as first:
                with writer.write() as db:
                    db.execute("UPDATE store_metadata SET value='after' WHERE key='test'")
                with self.store.read_session() as second, self.store.read_snapshot() as third:
                    self.assertIs(first, second)
                    self.assertIs(first, third)
                    self.assertEqual(self.value(), "before")
                self.assert_code("read_session_write", self.put, "forbidden")
            self.assertEqual(connect.call_count, 1)
        self.assertEqual(self.value(), "after")
        with self.assertRaises(sqlite3.ProgrammingError):
            first.execute("SELECT 1")

    def test_read_session_exception_closes_connection_and_does_not_leak(self):
        with self.assertRaisesRegex(ValueError, 'abort'):
            with self.store.read_session() as reader:
                raise ValueError('abort')
        with self.assertRaises(sqlite3.ProgrammingError):
            reader.execute('SELECT 1')
        self.put('after')
        self.assertEqual(self.value(), 'after')

    def test_read_only_connection_cannot_be_made_writable(self):
        with self.store.read_snapshot() as reader:
            reader.execute("PRAGMA query_only=OFF")
            with self.assertRaises(sqlite3.OperationalError):
                reader.execute("INSERT INTO store_metadata VALUES ('test', 'forbidden')")
        self.assertIsNone(self.value())

    def test_competing_writer_returns_retryable_coded_error(self):
        self.store.busy_timeout_ms = 20
        with self.store.write():
            with self.assertRaises(StoreError) as caught:
                self.put("blocked")
            self.assertEqual(caught.exception.code, "store_busy")
        self.assertIsNone(self.value())

    def test_full_database_rolls_back_all_transaction_changes(self):
        self.put("before")
        with self.assertRaises(StoreError) as caught:
            with self.store.write() as connection:
                pages = connection.execute("PRAGMA page_count").fetchone()[0]
                connection.execute(f"PRAGMA max_page_count={pages}")
                connection.execute("UPDATE store_metadata SET value='intermediate' WHERE key='test'")
                connection.executemany("INSERT INTO store_metadata VALUES (?, ?)",
                                       [(f"overflow-{index}", "x" * 60_000) for index in range(100)])
        self.assertEqual(caught.exception.code, "store_full")
        self.assertEqual(self.value(), "before")
        self.put("after")
        self.assertEqual(self.value(), "after")

    def test_process_death_rolls_back_uncommitted_transaction(self):
        self.put("before")
        script = """
import os, sys
from forge.store.db import Store
with Store.open(sys.argv[1]).write() as db:
    db.execute("UPDATE store_metadata SET value='lost' WHERE key='test'")
    os._exit(23)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(self.value(), "before")
        self.put("recovered")
        self.assertEqual(self.value(), "recovered")

    def test_process_death_after_commit_preserves_transaction(self):
        script = """
import os, sys
from forge.store.db import Store
with Store.open(sys.argv[1]).write() as db:
    db.execute("INSERT INTO store_metadata VALUES ('test', 'durable')")
os._exit(24)
"""
        result = subprocess.run([sys.executable, "-c", script, str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 24, result.stderr)
        self.assertEqual(self.value(), "durable")

    def test_backup_includes_committed_wal_and_excludes_uncommitted_write(self):
        self.put("committed")
        destination = self.root / "backup.sqlite3"
        with self.store.write() as writer:
            writer.execute("UPDATE store_metadata SET value='not-committed' WHERE key='test'")
            self.assertEqual(self.store.backup(destination), destination)
        with closing(sqlite3.connect(destination)) as backup:
            self.assertEqual(backup.execute("SELECT value FROM store_metadata WHERE key='test'").fetchone()[0], "committed")
            self.assertEqual(backup.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assert_code("backup_exists", self.store.backup, destination)
        self.assertEqual(list(self.root.glob("*.pending-*")), [])

    def test_failed_backup_does_not_publish_destination(self):
        destination = self.root / "backup.sqlite3"
        with patch.object(self.store, "_validate", side_effect=StoreError("test_failure", "injected")):
            self.assert_code("test_failure", self.store.backup, destination)
        self.assertFalse(destination.exists())
        self.assertEqual(list(self.root.glob("*.pending-*")), [])

    def test_backup_refuses_unsafe_parent_and_existing_sidecars(self):
        folder = self.root / "public"
        folder.mkdir(mode=0o755)
        folder.chmod(0o755)
        self.assert_code("unsafe_permissions", self.store.backup, folder / "backup")
        destination = self.root / "backup"
        Path(str(destination) + "-wal").symlink_to(self.store.path)
        self.assert_code("backup_exists", self.store.backup, destination)

    def test_files_and_live_sidecars_are_private(self):
        self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)
        with self.store.write() as connection:
            connection.execute("INSERT INTO store_metadata VALUES ('test', 'private')")
            for path in self.store.directory.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)

    def test_database_permissions_are_validated_on_every_connection(self):
        os.chmod(self.store.path, 0o644)
        self.assert_code("unsafe_permissions", Store.open, self.root)
        self.assert_code("unsafe_permissions", self.value)

    def test_private_root_permissions_are_required(self):
        os.chmod(self.root, 0o755)
        self.assert_code("unsafe_permissions", Store.open, self.root)

    def test_database_symlink_is_rejected(self):
        original = self.root / "actual-db"
        self.store.path.rename(original)
        self.store.path.symlink_to(original)
        self.assert_code("unsafe_permissions", Store.open, self.root)

    def test_database_hardlink_is_rejected(self):
        os.link(self.store.path, self.root / "unexpected-alias")
        self.assert_code("unsafe_permissions", Store.open, self.root)

    def test_sidecar_symlink_is_rejected_before_opening_database(self):
        Path(str(self.store.path) + "-wal").symlink_to(self.store.anchor_path)
        self.assert_code("unsafe_permissions", Store.open, self.root)

    def test_lost_database_does_not_create_empty_replacement(self):
        self.store.path.unlink()
        self.assert_code("store_missing", Store.open, self.root)
        self.assert_code("store_exists", Store.create, self.root)
        self.assertFalse(self.store.path.exists())

    def test_lost_anchor_never_recreates_identity(self):
        self.store.anchor_path.unlink()
        self.assert_code("store_missing", Store.open, self.root)
        self.assert_code("store_exists", Store.create, self.root)

    def test_mismatched_anchor_refuses_database(self):
        self.store.anchor_path.write_text(json.dumps({"format": 1, "instance_id": "another"}))
        self.assert_code("instance_mismatch", Store.open, self.root)
        self.assert_code("instance_mismatch", self.value)

    def test_corrupt_database_is_not_reset(self):
        self.store.path.write_bytes(b"not a SQLite database")
        self.assert_code("store_invalid", Store.open, self.root)
        self.assertEqual(self.store.path.read_bytes(), b"not a SQLite database")

    def test_missing_schema_is_rejected(self):
        with self.store.write() as connection:
            connection.execute("DELETE FROM schema_migrations")
        self.assert_code("schema_incomplete", Store.open, self.root)
        self.assert_code("schema_incomplete", self.value)

    def test_schema_metadata_drift_is_rejected_by_existing_handle(self):
        with self.store.write() as connection:
            connection.execute("UPDATE store_metadata SET value='0' WHERE key='schema_version'")
        self.assert_code('schema_incomplete', self.value)

    def test_modified_migration_checksum_is_rejected(self):
        with self.store.write() as connection:
            connection.execute("UPDATE schema_migrations SET checksum='changed'")
        self.assert_code("schema_checksum", Store.open, self.root)
        self.assert_code("schema_checksum", self.value)

    def test_newer_schema_is_rejected(self):
        with self.store.write() as connection:
            connection.execute("INSERT INTO schema_migrations VALUES (999, 'future', 0)")
        self.assert_code("schema_newer", Store.open, self.root)
        self.assert_code("schema_newer", self.value)

    def test_init_crash_before_anchor_refuses_partial_pair(self):
        with tempfile.TemporaryDirectory() as root:
            script = """
import os, sys
from forge.store import db
original = db._new_file
def interrupted(path, content=b''):
    if path.name == '.instance.pending':
        os._exit(25)
    return original(path, content)
db._new_file = interrupted
db.Store.create(sys.argv[1])
"""
            result = subprocess.run([sys.executable, "-c", script, root], capture_output=True, text=True)
            self.assertEqual(result.returncode, 25, result.stderr)
            self.assertTrue((Path(root) / "state/forge.sqlite3").exists())
            self.assert_code("store_missing", Store.open, root)
            self.assert_code("store_exists", Store.create, root)

    def test_unqualified_sqlite_is_refused_before_creating_files(self):
        with tempfile.TemporaryDirectory() as root:
            with patch("forge.store.db.sqlite3.sqlite_version_info", (3, 45, 1)):
                self.assert_code("sqlite_unqualified", Store.create, root)
                self.assert_code("sqlite_unqualified", Store.open, self.root)
            self.assertEqual(list(Path(root).iterdir()), [])

    def test_qualified_upstream_version_boundaries(self):
        for version in [(3, 44, 6), (3, 44, 7), (3, 50, 7), (3, 51, 3), (3, 53, 1)]:
            self.assertTrue(qualified_sqlite(version), version)
        for version in [(3, 44, 5), (3, 45, 1), (3, 50, 6), (3, 51, 2)]:
            self.assertFalse(qualified_sqlite(version), version)


if __name__ == "__main__":
    unittest.main()
