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
        previous = storage.Store(previous.root)  # Target release handle.
        upgrade.apply(previous)
        upgrade.apply(previous)
        with previous.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT remote_updated_at FROM issues").fetchone()[0], '2026-10-03T16:02:51.000000+00:00')
            plan = db.execute("EXPLAIN QUERY PLAN SELECT details_json FROM operator_audit WHERE request_id=? AND json_extract(details_json,'$.kind')='request_outcome'", ('request',)).fetchall()
            self.assertTrue(any('operator_audit_request_kind' in row[3] for row in plan))

    def test_runtime_refuses_old_schema_until_explicit_atomic_upgrade(self):
        script = 'CREATE TABLE upgrade_probe(value INTEGER);\nINSERT INTO upgrade_probe VALUES(42);\n'
        with patch('forge.store.db._migrations', return_value=self.following(script)):
            target = storage.Store(self.store.root)
            with self.assertRaisesRegex(storage.StoreError, 'explicit schema migration'):
                storage.Store.open(self.store.root)
            self.assertEqual(upgrade.inspect(target)['pending_migrations'], [len(self.original)+1])
            upgrade.apply(target)
            upgrade.apply(target)  # Repeating after a lost response does not reapply it.
            with self.assertRaises(storage.StoreError) as old_runtime:
                with self.store.read_snapshot():
                    pass
            self.assertEqual(old_runtime.exception.code, 'schema_newer')
            with storage.Store.open(self.store.root).read_snapshot() as db:
                self.assertEqual(db.execute('SELECT value FROM upgrade_probe').fetchall()[0][0], 42)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], len(self.original)+1)

    def test_failed_migration_rolls_back_schema_data_and_version(self):
        script = 'CREATE TABLE upgrade_probe(value INTEGER);\nINSERT INTO missing_table VALUES(42);\n'
        with patch('forge.store.db._migrations', return_value=self.following(script)):
            target = storage.Store(self.store.root)
            with self.assertRaises(storage.sqlite3.DatabaseError):
                upgrade.apply(target)
        with storage.Store.open(self.store.root).read_snapshot() as db:
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='upgrade_probe'").fetchone())
            self.assertEqual(db.execute('SELECT COUNT(*) FROM schema_migrations').fetchone()[0], len(self.original))

    def test_changed_history_and_anchor_are_rejected_before_upgrade(self):
        changed = [(n, 'incorrect' if n == 1 else checksum, script) for n, checksum, script in self.original]
        with patch('forge.store.db._migrations', return_value=changed):
            target = storage.Store(self.store.root)
            with self.assertRaises(storage.StoreError) as result:
                upgrade.apply(target)
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
            target = storage.Store(self.store.root)
            self.fail_update = True
            with self.assertRaisesRegex(MigrationError, 'upgrade_host_update_failed'):
                self.upgrader.run(revision='b'*40, apply=True)
            self.assertEqual(self.upgrader.read()['stage'], 'reconciling')
            self.assertEqual(self.active, [])
            with target.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT value FROM upgrade_probe').fetchone()[0], 42)
            self.fail_update = False
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'upgraded')
            self.assertEqual(len(self.active), 3)
            self.assertEqual(self.upgrader.read()['stage'], 'complete')
            self.assertEqual(len([x for x in self.calls if x[0]=='install']), 1)

    def test_worker_race_restores_services_before_source_or_schema_changes(self):
        with patch.object(self.upgrader, 'blockers', side_effect=[[], [], ['uncleaned_jobs'], ['uncleaned_jobs']]):
            result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'draining')
        self.assertEqual(self.current, 'a'*40)
        self.assertEqual(len(self.active), 3)
        self.assertEqual(self.upgrader.read()['stage'], 'draining')
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

    def runtime(self, **changes):
        from forge.store.configuration import Configuration
        from forge.store.migration.drain import Drain
        with upgrade.connection(self.store, write=bool(changes)) as db:
            revision, settings, credential = Drain.current(db)
            if changes:
                db.execute('BEGIN IMMEDIATE')
                revision = Configuration(self.store).replace_in(db, 'runtime.json', {**settings, **changes},
                    expected_revision=revision, credential_ref=credential)
                db.commit()
                settings.update(changes)
            return revision, settings

    def test_apply_drains_then_restores_only_owned_field_before_updater(self):
        self.runtime(enabled=True, model='before')
        workers = [True]
        with patch.object(self.upgrader, 'blockers', side_effect=lambda store:['uncleaned_jobs'] if workers[0] else []):
            result = self.upgrader.run(revision='b'*40, apply=True)
            self.assertEqual(result['status'], 'draining')
            self.assertEqual(result['next_command'], 'forge migrate upgrade')
            self.assertEqual(self.runtime()[1], dict(enabled=False, model='before'))
            self.assertEqual(self.calls, [])
            self.assertEqual(len(self.active), 3)
            self.runtime(model='operator-choice')
            workers[0] = False
            def update(*args, **kwargs):
                from types import SimpleNamespace
                self.assertEqual(self.runtime()[1], dict(enabled=True, model='operator-choice'))
                return SimpleNamespace(returncode=0)
            with patch.object(self.host, 'run', side_effect=update):
                result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertTrue(self.upgrader.read()['drain']['restored'])

    def test_operator_reenable_is_preserved_and_requires_explicit_disable(self):
        self.runtime(enabled=True)
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.upgrader.run(revision='b'*40, apply=True)
            self.runtime(enabled=True, model='operator-choice')
            before = self.runtime()
            with self.assertRaisesRegex(MigrationError, 'drain_configuration_changed'):
                self.upgrader.run(revision='b'*40, apply=True)
            self.assertEqual(self.runtime(), before)
            self.assertEqual(self.calls, [])
            self.runtime(enabled=False)
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'draining')

    def test_disable_commit_is_recognized_after_lost_progress(self):
        self.runtime(enabled=True)
        with patch.object(self.upgrader, 'blockers', side_effect=[[], OSError('disconnect')]):
            with self.assertRaises(OSError):self.upgrader.run(revision='b'*40, apply=True)
        revision, settings = self.runtime()
        self.assertFalse(settings['enabled'])
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'draining')
        self.assertEqual(self.runtime()[0], revision)

    def test_disable_rollback_after_journal_is_resumable(self):
        from forge.store.configuration import Configuration
        self.runtime(enabled=True)
        original = Configuration.replace_in
        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise OSError('disconnect before commit')
        with patch.object(Configuration, 'replace_in', side_effect=interrupt, autospec=True):
            with self.assertRaises(OSError):self.upgrader.run(revision='b'*40, apply=True)
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(self.upgrader.read()['stage'], 'draining')
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'draining')
        self.assertFalse(self.runtime()[1]['enabled'])

    def test_restore_commit_is_not_replayed_over_a_later_operator_change(self):
        self.runtime(enabled=True)
        save = self.upgrader.save
        def interrupt(value, stage):
            if value.get('drain', {}).get('restored'):
                raise OSError('disconnect after restoration commit')
            return save(value, stage)
        with patch.object(self.upgrader, 'save', side_effect=interrupt):
            with self.assertRaises(OSError):self.upgrader.run(revision='b'*40, apply=True)
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(self.upgrader.read()['stage'], 'reconciling')
        self.runtime(enabled=False, model='operator-choice')
        revision = self.runtime()[0]
        self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(self.runtime(), (revision, dict(enabled=False, model='operator-choice')))

    def test_new_release_inherits_unfinished_drain_and_restores_original_intent(self):
        self.runtime(enabled=True)
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            self.upgrader.run(revision='b'*40, apply=True)
        self.assertFalse(self.runtime()[1]['enabled'])
        self.upgrader.run(revision='c'*40, apply=True)
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(self.current, 'c'*40)

    def test_actual_schema19_can_drain_before_target_binary_can_open_store(self):
        with patch('forge.store.db._migrations', return_value=self.original[:19]):
            self.store = storage.Store.create(self.root/'schema19')
            with self.store.write() as db:
                db.executemany('INSERT INTO store_metadata VALUES(?,?)', [('runtime_root',str(self.root)),('runtime_active','true')])
        (self.root/'state/storage').write_text('forge-sqlite-v1\n'+str(self.store.root)+'\n'+self.store.instance_id+'\n')
        self.runtime(enabled=True)
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            first = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(first['current_schema'], 19)
        self.assertEqual(first['status'], 'draining')
        self.assertFalse(self.runtime()[1]['enabled'])
        self.assertEqual(self.upgrader.run(revision='b'*40, apply=True)['status'], 'upgraded')
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(upgrade.inspect(storage.Store(self.store.root))['current_schema'], len(self.original))

    def test_partially_restarted_updater_drains_new_workers_before_retry(self):
        from types import SimpleNamespace
        self.runtime(enabled=True)
        def partial_update(*args, **kwargs):
            self.active = ['forge-supervisor.service', 'forge-extra.timer']
            return SimpleNamespace(returncode=1)
        with patch.object(self.host, 'run', side_effect=partial_update):
            with self.assertRaisesRegex(MigrationError, 'upgrade_host_update_failed'):
                self.upgrader.run(revision='b'*40, apply=True)
        self.assertTrue(self.runtime()[1]['enabled'])
        self.calls.clear()
        with patch.object(self.upgrader, 'blockers', return_value=['uncleaned_jobs']):
            result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'draining')
        self.assertFalse(self.runtime()[1]['enabled'])
        self.assertEqual(self.current, 'b'*40)
        self.assertEqual(self.calls, [])
        self.assertEqual(self.active, ['forge-supervisor.service', 'forge-extra.timer'])
        result = self.upgrader.run(revision='b'*40, apply=True)
        self.assertEqual(result['status'], 'upgraded')
        self.assertTrue(self.runtime()[1]['enabled'])
        self.assertEqual(set(self.active), {'forge-status.service','forge-ui.service','forge-supervisor.service','forge-extra.timer'})
