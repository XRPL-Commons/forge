"""Initial legacy setup is imported without inventing a started factory."""
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_migration_cutover as active_fixture
from forge.store.migration.cutover import Cutover
from forge.store.migration.snapshot import MigrationError, Snapshot, initial_setup, sources
from forge.store.runtime import selected


class InitialSetupTests(unittest.TestCase):
    driver=active_fixture.CutoverTests.driver
    write=active_fixture.CutoverTests.write
    def setUp(self):
        active_fixture.CutoverTests.setUp(self)
        (self.root/'state/factory/factory-ledger.json').unlink()
        (self.root/'state/factory/factory-ledger.initialized').unlink()
        from forge.settings import DEFAULT
        self.write('factory.json',DEFAULT)
        self.runtime.update(enabled=False,merge_enabled=False)
        self.write('runtime.json',self.runtime)
        self.setup=dict(version=1,generation='initial-generation',phase='setup',apps={},checks={},probe_jobs=[],operation=None,error=None)
        self.write('state/onboarding/setup.json',self.setup)
        self.current='a'*40
        self.host.identity=lambda:self.current
        self.source_updates=[]
        self.host.prepare_source_update=lambda *args,**kwargs:self.source_updates.append(('verify',args,kwargs))
        def install(previous,target):
            self.assertFalse(self.active)
            self.source_updates.append(('install',previous,target))
            self.current=target
        self.host.install_source_update=install
        self.updater=[]
        def update(action,value,receipt=None):
            self.assertFalse(self.active)
            self.assertEqual(self.current,'b'*40)
            self.updater.append(action)
            if action=='prepare_initial':
                self.assertIsNone(selected(self.root))
                self.assertEqual(value['stage'],'source_preparing')
                return {'prepared_digest':'proof','source_revision':self.current}
            self.assertIsNotNone(selected(self.root))
            self.assertEqual(receipt['prepared_digest'],'proof')
            return {'committed':True}
        self.host.initial_updater=update
        self.original_sources=sources(self.root)

    def activate_initial(self,driver=None):
        return (driver or self.driver()).activate(source_revision='b'*40,source_pack={'fixture':True})

    def test_recorded_empty_ledger_also_preserves_inactive_setup(self):
        self.write('state/factory/factory-ledger.json',dict(version=1,issues={},jobs={},slots={},events=[]))
        self.write('state/factory/factory-ledger.initialized',{'version':1})
        self.write('state/factory/supervisor.json',{'started_at':1,'finished_at':2,'errors':[]})
        result=self.activate_initial()
        self.assertIn('state/factory/factory-ledger.json',Snapshot(result['diagnostic_copy']).entries)
        with selected(self.root).read_snapshot() as db:
            self.assertEqual(db.execute("SELECT phase FROM setup_instances WHERE scope='live'").fetchone()[0],'setup')

    def test_initial_import_preserves_setup_and_original_absence(self):
        result=self.activate_initial()
        store=selected(self.root)
        self.assertEqual(self.updater,['prepare_initial','commit_initial'])
        self.assertEqual(sources(self.root),self.original_sources)
        snapshot=Snapshot(result['diagnostic_copy'])
        self.assertNotIn('state/factory/factory-ledger.json',snapshot.entries)
        self.assertEqual(snapshot.manifest['source_revision'],'a'*40)
        with store.read_snapshot() as db:
            setup=dict(db.execute("SELECT * FROM setup_instances WHERE scope='live'").fetchone())
            self.assertEqual((setup['phase'],setup['generation'],setup['owner'],setup['account_type']),('setup','initial-generation','',''))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM units').fetchone()[0],0)
            config=json.loads(db.execute("SELECT settings_json FROM configuration WHERE domain='runtime.json' AND active=1").fetchone()[0])
            self.assertFalse(config['enabled']);self.assertFalse(config['merge_enabled'])
        self.assertEqual(self.current,'b'*40)
        self.assertEqual(sorted(self.active),self.original)

    def test_marker_publication_crash_commits_host_before_any_service_start(self):
        import os
        link=os.link
        def interrupted(source,target,**kwargs):
            link(source,target,**kwargs)
            if Path(target)==self.root/'state/storage':raise RuntimeError('marker crash')
        with patch('forge.store.migration.cutover.os.link',side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError,'marker crash'):self.activate_initial()
        self.assertFalse(self.active)
        self.assertEqual(json.loads(self.cutover.path.read_text())['stage'],'validated')
        self.assertTrue(self.driver().resume()['activation'])
        self.assertEqual(self.updater,['prepare_initial','commit_initial'])

    def test_initial_import_without_runtime_file_preserves_original_absence(self):
        (self.root/'runtime.json').unlink()
        result=self.activate_initial()
        self.assertNotIn('runtime.json',Snapshot(result['diagnostic_copy']).entries)
        self.assertFalse((self.root/'runtime.json').exists())
        self.assertIsNotNone(selected(self.root))

    def test_existing_identity_session_and_credentials_survive_initial_bridge(self):
        from forge.store.migration.identity import AUTH, CREDENTIAL
        from forge.store.identity import IdentityQueries
        auth=dict(version=1,config=dict(origin='https://anvil.example',client_id='identity-client',client_secret='s'*40,operators=[7]),
                  instance_id='initial-instance',epoch='epoch',sessions={'hashed-cookie':dict(operator={'id':7,'login':'operator'},csrf='synthetic-csrf',expires=4102444800)},
                  flows={},clients={},devices={},device_window={'start':1,'count':0},connection_lookups={})
        self.write(AUTH,auth)
        self.activate_initial()
        store=selected(self.root)
        self.assertEqual(IdentityQueries(store).session('hashed-cookie',100),auth['sessions']['hashed-cookie'])
        self.assertEqual((store.root/CREDENTIAL).read_text(),'s'*40)
        with store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT origin,instance_id FROM operator_auth_configuration').fetchone()[:],('https://anvil.example','initial-instance'))

    def test_source_preparation_failure_is_resumable_without_selecting_or_starting(self):
        update=self.host.initial_updater
        self.host.initial_updater=lambda *_: (_ for _ in ()).throw(MigrationError('build_failed'))
        with self.assertRaisesRegex(MigrationError,'build_failed'):self.activate_initial()
        self.assertIsNone(selected(self.root));self.assertFalse(self.active)
        self.assertEqual(self.current,'b'*40)
        self.host.initial_updater=update
        result=self.driver().resume()
        self.assertTrue(result['activation'])
        self.assertEqual(len([r for r in self.source_updates if r[0]=='verify']),1)

    def test_selected_commit_failure_keeps_new_backend_and_services_stopped(self):
        update=self.host.initial_updater
        def fail(action,*args):
            if action=='commit_initial':raise MigrationError('commit_failed')
            return update(action,*args)
        self.host.initial_updater=fail
        with self.assertRaisesRegex(MigrationError,'commit_failed'):self.activate_initial()
        self.assertIsNotNone(selected(self.root));self.assertFalse(self.active)
        self.host.initial_updater=update
        self.assertTrue(self.driver().resume()['activation'])

    def test_faults_before_and_after_source_preparation_resume_exact_snapshot(self):
        for stage in ('source_preparing','importing','selected'):
            with self.subTest(stage=stage):
                # Each fault needs a separate fixture because selection is one-way.
                fixture=InitialSetupTests('test_initial_import_preserves_setup_and_original_absence')
                fixture.setUp()
                try:
                    def fail(actual):
                        if actual==stage:raise RuntimeError('interrupted')
                    with self.assertRaisesRegex(RuntimeError,'interrupted'):
                        fixture.activate_initial(fixture.driver(after_stage=fail))
                    journal=json.loads(fixture.cutover.path.read_text())
                    result=fixture.driver().resume()
                    self.assertEqual(result['diagnostic_copy'],journal['snapshot'])
                    self.assertEqual(fixture.current,'b'*40)
                finally:fixture.doCleanups()

    def test_active_or_reset_history_never_becomes_an_empty_factory(self):
        for patch_value in ({'phase':'active'},{'reset_receipt':{'id':'reset'}},{'activation_request':'started'},
                            {'operation':{'action':'check'}},{'probe_jobs':[{'cleanup':False}]}):
            with self.subTest(patch=patch_value):
                self.write('state/onboarding/setup.json',{**self.setup,**patch_value})
                with self.assertRaisesRegex(MigrationError,'ledger_missing'):sources(self.root)
        self.write('state/onboarding/setup.json',self.setup)
        self.write('state/factory-v2.enabled',{'activated_at':1})
        with self.assertRaisesRegex(MigrationError,'ledger_missing'):sources(self.root)
        self.assertFalse(self.cutover.path.exists())

    def test_missing_ledger_without_setup_is_never_initialized(self):
        (self.root/'state/onboarding/setup.json').unlink()
        with self.assertRaisesRegex(MigrationError,'ledger_missing'):sources(self.root)

    def test_initial_setup_requires_explicit_pinned_source(self):
        with self.assertRaisesRegex(MigrationError,'initial_source_revision_required'):self.cutover.activate()
        self.assertFalse(self.cutover.path.exists())

    def test_setup_changed_during_stop_is_rejected_before_source_install(self):
        command=self.host.command
        def stop(*args):
            result=command(*args)
            if 'stop' in args:self.write('state/onboarding/setup.json',{**self.setup,'operation':{'action':'check'}})
            return result
        self.host.command=stop
        with self.assertRaisesRegex(MigrationError,'initial_setup_changed|ledger_missing'):self.activate_initial()
        self.assertEqual(self.current,'a'*40)
        self.assertFalse(self.active)
        self.assertIsNone(selected(self.root))

    def test_newer_runtime_settings_are_frozen_after_services_stop(self):
        command=self.host.command
        newer={**self.runtime,'cycle_seconds':31}
        def stop(*args):
            result=command(*args)
            if 'stop' in args:self.write('runtime.json',newer)
            return result
        self.host.command=stop
        result=self.activate_initial()
        self.assertEqual(Snapshot(result['diagnostic_copy']).json('runtime.json'),newer)
        with selected(self.root).read_snapshot() as db:
            actual=json.loads(db.execute("SELECT settings_json FROM configuration WHERE domain='runtime.json' AND active=1").fetchone()[0])
        self.assertEqual(actual,newer)

    def test_changed_prior_source_rejected_before_journal_or_pack_import(self):
        with self.assertRaisesRegex(MigrationError,'initial_source_changed'):
            self.driver().activate(source_revision='b'*40,previous_revision='c'*40,source_pack={'fixture':True})
        self.assertFalse(self.source_updates)
        self.assertFalse(self.cutover.path.exists())

    def test_release_repair_cannot_replace_initial_prepared_asset_authority(self):
        def fail(stage):
            if stage=='importing':raise RuntimeError('interrupted')
        with self.assertRaises(RuntimeError):self.activate_initial(self.driver(after_stage=fail))
        before=list(self.source_updates)
        with self.assertRaisesRegex(MigrationError,'initial_setup_release_repair_not_supported'):
            self.driver().resume(accept_tool_update=True,source_revision='c'*40)
        self.assertEqual(self.source_updates,before)
        self.assertFalse(self.active)
        self.assertIsNone(selected(self.root))
        self.assertTrue(self.driver().resume()['activation'])

    def test_real_public_parity_covers_initial_setup_absence(self):
        driver=Cutover(self.host,qualify=self.qualification)
        self.assertTrue(self.activate_initial(driver)['activation'])

