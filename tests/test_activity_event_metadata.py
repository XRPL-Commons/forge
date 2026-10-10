"""Activity reads bounded event metadata without hydrating discarded payloads."""
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import patch

from forge.factory.activity_reads import ActivityReads, EVENT_METADATA_SQL, event_metadata
from forge.factory.store_views import _event
from forge.store.queries import Queries
from tests import test_activity_reads as fixtures


class EventMetadataTests(unittest.TestCase):
    def test_current_and_imported_events_preserve_missing_null_and_json_value_types(self):
        with closing(sqlite3.connect(':memory:')) as db:
            db.row_factory = sqlite3.Row
            db.execute('CREATE TABLE events(kind TEXT,at REAL,payload_json TEXT)')
            fields = ('at', 'kind', 'provider', 'model')
            values = (None, False, True, 0, 1.5, 2**99, '', 'quote" slash\\ café',
                      [False, None, 'value'], {'value': True})
            for legacy in (False, True):
                for mask in range(16):
                    for value in values:
                        metadata = {key: value for index, key in enumerate(fields) if mask & (1 << index)}
                        payload = {'legacy': metadata} if legacy else metadata
                        payload = {**payload, 'discarded': [{'large': 'x'*12000}]}
                        kind = 'legacy_event' if legacy else 'issue_observed'
                        db.execute('DELETE FROM events')
                        db.execute('INSERT INTO events VALUES(?,?,?)', (kind, 3.5, json.dumps(payload)))
                        reference = _event(db.execute('SELECT * FROM events').fetchone(), fields)
                        row = db.execute('SELECT '+EVENT_METADATA_SQL+' FROM events e').fetchone()
                        with self.subTest(legacy=legacy, mask=mask, value=value):
                            actual = event_metadata(row)
                            self.assertEqual(actual, reference)
                            for field in actual:
                                self.assertIs(type(actual[field]), type(reference[field]))
                            self.assertNotIn('payload_json', row.keys())


class EventPageTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RealisticPageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        with self.fixture.store.write() as db:
            for number in range(1, 71):
                for kind, payload in (
                    ('issue_observed', {'at': -1, 'kind': 'ignored', 'provider': None, 'discarded': ['large']*1500}),
                    ('legacy_event', {'legacy': {'at': 1002, 'kind': 'observed', 'model': 'legacy'}, 'discarded': ['large']*1500}),
                    ('issue_observed', {'provider': 'codex', 'model': 'fixture', 'discarded': ['large']*1500}),
                ):
                    db.execute('INSERT INTO events(transaction_id,unit_id,kind,at,actor_id,payload_json) VALUES(?,?,?,?,?,?)',
                        ('fixture', 'u'+str(number), kind, 1001, 'test', json.dumps(payload)))

    def test_full_board_completed_and_detail_responses_keep_exact_history(self):
        for section in ('board', 'completed', 'runners'):
            fixtures.parity(self, self.fixture.views, section=section)
        for key in ('1', '70'):
            fixtures.parity(self, self.fixture.views, issue=key)
            # Explicit details keep the rich event reader and all other fields.
            expected = self.fixture.views.issues(key=key)
            with patch('forge.factory.activity_reads.ActivityReads', return_value=None):
                self.assertEqual(self.fixture.views.issues(key=key), expected)

    def test_only_latest_three_selected_unit_events_are_read_through_index(self):
        captured = []
        class Capture:
            def __init__(self, db): self.db = db
            def execute(self, sql, *args):
                if 'AS provider_json' in sql: captured.append((sql, args))
                return self.db.execute(sql, *args)
        with self.fixture.store.read_snapshot() as db:
            contexts = Queries._issue_contexts(db, ['70'])
            reads = ActivityReads(Capture(db), contexts)
            self.assertEqual(list(reads.events), ['u70'])
            self.assertEqual(len(reads.events['u70']), 3)
        self.assertEqual(len(captured), 1)
        sql, args = captured[0]
        self.assertEqual(json.loads(args[0][0]), ['u70'])
        def measured():
            steps = [0]
            with self.fixture.store.read_snapshot() as db:
                db.set_progress_handler(lambda: steps.__setitem__(0, steps[0]+1) or 0, 1)
                try: rows = [dict(row) for row in db.execute(sql, *args)]
                finally: db.set_progress_handler(None, 0)
                plan = [row['detail'] for row in db.execute('EXPLAIN QUERY PLAN '+sql, *args)]
            return rows, steps[0], plan
        before, first_steps, plan = measured()
        self.assertTrue(any('unit_events (unit_id=?)' in step for step in plan), plan)
        self.assertTrue(any('SEARCH e USING INTEGER PRIMARY KEY' in step for step in plan), plan)
        with self.fixture.store.write() as db:
            db.executemany('INSERT INTO events(transaction_id,unit_id,kind,at,payload_json) VALUES(?,?,?,?,?)',
                [('unrelated', 'u1', 'issue_observed', 1003, json.dumps({'discarded': 'x'*1000}))]*2000)
        after, next_steps, _ = measured()
        self.assertEqual(after, before)
        self.assertLessEqual(next_steps, first_steps+10, (first_steps, next_steps))


if __name__ == '__main__':
    unittest.main()
