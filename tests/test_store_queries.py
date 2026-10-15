from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import unittest

from forge.store.queries import ArtifactCorrupt, Queries


class MemoryStore:
    def __init__(self):
        self.connection = sqlite3.connect(':memory:')
        self.connection.row_factory = sqlite3.Row
        from forge.store.remote_time import register
        register(self.connection)
        directory = Path(__file__).resolve().parents[1] / 'forge/store/schema'
        for schema in sorted(directory.glob('*.sql')):
            self.connection.executescript(schema.read_text())
        self.statements = []
        self.connection.set_trace_callback(self.statements.append)

    @contextmanager
    def read_snapshot(self):
        self.connection.execute('PRAGMA query_only=ON')
        self.connection.execute('BEGIN')
        try:
            yield self.connection
        finally:
            self.connection.rollback()
            self.connection.execute('PRAGMA query_only=OFF')


class StoreQueryTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.addCleanup(self.store.connection.close)
        self.queries = Queries(self.store)
        db = self.store.connection
        db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'example/repo')")
        for number in range(1, 5):
            unit = f'u{number}'
            db.execute(
                'INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at,close_code) '
                "VALUES(?,1,'main',?,1,1,?)",
                (unit, 'closed' if number == 4 else 'building', 'operator_closed' if number == 4 else None),
            )
            db.execute(
                'INSERT INTO events(transaction_id,unit_id,kind,at,payload_json) '
                "VALUES(?,?,'created',1,'{}')", (f'tx{number}', unit),
            )
        db.execute(
            'INSERT INTO attempts(id,unit_id,membership_generation,assignment_generation,'
            'input_manifest_json,budget_generation,created_at) '
            "VALUES('a1','u1',1,1,'{}',1,1)"
        )
        db.execute(
            'INSERT INTO jobs(id,unit_id,attempt_id,role,phase,created_at) '
            "VALUES('j1','u1','a1','integration','reserved',1)"
        )
        for sequence, content in enumerate((b'first output', b'second output')):
            db.execute(
                'INSERT INTO job_artifacts(job_id,kind,sequence,media_type,digest,content,size) '
                "VALUES('j1','output',?,'text/plain',?,?,?)",
                (sequence, hashlib.sha256(content).hexdigest(), content, len(content)),
            )
        db.commit()
        self.store.statements.clear()

    def test_units_keyset_and_terminal_filter(self):
        first = self.queries.units(limit=2)
        self.assertEqual([x['id'] for x in first['items']], ['u1', 'u2'])
        self.assertEqual(first['next_cursor'], 'u2')
        second = self.queries.units(after=first['next_cursor'], limit=2)
        self.assertEqual([x['id'] for x in second['items']], ['u3'])
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(len(self.queries.units(include_terminal=True)['items']), 4)
        self.assertFalse(any('job_artifacts' in sql for sql in self.store.statements))

    def test_detail_and_list_use_same_projection(self):
        self.assertEqual(self.queries.unit('u1'), self.queries.units()['items'][0])

    def test_events_have_stable_sequence_cursor_and_filter(self):
        page = self.queries.events(limit=2)
        self.assertEqual([x['sequence'] for x in page['items']], [1, 2])
        self.assertEqual(page['next_cursor'], 2)
        self.assertEqual([x['sequence'] for x in self.queries.events(after=2)['items']], [3, 4])
        self.assertEqual(self.queries.events(unit_id='u3')['items'][0]['payload'], {})
        self.assertEqual(len(self.queries.events(unit_id='u3')['items']), 1)

    def test_output_is_lazy_paged_and_checked(self):
        first = self.queries.artifacts('j1', limit=1)
        self.assertEqual(first['next_cursor'], ['output', 0])
        second = self.queries.artifacts('j1', after=first['next_cursor'], limit=1)
        self.assertEqual(second['items'][0]['sequence'], 1)
        self.assertIsNone(second['next_cursor'])
        self.assertFalse(any('SELECT content' in sql for sql in self.store.statements))
        self.assertEqual(self.queries.artifact('j1', 'output', 1).content, b'second output')
        with self.assertRaises(KeyError):
            self.queries.artifact('j1', 'output', 99)

    def test_corrupt_artifact_is_not_returned(self):
        self.store.connection.execute(
            'INSERT INTO job_artifacts(job_id,kind,sequence,media_type,digest,content,size) '
            "VALUES('j1','result',0,'text/plain',?,X'00',1)", ('0' * 64,),
        )
        self.store.connection.commit()
        with self.assertRaises(ArtifactCorrupt):
            self.queries.artifact('j1', 'result')

    def test_limits_and_cursors_are_validated(self):
        for limit in (0, -1, 101, True, 1.5, '10'):
            for method in (self.queries.units, self.queries.events):
                with self.subTest(limit=limit, method=method.__name__):
                    with self.assertRaises(ValueError):
                        method(limit=limit)
        for after in (-1, True, '1'):
            with self.assertRaises(ValueError):
                self.queries.events(after=after)
        for after in (['output'], ['output', -1], [1, 0]):
            with self.assertRaises(ValueError):
                self.queries.artifacts('j1', after=after)


if __name__ == '__main__':
    unittest.main()
