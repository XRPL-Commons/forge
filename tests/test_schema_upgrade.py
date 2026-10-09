import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.store import db as storage
from forge.store import upgrade
from forge.store.migration.upgrade import Upgrade
from forge.store.migration.snapshot import MigrationError


class SchemaUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = storage.Store.create(self.root/'sqlite')
        self.original = storage._migrations()

    def following(self, script):
        return [*self.original, (len(self.original)+1, hashlib.sha256(script.encode()).hexdigest(), script)]

    def test_released_schema_upgrade_repairs_imports_and_indexes_requests(self):
        with patch('forge.store.db._migrations', return_value=self.original[:19]):
            previous = storage.Store.create(self.root/'previous')
            with previous.write() as db:
                db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'example/project')")
                db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,remote_updated_at,observed_at) VALUES('1:1',1,1,'open','r','2026-10-03T16:02:51Z',1)")
                db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES('r','1:1','Issue','','body','req','{}',1)")
        upgrade.apply(previous)
        upgrade.apply(previous)
        with previous.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT remote_updated_at FROM issues").fetchone()[0], '2026-10-03T16:02:51.000000+00:00')
            plan = db.execute("EXPLAIN QUERY PLAN SELECT details_json FROM operator_audit WHERE request_id=? AND json_extract(details_json,'$.kind')='request_outcome'", ('request',)).fetchall()
            self.assertTrue(any('operator_audit_request_kind' in row[3] for row in plan))

    def test_runtime_refuses_old_schema_until_explicit_atomic_upgrade(self):
        script = 'CREATE TABLE upgrade_probe(value INTEGER);\nINSERT INTO upgrade_probe VALUES(42);\n'
        with patch('forge.store.db._migrations', return_value=self.following(script)):
            with self.assertRaisesRegex(storage.StoreError, 'explicit schema migration'):
                storage.Store.open(self.store.root)
            self.assertEqual(upgrade.inspect(self.store)['pending_migrations'], [len(self.original)+1])
            upgrade.apply(self.store)
            upgrade.apply(self.store)  # Repeating after a lost response does not reapply it.
            with storage.Store.open(self.store.root).read_snapshot() as db:
                self.assertEqual(db.execute('SELECT value FROM upgrade_probe').fetchall()[0][0], 42)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], len(self.original)+1)

    def test_failed_migration_rolls_back_schema_data_and_version(self):
        script = 'CREATE TABLE upgrade_probe(value INTEGER);\nINSERT INTO missing_table VALUES(42);\n'
        with patch('forge.store.db._migrations', return_value=self.following(script)):
            with self.assertRaises(storage.sqlite3.DatabaseError):
                upgrade.apply(self.store)
        with storage.Store.open(self.store.root).read_snapshot() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='upgrade_probe'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], len(self.original))

    def test_changed_history_and_anchor_are_rejected_before_upgrade(self):
        changed = [(n, 'incorrect' if n == 1 else checksum, script) for n, checksum, script in self.original]
        with patch('forge.store.db._migrations', return_value=changed):
            with self.assertRaises(storage.StoreError) as result:
                upgrade.apply(self.store)
            self.assertEqual(result.exception.code, 'schema_checksum')
        self.store.instance_id = 'different-instance'
        with self.assertRaises(storage.StoreError) as result:
            upgrade.inspect(self.store)
        self.assertEqual(result.exception.code, 'instance_mismatch')


class UpgradeHostTests(SchemaUpgradeTests):
    def setUp(self):
        super().setUp()
        from types import SimpleNamespace
        (self.root/'state').mkdir(mode=0o700)
        storage._new_file(self.root/'state/storage', ('forge-sqlite-v1\n'+str(self.store.root)+'\n'+self.store.instance_id+'\n').encode())
        with self.store.write() as db:
            db.executemany('INSERT INTO store_metadata VALUES(?,?)', [('runtime_root',str(self.root)),('runtime_active','true')])
        self.current = 'a'*40
        self.active = ['forge-status.service','forge-ui.service','forge-supervisor.service']
        self.calls = []
        self.fail_update = False
        def command(*args):
            self.calls.append(args)
            if 'stop' in args:
                self.active = []
            if 'start' in args:
                self.active = list(args[3:])
            return ''
        def install(previous, target):
            self.calls.append(('install', previous, target))
            self.current = target
        def run(*args, **kwargs):
            self.calls.append(('update',))
            return SimpleNamespace(returncode=int(self.fail_update))
        self.host = SimpleNamespace(root=self.root, source=self.root/'source', identity=lambda:self.current,
            units=lambda:list(self.active), prepare_source_update=lambda *args:None, install_source_update=install,
            command=command, run=run, quiet=lambda:None)
        self.upgrader = Upgrade(self.host)
        self.addCleanup(patch.stopall)
        patch('forge.store.migration.upgrade.package_version', return_value='tool').start()

    def test_plan_is_read_only_and_apply_requires_exact_revision(self):
        self.assertEqual(self.upgrader.run(revision=None)['status'], 'planned')
        self.assertFalse(self.upgrader.path.exists())
        self.assertEqual(self.calls, [])
        with self.assertRaises(MigrationError):
            self.upgrader.run(revision='main', apply=True)
        self.assertEqual(self.calls, [])

    def test_lost_update_resumes_after_committed_schema_without_reapplying(self):
        script = 'CREATE TABLE upgrade_probe(value INTEGER);\nINSERT INTO upgrade_probe VALUES(42);\n'
        with patch('forge.store.db._migrations', return_value=self.following(script)):
            self.fail_update = True
            with self.assertRaisesRegex(MigrationError, 'upgrade_host_update_failed'):
                self.upgrader.run(revision='b'*40, apply=True)
            self.assertEqual(self.upgrader.read()['stage'], 'reconciling')
            self.assertEqual(self.active, [])
            with self.store.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT value FROM upgrade_probe').fetchone()[0], 42)
            self.fail_update = False
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'upgraded')
            self.assertEqual(len(self.active), 3)
            self.assertEqual(self.upgrader.read()['stage'], 'complete')
            self.assertEqual(len([x for x in self.calls if x[0]=='install']), 1)

    def test_worker_race_restores_services_before_source_or_schema_changes(self):
        with patch.object(self.upgrader, 'blockers', side_effect=[[], ['uncleaned_jobs'], ['uncleaned_jobs']]):
            result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'blocked')
        self.assertEqual(self.current, 'a'*40)
        self.assertEqual(len(self.active), 3)
        self.assertFalse(self.upgrader.path.exists())
        self.assertFalse(any(x[0] in {'update','install'} for x in self.calls))

    def test_explicit_new_release_repairs_failed_deployment(self):
        self.fail_update = True
        with self.assertRaises(MigrationError):
            self.upgrader.run(revision='b'*40, apply=True)
        self.fail_update = False
        result = self.upgrader.run(revision='c'*40, apply=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertEqual(self.current, 'c'*40)
        self.assertEqual(len(self.active), 3)

    def test_resume_revalidates_source_before_reconciling(self):
        self.fail_update = True
        with self.assertRaises(MigrationError):
            self.upgrader.run(revision='b'*40, apply=True)
        self.calls.clear()
        self.current = 'c'*40
        with self.assertRaisesRegex(MigrationError, 'upgrade_source_changed'):
            self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(self.calls, [])
