"""Physical request connections never become shared transactions or state caches."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from forge.store.db import Store, StoreError


class ReadConnectionLeaseTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store.create(Path(temporary.name))
        self.put('before')

    def put(self,value):
        with self.store.write() as db:
            db.execute("INSERT OR REPLACE INTO store_metadata VALUES('lease_test',?)",(value,))

    def value(self,db):
        return db.execute("SELECT value FROM store_metadata WHERE key='lease_test'").fetchone()[0]

    def assert_closed(self,db):
        with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')

    def test_separate_snapshots_see_intervening_write_and_revalidate(self):
        with patch.object(self.store,'_connect_path',wraps=self.store._connect_path) as connect, \
                patch.object(self.store,'_validate',wraps=self.store._validate) as validate:
            with self.store.read_connection_lease():
                with self.store.read_session() as first:self.assertEqual(self.value(first),'before')
                self.assertFalse(first.in_transaction)
                self.put('after')  # Same Store, independent write connection.
                with self.store.read_session() as second:
                    self.assertIs(first,second);self.assertEqual(self.value(second),'after')
                self.assertEqual(connect.call_count,2)
                self.assertEqual(validate.call_count,3)
            self.assert_closed(first)
            with self.store.read_snapshot() as fresh:self.assertIsNot(first,fresh)

    def test_reentrant_snapshot_keeps_its_own_transaction(self):
        with self.store.read_connection_lease():
            with self.store.read_snapshot() as first:
                self.assertEqual(self.value(first),'before')
                self.put('after')
                with self.store.read_snapshot() as nested:
                    self.assertIsNot(first,nested);self.assertEqual(self.value(nested),'after')
                self.assert_closed(nested)
                self.assertEqual(self.value(first),'before')
            with self.store.read_snapshot() as next_read:
                self.assertIs(first,next_read);self.assertEqual(self.value(next_read),'after')

    def test_copied_context_in_other_thread_does_not_borrow_connection(self):
        with self.store.read_connection_lease():
            with self.store.read_snapshot() as first:pass
            context=copy_context()
            def read():
                with self.store.read_snapshot() as other:
                    self.assertIsNot(first,other);self.assertEqual(self.value(other),'before')
                self.assert_closed(other)
            with ThreadPoolExecutor(max_workers=1) as pool:pool.submit(context.run,read).result()
            with self.store.read_snapshot() as again:self.assertIs(first,again)
        self.assert_closed(first)

    def test_exception_rolls_back_before_reuse_and_closes_at_scope_exit(self):
        with self.assertRaisesRegex(ValueError,'outer'):
            with self.store.read_connection_lease():
                with self.assertRaisesRegex(ValueError,'inner'):
                    with self.store._connection(read_only=True) as db:
                        db.execute('BEGIN DEFERRED');raise ValueError('inner')
                self.assertFalse(db.in_transaction)
                with self.store.read_snapshot() as again:self.assertIs(db,again)
                raise ValueError('outer')
        self.assert_closed(db);self.assertIsNone(self.store._read_lease.get())

    def test_live_schema_drift_is_rejected_on_reused_connection(self):
        with self.store.read_connection_lease():
            with self.store.read_snapshot() as first:pass
            with self.store.write() as writer:
                writer.execute("UPDATE store_metadata SET value='0' WHERE key='schema_version'")
            with self.assertRaises(StoreError) as caught:
                with self.store.read_snapshot():pass
            self.assertEqual(caught.exception.code,'schema_incomplete')
        self.assert_closed(first)

    def test_live_anchor_drift_is_rejected_on_reused_connection(self):
        with self.store.read_connection_lease():
            with self.store.read_snapshot() as first:pass
            self.store.anchor_path.write_text(json.dumps({'format':1,'instance_id':'changed'}))
            with self.assertRaises(StoreError) as caught:
                with self.store.read_snapshot():pass
            self.assertEqual(caught.exception.code,'instance_mismatch')
        self.assert_closed(first)

    def test_nested_lease_owns_and_closes_its_connection(self):
        with self.store.read_connection_lease():
            with self.store.read_snapshot() as outer:pass
            with self.store.read_connection_lease():
                with self.store.read_snapshot() as inner:self.assertIsNot(outer,inner)
            self.assert_closed(inner)
            with self.store.read_snapshot() as again:self.assertIs(outer,again)
        self.assert_closed(outer)

    def test_connection_safety_settings_are_reestablished_on_checkout(self):
        with self.store.read_connection_lease():
            with self.store._connection(read_only=True) as db:
                db.execute('PRAGMA foreign_keys=OFF')
                db.execute('PRAGMA trusted_schema=ON')
                db.execute('PRAGMA synchronous=NORMAL')
            with self.store.read_snapshot() as reused:
                self.assertIs(db,reused)
                self.assertEqual(reused.execute('PRAGMA foreign_keys').fetchone()[0],1)
                self.assertEqual(reused.execute('PRAGMA trusted_schema').fetchone()[0],0)
                self.assertEqual(reused.execute('PRAGMA synchronous').fetchone()[0],2)
                self.assertEqual(reused.execute('PRAGMA query_only').fetchone()[0],1)

    def test_replaced_file_cannot_reuse_previous_database_handle(self):
        with self.store.read_connection_lease():
            with self.store.read_snapshot() as first:pass
            replacement=self.store.path.with_suffix('.replacement')
            # SQLite's backup produces a standalone same-instance database.
            target=sqlite3.connect(replacement)
            try:first.backup(target)
            finally:target.close()
            replacement.chmod(0o600)
            os.replace(replacement,self.store.path)
            with self.assertRaises(StoreError) as caught:
                with self.store.read_snapshot():pass
            self.assertEqual(caught.exception.code,'store_changed')
        self.assert_closed(first)
