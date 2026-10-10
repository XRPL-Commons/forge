"""HTTP idle handles preserve request and snapshot ownership across threads."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import sqlite3
import threading
import unittest
from unittest.mock import patch

from forge.store.db import Store, StoreError
from forge.store.read_handles import IdleReadHandle
from tests import test_store_read_lease as fixture


class IdleReadsTests(unittest.TestCase):
    setUp = fixture.ReadConnectionLeaseTests.setUp
    put = fixture.ReadConnectionLeaseTests.put
    value = fixture.ReadConnectionLeaseTests.value
    def assert_closed(self, db):
        # Bypass request ownership checks: prove the physical handle was closed.
        with self.assertRaises(sqlite3.ProgrammingError):
            sqlite3.Connection.execute(db, 'SELECT 1')

    def pool(self):
        pool = IdleReadHandle(); self.addCleanup(pool.close); return pool

    def read(self, pool):
        with self.store.read_connection_lease(pool):
            with self.store.read_snapshot() as db:
                return db, self.value(db)

    def test_sequential_different_threads_reuse_handle_with_fresh_snapshot(self):
        pool = self.pool()
        with ThreadPoolExecutor(max_workers=1) as worker:
            first, value = worker.submit(self.read, pool).result()
        self.assertEqual(value, 'before'); self.assertFalse(first.in_transaction)
        self.put('after')
        second, value = self.read(pool)
        self.assertIs(first, second); self.assertEqual(value, 'after')
        with self.assertRaises(sqlite3.ProgrammingError): first.execute('SELECT 1')
        pool.close(); self.assert_closed(first)

    def test_concurrent_requests_are_exclusive_and_only_one_idle_survives(self):
        pool = self.pool(); barrier = threading.Barrier(2)
        def read():
            with self.store.read_connection_lease(pool):
                with self.store.read_snapshot() as db:
                    barrier.wait(timeout=5)
                    return db, self.value(db)
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures = [workers.submit(read) for _ in range(2)]
            pairs = [future.result() for future in futures]
        self.assertIsNot(pairs[0][0], pairs[1][0])
        self.assertEqual([p[1] for p in pairs], ['before', 'before'])
        closed = [p[0] for p in pairs if p[0] is not pool.idle['connection']]
        self.assertEqual(len(closed), 1); self.assert_closed(closed[0])

    def test_partial_cursor_cannot_escape_snapshot_or_pin_old_results(self):
        pool = self.pool()
        with self.store.read_connection_lease(pool):
            with self.store.read_snapshot() as db:
                cursor = db.execute('SELECT key,value FROM store_metadata')
                self.assertIsNotNone(cursor.fetchone())
            with self.assertRaises(sqlite3.ProgrammingError): cursor.fetchone()
            self.put('after')
            with self.store.read_snapshot() as again:
                self.assertIs(db, again); self.assertEqual(self.value(again), 'after')
                with self.assertRaises(sqlite3.ProgrammingError): next(cursor)
        # No old cursor or read transaction prevents a normal checkpoint.
        with self.store.write() as writer: pass
        with self.store._connection() as writer:
            self.assertEqual(writer.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0], 0)

    def test_inner_exception_evicts_even_if_handler_catches_it(self):
        pool = self.pool()
        with self.store.read_connection_lease(pool):
            with self.assertRaises(ValueError):
                with self.store.read_snapshot() as db: raise ValueError('projection')
        self.assertIsNone(pool.idle); self.assert_closed(db)

    def test_reused_handle_revalidates_schema_and_settings(self):
        pool = self.pool(); first, _ = self.read(pool)
        with self.store.read_connection_lease(pool):
            with self.store._connection(read_only=True) as db:
                db.execute('PRAGMA trusted_schema=ON')
            with self.store.read_snapshot() as db:
                self.assertEqual(db.execute('PRAGMA trusted_schema').fetchone()[0], 0)
        with self.store.write() as writer:
            writer.execute("UPDATE store_metadata SET value='0' WHERE key='schema_version'")
        with self.assertRaises(StoreError) as error: self.read(pool)
        self.assertEqual(error.exception.code, 'schema_incomplete')
        self.assertIsNone(pool.idle); self.assert_closed(first)

    def test_selection_and_shutdown_reject_late_release(self):
        pool = self.pool()
        with self.store.read_connection_lease(pool):
            with self.store.read_snapshot() as old: pass
            other = Store.open(self.store.root)
            with other.read_connection_lease(pool):
                with other.read_snapshot() as second: pass
            # Returning to the first selection must not revive its older lease.
            with self.store.read_connection_lease(pool):
                with self.store.read_snapshot() as third: pass
        self.assert_closed(old); self.assert_closed(second)
        self.assertIs(pool.idle['connection'], third)
        with self.store.read_connection_lease(pool):
            with self.store.read_snapshot() as active: pass
            pool.close()
        self.assert_closed(active); self.assertIsNone(pool.idle)

    def test_copied_context_does_not_transfer_active_request_ownership(self):
        pool = self.pool()
        with self.store.read_connection_lease(pool):
            with self.store.read_snapshot() as first: pass
            context = copy_context()
            def read():
                with self.store.read_snapshot() as other:
                    self.assertIsNot(first, other)
                self.assert_closed(other)
            with ThreadPoolExecutor(max_workers=1) as workers: workers.submit(context.run, read).result()

    def test_file_replacement_discards_idle_inode_before_fresh_validation(self):
        import os
        pool = self.pool(); original, _ = self.read(pool)
        replacement = self.store.path.with_suffix('.replacement')
        target = sqlite3.connect(replacement)
        try:
            with self.store.read_snapshot() as source: source.backup(target)
        finally: target.close()
        replacement.chmod(0o600)
        os.replace(replacement, self.store.path)
        fresh, value = self.read(pool)
        self.assertIsNot(original, fresh); self.assertEqual(value, 'before')
        with self.assertRaises(sqlite3.ProgrammingError): sqlite3.Connection.execute(original, 'SELECT 1')

    def test_anchor_failure_closes_idle_handle(self):
        import json
        pool = self.pool(); original, _ = self.read(pool)
        self.store.anchor_path.write_text(json.dumps({'format': 1, 'instance_id': 'changed'}))
        with self.assertRaises(StoreError) as error: self.read(pool)
        self.assertEqual(error.exception.code, 'instance_mismatch')
        self.assertIsNone(pool.idle)
        with self.assertRaises(sqlite3.ProgrammingError): sqlite3.Connection.execute(original, 'SELECT 1')

    def test_path_validation_failure_closes_handle_even_when_caught_by_handler(self):
        pool = self.pool(); original, _ = self.read(pool)
        self.store.path.chmod(0o644)
        try:
            with self.store.read_connection_lease(pool):
                with self.assertRaises(StoreError) as error:
                    with self.store.read_snapshot(): pass
                self.assertEqual(error.exception.code, 'unsafe_permissions')
        finally: self.store.path.chmod(0o600)
        self.assertIsNone(pool.idle)
        with self.assertRaises(sqlite3.ProgrammingError): sqlite3.Connection.execute(original, 'SELECT 1')

    def test_transfer_requires_serialized_sqlite(self):
        with patch('forge.store.read_handles.sqlite3.threadsafety', 1):
            with self.assertRaisesRegex(RuntimeError, 'serialized'): IdleReadHandle()

from types import SimpleNamespace
from tests import test_operator_read_snapshot as http_fixture


class IdleHttpTests(unittest.TestCase):
    def setUp(self):
        http_fixture.OperatorReadSnapshotTests.setUp(self)
        self.handles = IdleReadHandle(); self.addCleanup(self.handles.close)

    def handler(self, *args, **kwargs):
        result = http_fixture.OperatorReadSnapshotTests.handler(self, *args, **kwargs)
        result.server = SimpleNamespace(read_handles=self.handles)
        return result

    test_cached_response_reauthenticates_after_revocation = http_fixture.OperatorReadSnapshotTests.test_cached_response_reauthenticates_after_revocation
    test_wait_holds_no_auth_snapshot_and_rechecks_revocation = http_fixture.OperatorReadSnapshotTests.test_wait_holds_no_auth_snapshot_and_rechecks_revocation

    def test_handler_disconnect_discards_handle(self):
        from unittest.mock import Mock
        with patch('forge.factory.control.factory_view', return_value={'issues': []}):
            handler = self.handler(); handler.wfile = Mock()
            handler.wfile.write.side_effect = BrokenPipeError('disconnected')
            with self.assertRaises(BrokenPipeError): handler.do_GET()
        self.assertIsNone(self.handles.idle)

    def test_committed_update_invalidates_cache_on_reused_connection(self):
        import json
        with self.writer.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('projection_test','before')")
        def build(*args, **kwargs):
            with self.store.read_session() as db:
                return {'value': db.execute("SELECT value FROM store_metadata WHERE key='projection_test'").fetchone()[0]}
        with patch('forge.factory.control.factory_view', side_effect=build):
            first = self.handler(); first.do_GET()
            connection = self.handles.idle['connection']
            tag = next(c.args[1] for c in first.send_header.call_args_list if c.args[0] == 'ETag')
            same = self.handler(tag=tag); same.do_GET()
            same.send_response.assert_called_once_with(304)
            with self.writer.write() as db:
                db.execute("UPDATE store_metadata SET value='after' WHERE key='projection_test'")
            fresh = self.handler(tag=tag); fresh.do_GET()
            self.assertIs(connection, self.handles.idle['connection'])
            fresh.send_response.assert_called_once_with(200)
            self.assertEqual(json.loads(fresh.wfile.getvalue())['value'], 'after')
