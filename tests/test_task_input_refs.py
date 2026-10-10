"""Historical bundle closure uses immutable output identities and manifest versions."""
from contextlib import contextmanager
import json
import hashlib
import sqlite3
import unittest

from forge.store.executions import ExecutionQueries
from forge.store.models import TransitionError


class ReadStore:
    def __init__(self):
        self.db = sqlite3.connect(':memory:'); self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE part_outputs(id TEXT PRIMARY KEY, task_id TEXT, attempt_id TEXT, commit_oid TEXT, input_manifest_hash TEXT);
            CREATE TABLE jobs(id TEXT PRIMARY KEY, attempt_id TEXT);
            CREATE TABLE attempts(id TEXT PRIMARY KEY, input_manifest_json TEXT);
            CREATE TABLE job_members(job_id TEXT, member_id TEXT);
        ''')
        self.snapshots = 0

    @contextmanager
    def read_snapshot(self):
        self.snapshots += 1
        yield self.db


class TaskInputRefsTests(unittest.TestCase):
    def setUp(self):
        self.store = ReadStore(); self.addCleanup(self.store.db.close)
        self.queries = ExecutionQueries(self.store)

    def output(self, key, *, prerequisites=(), members=('a',), version=2):
        reference = {'output_id': key, 'task_id': 'task-' + key, 'attempt_id': 'attempt-' + key,
                     'commit_oid': hashlib.sha1(key.encode()).hexdigest(), 'input_manifest_hash': 'hash-' + key}
        manifest = {'prerequisite_outputs': list(prerequisites)}
        if version == 2: manifest['manifest_version'] = 2
        self.store.db.execute('INSERT INTO part_outputs VALUES(?,?,?,?,?)', tuple(reference.values()))
        self.store.db.execute('INSERT INTO jobs VALUES(?,?)', ('job-' + key, reference['attempt_id']))
        self.store.db.execute('INSERT INTO attempts VALUES(?,?)', (reference['attempt_id'], json.dumps(manifest)))
        self.store.db.executemany('INSERT INTO job_members VALUES(?,?)', [('job-' + key, member) for member in members])
        return reference

    def test_shared_multimember_diamond_is_topological_unique_and_one_snapshot(self):
        base = self.output('a', members=('a', 'b'))
        left = self.output('b', prerequisites=[base]); right = self.output('c', prerequisites=[base])
        refs = self.queries.input_refs({'role': 'build', 'input_manifest': {'manifest_version': 2, 'part_outputs': [left, right]}})
        self.assertEqual([row['output_id'] for row in refs], ['a', 'b', 'c'])
        self.assertEqual([row['job_id'] for row in refs], ['job-a', 'job-b', 'job-c'])
        self.assertEqual(self.store.snapshots, 1)
        self.assertNotIn('member_id', refs[0])

    def test_duplicate_output_reference_cannot_hide_task_or_attempt_substitution(self):
        reference = self.output('a')
        for field in ('task_id', 'attempt_id', 'commit_oid', 'input_manifest_hash'):
            with self.subTest(field=field), self.assertRaises(TransitionError):
                self.queries.input_refs({'role': 'build', 'input_manifest': {'manifest_version': 2,
                    'part_outputs': [reference, {**reference, field: 'substituted'}]}})

    def test_v1_capture_uses_original_single_member_scope_and_never_chooses_first(self):
        reference = self.output('a', version=1)
        old = {'output_id': 'a', 'member_id': 'a', 'commit_oid': reference['commit_oid'],
               'input_manifest_hash': reference['input_manifest_hash']}
        recovery = {'role': 'recovery', 'input_manifest': {}, 'failure': {'input_manifest': {'prerequisite_outputs': [old]}}}
        self.assertEqual(self.queries.input_refs(recovery)[0]['job_id'], 'job-a')
        self.store.db.execute('INSERT INTO job_members VALUES(?,?)', ('job-a', 'b'))
        with self.assertRaises(TransitionError):
            self.queries.input_refs(recovery)

    def test_retained_history_does_not_depend_on_python_recursion_limit(self):
        previous = None
        for index in range(1100):
            previous = self.output(str(index), prerequisites=[previous] if previous else [])
        refs = self.queries.input_refs({'role': 'build', 'input_manifest': {'manifest_version': 2,
            'prerequisite_outputs': [previous]}})
        self.assertEqual(len(refs), 1100)
        self.assertEqual((refs[0]['output_id'], refs[-1]['output_id']), ('0', '1099'))
