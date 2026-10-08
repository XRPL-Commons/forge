"""Forward-only repair keeps the frozen source and every previous staging byte."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests import test_migration_cutover as cutover_fixture
from forge.store.migration.cutover import package_version
from forge.store.migration.host import Host, failure
from forge.store.migration.snapshot import MigrationError
from forge.store.runtime import selected


class RepairTests(unittest.TestCase):
    setUp = cutover_fixture.CutoverTests.setUp
    driver = cutover_fixture.CutoverTests.driver
    write = cutover_fixture.CutoverTests.write
    target = 'b'*40

    def failed_import(self):
        def crash(*args): raise RuntimeError('import bug')
        with patch('forge.store.migration.cutover.Importer.run',side_effect=crash):
            with self.assertRaisesRegex(RuntimeError,'import bug'):self.cutover.activate()
        old=json.loads(self.cutover.path.read_text())
        self.assertEqual(old['stage'],'importing')
        self.assertFalse(self.active)
        self.assertTrue((self.root/'sqlite/state/forge.sqlite3').exists())
        self.retained=(self.root/'sqlite'/'private-diagnostic-proof')
        self.retained.write_bytes(b'original partial database')
        self.revision='a'*40
        self.host.identity=lambda:self.revision
        self.updates=[]
        def prepare(previous,target,version):
            self.assertEqual((previous,target,version),('a'*40,self.target,'1:corrected'))
        def install(previous,target):
            self.assertIn(self.revision,(previous,target));self.revision=target;self.updates.append(target)
        self.host.prepare_source_update=prepare
        self.host.install_source_update=install
        for name in ('forge.store.migration.cutover.importer_version','forge.store.migration.cutover.package_version','forge.store.migration.importer.importer_version'):
            manager=patch(name,return_value='1:corrected');manager.start();self.addCleanup(manager.stop)
        return old

    def assert_repaired(self,old):
        value=json.loads(self.cutover.path.read_text())
        self.assertEqual(value['stage'],'complete')
        for name in ('snapshot','manifest','original_runtime','services','authorized','actor_id','paused_digest','id'):
            self.assertEqual(value[name],old[name])
        self.assertEqual(value['source_revision'],self.target)
        self.assertEqual(value['tool_version'],'1:corrected')
        self.assertIsNone(value['tool_update'])
        self.assertEqual(len(value['source_updates']),1)
        receipt=value['source_updates'][0]
        self.assertEqual((Path(receipt['archive'])/'private-diagnostic-proof').read_bytes(),b'original partial database')
        self.assertFalse(self.retained.exists())
        self.assertTrue(selected(self.root))
        with selected(self.root).read_snapshot() as db:
            proof=json.loads(db.execute('SELECT evidence_json FROM runtime_operations').fetchone()[0])
            self.assertEqual(proof['source_updates'],value['source_updates'])

    def test_explicit_repair_reimports_identical_snapshot_and_retains_partial_store(self):
        old=self.failed_import()
        with self.assertRaisesRegex(MigrationError,'cutover_source_changed'):self.driver().resume()
        self.assertFalse(self.active)
        result=self.driver().resume(accept_tool_update=True,source_revision=self.target)
        self.assertEqual(result['status'],'active')
        self.assert_repaired(old)
        with self.assertRaisesRegex(MigrationError,'repair_after_selection_forbidden'):
            self.driver().resume(accept_tool_update=True,source_revision=self.target)

    def test_repair_recovers_crashes_around_checkout_archive_and_journal(self):
        # Each case uses a new independent installation, preserving exact inode
        # proof when a crash lands after rename but before the journal advances.
        for stop in ('tool_update_prepared','tool_update_renamed','tool_update_archived'):
            with self.subTest(stop=stop):
                fixture=RepairTests('test_explicit_repair_reimports_identical_snapshot_and_retains_partial_store')
                fixture.setUp()
                try:
                    old=fixture.failed_import()
                    def crash(stage):
                        if stage==stop:raise RuntimeError('interrupted repair')
                    with self.assertRaisesRegex(RuntimeError,'interrupted repair'):
                        fixture.driver(after_stage=crash).resume(accept_tool_update=True,source_revision=fixture.target)
                    self.assertFalse(fixture.active)
                    self.assertFalse((fixture.root/'state/storage').exists())
                    fixture.driver().resume()
                    fixture.assert_repaired(old)
                finally:fixture.doCleanups()

    def test_lost_checkout_response_resumes_same_authorized_target(self):
        old=self.failed_import()
        install=self.host.install_source_update
        def lost(previous,target):
            install(previous,target)
            self.host.install_source_update=install
            raise RuntimeError('lost checkout response')
        self.host.install_source_update=lost
        with self.assertRaisesRegex(RuntimeError,'lost checkout response'):
            self.driver().resume(accept_tool_update=True,source_revision=self.target)
        self.assertEqual(self.revision,self.target)
        self.assertTrue(self.retained.exists())
        self.assertFalse(self.active)
        self.driver().resume()
        self.assert_repaired(old)

    def test_changed_frozen_data_rejects_before_source_or_staging_changes(self):
        self.failed_import()
        self.write('factory.json',{'changed':True})
        with self.assertRaisesRegex(MigrationError,'cutover_source_state_changed'):
            self.driver().resume(accept_tool_update=True,source_revision=self.target)
        self.assertEqual(self.updates,[])
        self.assertTrue(self.retained.exists())
        self.assertIsNone(json.loads(self.cutover.path.read_text()).get('tool_update'))

    def test_unauthorized_or_conflicting_repair_cannot_replace_pending_intent(self):
        self.failed_import()
        with self.assertRaisesRegex(MigrationError,'repair_authorization_required'):
            self.driver().resume(source_revision=self.target)
        def crash(stage):
            if stage=='tool_update_prepared':raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):self.driver(after_stage=crash).resume(accept_tool_update=True,source_revision=self.target)
        with self.assertRaisesRegex(MigrationError,'repair_authority_mismatch'):
            self.driver().resume(accept_tool_update=True,source_revision='c'*40)
        self.assertEqual(self.updates,[])
        self.assertTrue(self.retained.exists())

    def test_unexpected_failure_diagnostics_never_include_exception_text(self):
        import sqlite3
        self.failed_import()
        value=failure(sqlite3.IntegrityError('secret row content'),{'root':str(self.root)})
        self.assertEqual(value,dict(ok=False,error='unexpected_migration_failure',exception_type='IntegrityError',stage='importing'))
        self.assertNotIn('secret',json.dumps(value))


class RepairGitTests(unittest.TestCase):
    def test_only_clean_forward_matching_source_is_installed_without_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary).resolve();source=base/'source';source.mkdir()
            def git(*args):
                return subprocess.run(['git','-C',str(source),*args],capture_output=True,text=True,check=True).stdout.strip()
            git('init','-q');git('config','commit.gpgsign','false');git('config','user.email','test@example.invalid');git('config','user.name','Test')
            (source/'forge').mkdir();(source/'forge/__init__.py').write_text('version=1\n')
            (source/'forge/example').mkdir();(source/'forge/example/module.py').write_text('pass\n')
            (source/'forge/example.py').write_text('pass\n')
            git('add','.');git('commit','-qm','first');previous=git('rev-parse','HEAD')
            (source/'forge/__init__.py').write_text('version=2\n')
            git('commit','-qam','corrected');target=git('rev-parse','HEAD');version=package_version(source)
            origin=base/'origin.git';subprocess.run(['git','clone','--bare',str(source),str(origin)],capture_output=True,check=True)
            git('remote','add','origin',str(origin));git('checkout','-q',previous)
            (source/'existing-diagnostic.md').write_text('keep this')
            host=Host(base,source);host.identity=lambda:git('rev-parse','HEAD')
            with self.assertRaisesRegex(MigrationError,'repair_bundle_source_mismatch'):
                host.prepare_source_update(previous,target,'1:wrong')
            self.assertEqual(git('rev-parse','HEAD'),previous)
            host.prepare_source_update(previous,target,version)
            host.install_source_update(previous,target)
            self.assertEqual(git('rev-parse','HEAD'),target)
            self.assertEqual((source/'existing-diagnostic.md').read_text(),'keep this')
            host.install_source_update(previous,target)
            with self.assertRaisesRegex(MigrationError,'repair_requires_forward_revision'):
                host.prepare_source_update(target,previous,version)
            (source/'forge/__init__.py').write_text('dirty\n')
            with self.assertRaisesRegex(MigrationError,'repair_source_not_clean'):
                host.prepare_source_update(target,target,version)
