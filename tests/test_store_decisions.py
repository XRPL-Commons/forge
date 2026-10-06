"""A rejected request remains rejected even when its preconditions later change."""
from dataclasses import replace
import sqlite3
import tempfile
import unittest

from forge.store.db import Store, StoreError
from forge.store.models import Command, IdempotencyConflict, TransitionError, VersionConflict
from forge.store.transitions import Lifecycle


class DecisionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.life = Lifecycle(self.store, clock=lambda: 100)
        self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='unit',
                              issue_id='issue', repository_id=1, repository_name='test/repo',
                              issue_number=1, title='Change', body='Details', requirements={}, observed_at=90)

    def test_rejection_replays_original_error_after_preconditions_change(self):
        request = Command('resume', 'operator', 'unit', 1)
        with self.assertRaisesRegex(TransitionError, 'no operator pause'):
            self.life.resume(request)
        self.life.pause(Command('pause', 'operator', 'unit', 1))
        with self.assertRaisesRegex(TransitionError, 'no operator pause'):
            self.life.resume(request)
        for changed in (replace(request, actor_id='another'), replace(request, expected_version=2),
                        replace(request, unit_id='another')):
            with self.assertRaises(IdempotencyConflict):
                self.life.resume(changed)
        with self.assertRaises(IdempotencyConflict):
            self.life.pause(request)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT status FROM decisions WHERE request_id='resume'").fetchone()[0], 'rejected')
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE transaction_id='resume'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0], 2)
        self.life.resume(Command('new-resume', 'operator', 'unit', 2))

    def test_partial_mutation_and_events_are_rolled_back_before_rejection_is_saved(self):
        request = Command('reject', 'operator', 'unit', 1)
        def fail(db, unit):
            self.life._open_gate(db, request, 'environment', {})
            db.execute("UPDATE units SET fencing_generation=2 WHERE id='unit'")
            raise TransitionError('late rejection')
        with self.assertRaisesRegex(TransitionError, 'late rejection'):
            self.life._apply(request, 'test', {'value': 1}, fail)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM unit_gates').fetchone()[0], 0)
            self.assertEqual(tuple(db.execute("SELECT version,fencing_generation FROM units WHERE id='unit'").fetchone()), (1, 1))
            self.assertEqual([r[0] for r in db.execute("SELECT kind FROM events WHERE transaction_id='reject'")], ['decision_rejected'])
        with self.assertRaises(IdempotencyConflict):
            self.life._apply(request, 'test', {'value': 2}, fail)

    def test_failed_admission_retains_request_without_partial_repository_or_issue(self):
        arguments = dict(request_id='bad-admit', actor_id='operator', unit_id='new-unit',
                         issue_id='issue', repository_id=2, repository_name='test/new', issue_number=2,
                         title='Duplicate identity', body='', requirements={}, observed_at=90)
        for _ in range(2):
            with self.assertRaises(sqlite3.IntegrityError):
                self.life.admit_issue(**arguments)
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT id FROM repositories WHERE id=2').fetchone())
            self.assertIsNone(db.execute("SELECT id FROM units WHERE id='new-unit'").fetchone())
            self.assertIsNone(db.execute("SELECT unit_id FROM decisions WHERE request_id='bad-admit'").fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE transaction_id='bad-admit'").fetchone()[0], 1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_version_conflict_is_saved_but_storage_failure_is_retryable(self):
        request = Command('stale', 'operator', 'unit', 99)
        for _ in range(2):
            with self.assertRaises(VersionConflict):
                self.life.pause(request)
        retry = Command('storage', 'operator', 'unit', 1)
        def fail(db, unit):
            raise sqlite3.OperationalError('disk I/O error')
        with self.assertRaises(StoreError):
            self.life._apply(retry, 'test', {}, fail)
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute("SELECT id FROM decisions WHERE request_id='storage'").fetchone())
        self.life._apply(retry, 'test', {}, lambda db, unit: {})
