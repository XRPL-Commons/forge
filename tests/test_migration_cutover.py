import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.config import validate
from forge.store.configuration import Configuration
from forge.store.migration.cutover import Cutover, maintenance_writable
from forge.store.migration.host import Host
from forge.store.migration.snapshot import MigrationError, encode
from forge.store.runtime import selected


class CutoverTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name).resolve(); self.root=self.base/'forge'; self.root.mkdir(mode=0o700)
        self.source=self.base/'source'; self.source.mkdir()
        self.ledger=dict(version=1,issues={},jobs={},slots={},events=[])
        self.runtime=validate({'enabled':True,'merge_enabled':True,'image':'sha256:'+'1'*64})
        self.write('runtime.json',self.runtime)
        self.write('factory.json',{'repositories':[]})
        self.write('state/factory/factory-ledger.json',self.ledger)
        self.write('state/factory/factory-ledger.initialized',{'version':1})
        self.active={'forge-status.service','forge-ui.service','forge-supervisor.service'}
        self.original=sorted(self.active); self.calls=[]
        self.host=Host(self.root,self.source)
        self.host.identity=lambda:'a'*40
        self.host.units=lambda:sorted(self.active)
        self.host.inspect=lambda:dict(plan='plan',blockers=[],services=sorted(self.active),source_revision='a'*40)
        def command(*args):
            self.calls.append(args)
            if 'stop' in args:self.active.difference_update(args[args.index('stop')+1:])
            if 'start' in args:self.active.update(args[args.index('start')+1:])
            return ''
        self.host.command=command
        self.host.quiet=lambda:self.assertFalse(self.active)
        self.parity=lambda snapshot,store:dict(status='passed',findings=[],counts={'fixture':1})
        self.qualification=lambda root,source:dict(qualified=True,sqlite='3.53.1')
        self.cutover=self.driver()
        for target in ('forge.store.migration.cutover.importer_version','forge.store.migration.cutover.package_version','forge.store.migration.importer.importer_version'):
            patched=patch(target,return_value='1:fixture');patched.start();self.addCleanup(patched.stop)

    def driver(self,**kwargs):
        return Cutover(self.host,public_parity=self.parity,qualify=self.qualification,**kwargs)

    def write(self,name,value):
        path=self.root/name; path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(encode(value));path.chmod(0o600)

    def test_activation_captures_validates_restores_intent_and_restarts(self):
        result=self.cutover.activate(expected_plan='plan')
        self.assertTrue(result['activation'])
        self.assertEqual(result['status'],'active')
        self.assertEqual(sorted(self.active),self.original)
        store=selected(self.root)
        self.assertEqual(Configuration(store).get('runtime.json')['settings'],self.runtime)
        self.assertFalse(json.loads((self.root/'runtime.json').read_text())['enabled'])
        self.assertEqual((self.root/'state/storage').stat().st_nlink,1)
        self.assertEqual((self.root/'state/storage').stat().st_mode & 0o777,0o600)
        with store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT stage FROM runtime_operations').fetchone()[0],'complete')
            self.assertEqual(db.execute('SELECT status FROM import_runs').fetchone()[0],'validated')
        maintenance_writable(self.root)
        self.assertTrue((Path(result['diagnostic_copy'])/'sealed').exists())

    def test_drain_pauses_admission_and_returns_without_killing_workers(self):
        self.cutover._pending=lambda:dict(pipeline=1,slots=1,auxiliary=0)
        result=self.cutover.activate()
        self.assertEqual(result['status'],'draining')
        self.assertEqual(self.active,{'forge-supervisor.service'})
        paused=json.loads((self.root/'runtime.json').read_text())
        self.assertFalse(paused['enabled']);self.assertFalse(paused['merge_enabled'])
        self.assertFalse((self.root/'state/storage').exists())
        with self.assertRaisesRegex(MigrationError,'maintenance_in_progress'):
            maintenance_writable(self.root)
        self.assertEqual(self.driver().resume()['status'],'active')

    def test_resume_after_selected_never_reads_or_restores_legacy(self):
        def interrupt(stage):
            if stage=='selected':raise RuntimeError('lost connection')
        with self.assertRaisesRegex(RuntimeError,'lost connection'):
            self.driver(after_stage=interrupt).activate()
        self.assertTrue((self.root/'state/storage').exists())
        (self.root/'runtime.json').write_text('not JSON any longer')
        self.assertEqual(self.driver().resume()['status'],'active')
        self.assertEqual((self.root/'runtime.json').read_text(),'not JSON any longer')

    def test_resume_after_import_capture_is_idempotent(self):
        def interrupt(stage):
            if stage=='importing':raise RuntimeError('lost connection')
        with self.assertRaises(RuntimeError):self.driver(after_stage=interrupt).activate()
        snapshot=json.loads(self.cutover.path.read_text())['snapshot']
        self.assertTrue((Path(snapshot)/'sealed').exists())
        result=self.driver().resume()
        self.assertEqual(result['diagnostic_copy'],snapshot)
        before=(self.root/'state/storage').read_bytes()
        self.driver().resume()
        self.assertEqual((self.root/'state/storage').read_bytes(),before)

    def test_changed_source_state_before_selection_blocks_resume(self):
        def interrupt(stage):
            if stage=='importing':raise RuntimeError('lost connection')
        with self.assertRaises(RuntimeError):self.driver(after_stage=interrupt).activate()
        self.write('factory.json',{'repositories':[],'changed':True})
        with self.assertRaisesRegex(MigrationError,'source_state_changed'):self.driver().resume()
        self.assertFalse((self.root/'state/storage').exists())

    def test_public_parity_failure_keeps_staging_and_no_marker(self):
        self.cutover.public_parity=lambda *_:dict(status='blocked',findings=['changed public issue'],counts={})
        with self.assertRaisesRegex(MigrationError,'public_view_parity_failed'):self.cutover.activate()
        self.assertFalse((self.root/'state/storage').exists())
        self.assertTrue((self.root/'sqlite/state/forge.sqlite3').exists())
        self.assertFalse(self.active)
        self.assertEqual(self.driver().resume()['status'],'active')

    def test_service_qualification_failure_never_selects(self):
        self.cutover.qualify=lambda *_:dict(qualified=False)
        with self.assertRaisesRegex(MigrationError,'service_runtime_unqualified'):self.cutover.activate()
        self.assertFalse((self.root/'state/storage').exists())

    def test_pending_setup_and_unmanaged_schedule_block_before_pause(self):
        self.write('state/onboarding/setup.json',{'phase':'active','operation':{'action':'reset'}})
        with self.assertRaisesRegex(MigrationError,'setup_operation_pending'):self.cutover.activate()
        self.assertEqual(json.loads((self.root/'runtime.json').read_text()),self.runtime)
        self.assertFalse(self.cutover.path.exists())

    def test_reconciliation_gates_remain_and_budget_never_resets(self):
        self.ledger['issues']['1:1']=dict(key='1:1',repo_id=1,repo='test/repo',number=1,title='Change',state='ready',reason=None,
            requirements='req',candidate=None,active=None,failures=[],budget_start=0,created_at=10)
        self.ledger['events']=[dict(sequence=1,at=10,issue='1:1',kind='discovered')]
        self.write('state/factory/factory-ledger.json',self.ledger)
        self.write('factory.json',{'repositories':[{'id':1,'enabled':True}]})
        result=self.cutover.activate()
        self.assertEqual(result['reconciliation_holds'],1)
        with selected(self.root).read_snapshot() as db:
            self.assertEqual(db.execute('SELECT budget_generation FROM units').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def truncated_inventory(self):
        self.write('factory.json',{'repositories':[{'id':1,'name':'test/repo','enabled':True}]})
        self.write('state/issues/inventory.json',{'repositories':{'1':{'name':'test/repo','at':20,'issues':{'1':dict(number=1,title='Long request',body='partial',body_truncated=True,state='open',labels=['forge:ready'],assignees=[],content_hash='req')}}}})

    def test_truncated_untracked_observation_preserved_but_cannot_admit_before_refresh(self):
        from forge.store.transitions import Lifecycle
        from forge.store.models import TransitionError
        self.truncated_inventory()
        self.assertEqual(self.cutover.activate()['status'],'active')
        store=selected(self.root)
        with store.read_snapshot() as db:
            value=json.loads(db.execute('SELECT requirements_json FROM issue_revisions').fetchone()[0])
            self.assertTrue(value['body_truncated'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM import_findings WHERE code='truncated_observation'").fetchone()[0],1)
        with self.assertRaisesRegex(TransitionError,'fresh complete'):
            Lifecycle(store).admit_known_issue(request_id='admit',actor_id='controller',unit_id='new',issue_id='1:1',expected_issue_version=1)

    def test_truncated_live_observation_keeps_reconciliation_boundary(self):
        self.truncated_inventory()
        self.ledger['issues']['1:1']=dict(key='1:1',repo_id=1,repo='test/repo',number=1,title='Long request',state='ready',reason=None,
            requirements='req',candidate=None,active=None,failures=[],budget_start=0,created_at=10)
        self.write('state/factory/factory-ledger.json',self.ledger)
        result=self.cutover.activate()
        self.assertEqual(result['reconciliation_holds'],1)

    def test_marker_publication_crash_recovers_its_duplicate_link(self):
        import os
        link=os.link
        def interrupted(source,target,**kwargs):
            link(source,target,**kwargs)
            if Path(target)==self.root/'state/storage':
                raise RuntimeError('lost immediately after marker publication')
        with patch('forge.store.migration.cutover.os.link',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.cutover.activate()
        self.assertEqual((self.root/'state/storage').stat().st_nlink,2)
        self.assertEqual(self.driver().resume()['status'],'active')
        self.assertEqual((self.root/'state/storage').stat().st_nlink,1)

    def test_unsealed_interrupted_capture_is_retained_and_recaptured(self):
        def partial(root,destination,**kwargs):
            Path(destination).mkdir(mode=0o700)
            (Path(destination)/'000000.source').write_bytes(b'partial private bytes')
            raise RuntimeError('capture interrupted')
        with patch('forge.store.migration.cutover.capture',side_effect=partial):
            with self.assertRaises(RuntimeError):self.cutover.activate()
        self.assertEqual(self.driver().resume()['status'],'active')
        retained=list(self.base.glob('forge-pre-sqlite-*.incomplete-*'))
        self.assertEqual(len(retained),1)
        self.assertEqual((retained[0]/'000000.source').read_bytes(),b'partial private bytes')

    def test_unknown_blocking_import_finding_cannot_activate(self):
        self.write('state/unmapped.json',{'new_authority':True})
        with self.assertRaisesRegex(MigrationError,'cutover_import_findings'):
            self.cutover.activate()
        self.assertFalse((self.root/'state/storage').exists())

    def test_service_restart_failure_resumes_only_sqlite(self):
        command=self.host.command
        def fail(*args):
            if 'start' in args:raise MigrationError('service_unavailable')
            return command(*args)
        self.host.command=fail
        with self.assertRaisesRegex(MigrationError,'service_unavailable'):self.cutover.activate()
        self.assertIsNotNone(selected(self.root))
        self.host.command=command
        self.assertEqual(self.driver().resume()['status'],'active')


class CutoverWriterFenceTests(unittest.TestCase):
    setUp = CutoverTests.setUp
    driver = CutoverTests.driver
    write = CutoverTests.write

    def child(self, kind):
        import select
        import subprocess
        import sys
        script = r'''
import sys
from pathlib import Path
from forge.settings import Settings
from forge.operator_auth import OperatorAuth
from forge.state import AtomicStore
root=Path(sys.argv[1]); kind=sys.argv[2]
settings=Settings(root); auth=OperatorAuth(root)
if kind=='held':
    with AtomicStore(root/'state/factory','factory-ledger').transaction() as data:
        print('ready',flush=True); input()
    print('finished',flush=True)
else:
    print('ready',flush=True); input()
    try:
        if kind=='settings':settings.save_config({'changed':True})
        else:auth.configure('https://forge.example','client-id','x'*40,[1])
    except Exception as error:print(getattr(error,'code',str(error)),flush=True)
    else:raise SystemExit('unexpected write')
'''
        process = subprocess.Popen([sys.executable, '-c', script, str(self.root), kind],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        self.assertTrue(select.select([process.stdout], [], [], 10)[0], 'writer did not initialize')
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        return process

    def late_writers(self, hook):
        writers = [self.child(kind) for kind in ('settings', 'auth')]
        attempted = []
        def attempt(*args):
            if not attempted:
                attempted.append(True)
                for writer in writers:
                    stdout, stderr = writer.communicate('go\n', timeout=10)
                    self.assertEqual(writer.returncode, 0, stderr)
                    self.assertEqual(stdout.strip(), 'cutover_maintenance_in_progress')
            return self.parity(*args) if hook == 'public_parity' else self.qualification(*args)
        setattr(self.cutover, hook, attempt)
        self.assertEqual(self.cutover.activate()['status'], 'active')
        self.assertTrue(attempted)
        self.assertEqual(json.loads((self.root/'factory.json').read_text()), {'repositories':[]})
        self.assertFalse((self.root/'state/operator-auth/auth.json').exists())
        with selected(self.root).read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operator_auth_configuration').fetchone()[0], 0)

    def test_separate_process_settings_and_identity_writes_during_public_parity_rejected(self):
        self.late_writers('public_parity')

    def test_separate_process_settings_and_identity_writes_during_qualification_rejected(self):
        self.late_writers('qualify')

    def test_writer_already_in_transaction_prevents_capture_until_finished(self):
        writer = self.child('held')
        with self.assertRaisesRegex(MigrationError, 'cutover_writers_busy'):
            self.cutover.activate()
        self.assertFalse((self.root/'state/storage').exists())
        journal=json.loads(self.cutover.path.read_text())
        self.assertEqual(journal['stage'], 'stopping')
        self.assertFalse(Path(journal['snapshot']).exists())
        stdout, stderr = writer.communicate('finish\n', timeout=10)
        self.assertEqual(writer.returncode, 0, stderr)
        self.assertEqual(stdout.strip(), 'finished')
        self.assertEqual(self.driver().resume()['status'], 'active')

    def test_preinstantiated_settings_and_identity_refuse_after_selection(self):
        from forge.settings import Settings
        from forge.operator_auth import OperatorAuth
        from forge.store.db import StoreError
        settings, auth = Settings(self.root), OperatorAuth(self.root)
        self.cutover.activate()
        for read in (settings.load, settings.configured, settings.authority.phase):
            with self.assertRaisesRegex(StoreError, 'legacy state access is disabled'):
                read()
        with self.assertRaisesRegex(StoreError, 'legacy state access is disabled'):
            settings.save_config({'changed':True})
        with self.assertRaisesRegex(StoreError, 'legacy state access is disabled'):
            auth.configure('https://forge.example','client-id','x'*40,[1])

    def test_draining_allows_cleanup_but_rejects_config_and_identity_intents(self):
        from forge.settings import Settings
        from forge.operator_auth import OperatorAuth
        from forge.state import AtomicStore
        settings, auth = Settings(self.root), OperatorAuth(self.root)
        self.cutover._pending=lambda:dict(pipeline=1,slots=1,auxiliary=0)
        self.assertEqual(self.cutover.activate()['status'], 'draining')
        with AtomicStore(self.root/'state/factory','factory-ledger').transaction() as data:
            self.assertEqual(data, self.ledger)
        with self.assertRaisesRegex(MigrationError,'maintenance_in_progress'):
            settings.save_config({'changed':True})
        with self.assertRaisesRegex(MigrationError,'maintenance_in_progress'):
            auth.configure('https://forge.example','client-id','x'*40,[1])

    def test_crashed_import_keeps_new_writers_fenced_without_live_lock(self):
        from forge.settings import write_json
        def interrupt(stage):
            if stage=='importing':raise RuntimeError('interrupted')
        with self.assertRaisesRegex(RuntimeError,'interrupted'):
            self.driver(after_stage=interrupt).activate()
        with self.assertRaisesRegex(MigrationError,'maintenance_in_progress'):
            write_json(self.root/'state/factory/supervisor.json', {'late':True})
