import json
import os
from pathlib import Path
import tempfile
import unittest

from forge.store.migration.snapshot import capture, Snapshot, MigrationError, encode
from forge.store.migration.importer import Importer
from forge.store.transitions import project_unit


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=Path(self.tmp.name).resolve(); self.root=self.base/'legacy'; self.root.mkdir(mode=0o700)
        self.issue=dict(key='1:1',repo_id=1,repo='test/repo',number=1,title='Change',state='ready',reason=None,
                        requirements='requirements-hash',candidate=None,active=None,failures=[],budget_start=0,created_at=10)
        self.ledger=dict(version=1,issues={'1:1':self.issue},jobs={},slots={},events=[dict(sequence=1,at=10,issue='1:1',kind='discovered')])
        self.write('factory.json',{'repositories':[{'id':1,'enabled':True}]})
        self.write('state/factory/factory-ledger.initialized',{'version':1})

    def write(self,name,value):
        path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(encode(value));path.chmod(0o600)

    def snapshot(self):
        self.write('state/factory/factory-ledger.json',self.ledger)
        return capture(self.root,self.base/'snapshot',source_revision='a'*40,storage_generation=1,quiescent=lambda:None)

    def test_import_roundtrips_every_source_and_replays_without_duplicate_rows(self):
        self.write('state/extra.json',{'messages':[{'text':'private words'*100000}],'empty':{}})
        source=self.snapshot(); importer=Importer(source,self.base/'staging',batch=2)
        report=importer.run(); again=importer.run()
        self.assertEqual(report['rows'],again['rows']);self.assertEqual(report['source_parity_mismatches'],0)
        self.assertFalse(report['activation_allowed']);self.assertEqual(importer.read('state/extra.json')['empty'],{})
        with importer.store.read_snapshot() as db:
            view=project_unit(db,'legacy:unit:1:1')
            self.assertEqual(view['current_blocker']['code'],'legacy_reconciliation')
            self.assertEqual(db.execute('SELECT sequence FROM events').fetchone()[0],1)
            self.assertLessEqual(db.execute('SELECT max(length(content)) FROM import_records').fetchone()[0],1024*1024)
            self.assertEqual(db.execute('SELECT status FROM import_runs').fetchone()[0],'validating')

    def test_resume_after_committed_batch_does_not_repeat_rows(self):
        source=self.snapshot()
        def killed(name,end):
            raise RuntimeError('lost acknowledgement')
        importer=Importer(source,self.base/'staging',batch=1,after_batch=killed)
        with self.assertRaises(RuntimeError): importer.run()
        before=importer.store.path.stat().st_size
        resumed=Importer(source,self.base/'staging',batch=1).run()
        self.assertGreater(before,0);self.assertEqual(resumed['rows']['units'],1);self.assertEqual(resumed['rows']['events'],1)

    def test_budget_history_is_preserved_without_resetting_current_charges(self):
        self.issue['budget_start']=1
        for n,charged in enumerate((True,True,False)):
            identifier=f'job-{n}'
            self.ledger['jobs'][identifier]=dict(id=identifier,issue_key='1:1',role='build',cleanup=True,fenced=True,
                  slot='slot',created_at=10+n,phase='cleaned',outcome='failed',requirements='requirements-hash',
                  capture={'logs':'long output'*200000,'result':{'outcome':'unable'}},branch='work',target_branch='main')
            self.issue['failures'].append(dict(attempt=identifier,charged=charged,at=11+n))
        importer=Importer(self.snapshot(),self.base/'staging',batch=1);report=importer.run()
        with importer.store.read_snapshot() as db:
            self.assertEqual(project_unit(db,'legacy:unit:1:1')['charged_failures'],1)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],4)
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0],0)
        self.assertEqual(report['rows']['jobs'],3)

    def test_snapshot_hash_change_and_different_manifest_cannot_resume(self):
        source=self.snapshot();Importer(source,self.base/'staging').run()
        (source.root/source.entries['factory.json']['file']).write_bytes(b'{}')
        with self.assertRaisesRegex(MigrationError,'snapshot_changed'): Snapshot(source.root)

    def test_capture_rejects_active_or_uncleaned_jobs(self):
        self.ledger['jobs']['job']=dict(id='job',issue_key='1:1',role='build',cleanup=False,slot='slot')
        self.ledger['slots']['slot']='job'
        with self.assertRaisesRegex(MigrationError,'uncleaned_jobs'):self.snapshot()
        self.assertFalse((self.base/'snapshot').exists())

    def test_capture_never_seals_a_source_that_changes_during_copy(self):
        self.write('state/factory/factory-ledger.json',self.ledger)
        calls=[]
        def check():
            calls.append(True)
            if len(calls)==2:self.write('factory.json',{'changed':True})
        with self.assertRaisesRegex(MigrationError,'source_changed'):
            capture(self.root,self.base/'snapshot',source_revision='a',storage_generation=1,quiescent=check)
        self.assertFalse((self.base/'snapshot'/'sealed').exists())

    def test_missing_initialized_store_and_symlink_sources_fail_closed(self):
        self.write('state/missing.initialized',{'version':1})
        with self.assertRaisesRegex(MigrationError,'initialized_source_missing'):self.snapshot()
        (self.root/'state/missing.initialized').unlink()
        (self.root/'state/alias').symlink_to(self.base,target_is_directory=True)
        with self.assertRaisesRegex(MigrationError,'symlink_source'):self.snapshot()

    def test_completed_closure_and_merge_receipt_are_distinct(self):
        self.issue['state']='cancelled'
        self.issue['github_observation']={'at':20,'issue':{'number':1,'title':'Change','body':'','state':'closed','state_reason':'completed'}}
        importer=Importer(self.snapshot(),self.base/'staging');importer.run()
        with importer.store.read_snapshot() as db:
            view=project_unit(db,'legacy:unit:1:1')
            self.assertEqual(view['state'],'closed');self.assertEqual(view['members'][0]['projected_state'],'done')
            self.assertIsNone(view['merge_sha'])

    def test_staging_rejects_lifecycle_commands_and_parity_detects_changed_facts(self):
        from forge.store.transitions import Lifecycle
        from forge.store.models import Command,TransitionError
        importer=Importer(self.snapshot(),self.base/'staging');importer.run()
        with self.assertRaisesRegex(TransitionError,'staging imports'):
            Lifecycle(importer.store).pause(Command('pause','operator','legacy:unit:1:1',1))
        with importer.store.write() as db:
            db.execute("UPDATE units SET version=2 WHERE id='legacy:unit:1:1'")
        with self.assertRaisesRegex(MigrationError,'unit_parity_mismatch'):
            importer.validate()

    def test_merge_receipt_in_issue_evidence_imports_terminal_history(self):
        self.issue['state']='merged'
        self.issue['candidate']=dict(pr=12,head='head',base='base',policy='policy',requirements='requirements-hash',
                                    key='identity',build_attempt='build',accepted_at=12,branch='work')
        self.issue['evidence']={'merge':{'pr':12,'sha':'confirmed-merge'}}
        self.ledger['jobs']['build']=dict(id='build',issue_key='1:1',role='build',cleanup=True,fenced=True,
                          slot='slot',created_at=11,phase='cleaned',outcome='accepted',branch='work',target_branch='main')
        importer=Importer(self.snapshot(),self.base/'staging');report=importer.run()
        with importer.store.read_snapshot() as db:
            view=project_unit(db,'legacy:unit:1:1')
            self.assertEqual(view['state'],'merged');self.assertEqual(view['merge_sha'],'confirmed-merge')
            self.assertIsNone(view['current_blocker'])
            self.assertEqual(db.execute('SELECT count(*) FROM candidate_evidence').fetchone()[0],1)
        self.assertEqual(report['normalized_checks']['jobs'],1)

    def test_changed_importer_version_cannot_resume_into_existing_staging(self):
        from unittest.mock import patch
        source=self.snapshot();Importer(source,self.base/'staging').run()
        with patch('forge.store.migration.importer.importer_version',return_value='new-code'):
            with self.assertRaisesRegex(MigrationError,'import_identity_conflict'):
                Importer(source,self.base/'staging')

    def test_verify_does_not_create_an_empty_destination(self):
        with self.assertRaisesRegex(MigrationError,'import_missing'):
            Importer(self.snapshot(),self.base/'absent',create=False)
        self.assertFalse((self.base/'absent').exists())

    def test_real_process_exit_after_commit_resumes_one_import(self):
        import subprocess,sys
        source=self.snapshot()
        script="""from forge.store.migration.importer import Importer
import os,sys
Importer(sys.argv[1],sys.argv[2],batch=1,after_batch=lambda *_:os._exit(23)).run()
"""
        destination=self.base/'staging'
        process=subprocess.run([sys.executable,'-c',script,str(source.root),str(destination)],timeout=30)
        self.assertEqual(process.returncode,23)
        importer=Importer(source,destination,batch=1)
        report=importer.run()
        self.assertEqual(report['rows']['units'],1)
        with importer.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM import_runs').fetchone()[0],1)

    def test_staging_cannot_overlap_snapshot_or_take_over_existing_domain_data(self):
        from forge.store.db import Store
        source=self.snapshot()
        with self.assertRaisesRegex(MigrationError,'overlapping_import_paths'):
            Importer(source,source.root)
        store=Store.create(self.base/'existing')
        with store.write() as db:
            db.execute("INSERT INTO runtime_controls(name,updated_at) VALUES('existing',1)")
        with self.assertRaisesRegex(MigrationError,'destination_not_empty'):
            Importer(source,self.base/'existing')
        with store.read_snapshot() as db:
            self.assertIsNone(db.execute("SELECT value FROM store_metadata WHERE key='import_only'").fetchone())
