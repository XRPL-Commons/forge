import sqlite3
import unittest
from unittest.mock import patch

from forge.store.migration.importer import Importer
from forge.store.migration.snapshot import MigrationError
from forge.store.configuration import Configuration,ConfigurationConflict
from tests import test_store_import as fixtures


class OperationalImportTests(unittest.TestCase):
    setUp=fixtures.ImportTests.setUp
    write=fixtures.ImportTests.write
    snapshot=fixtures.ImportTests.snapshot

    def sources(self):
        self.write('state/github-access/access.json',{'observed_at':12,'roles':{'build':dict(status='ok',observed_at=12,
            connection='fingerprint',repositories=[dict(id=1,name='test/repo',archived=False,disabled=False)])}})
        self.write('state/github-access/sync.json',{'finished_at':12,'status':'ok'})
        self.write('state/github-access/repositories.json',{'repositories':{'1':dict(name='test/repo',status='ready',
            revision='revision',started_at=12,report={'requirements':[]},last_known={'available':True},previous_status='pending')}})
        self.write('state/onboarding/setup.json',dict(generation='generation',phase='active',owner='operator',account_type='organization',
            version=1,operation=None,probe_jobs=[dict(id='probe',cleanup=True,fenced=True,created_at=1,role='build')]))
        self.write('state/deployments.json',{'version':1,'repositories':{'1':dict(name='test/repo',status='confirmed',workflows_digest='digest',
            head='head',job=dict(id='strategist',cleanup=True,fenced=True,capture={'logs':'large'*300000}),proposal={'strategy':'unobservable'},
            summary='Confirmed',reason=None,error=None,observed_at=2,checked_at=2,reread_requested=False,observation=None,
            observed=None,exposure_issue=None)}})
        self.write('state/factory/check-requests.json',{'1:1':13})
        self.write('state/environment-scans.json',{'1':dict(id='scan',status='complete',started_at=12,error=None)})
        self.write('state/environment-proposals.json',{'1':dict(repo_id=1,repo='test/repo',revision='revision',head='head',services=[],evidence=[])})

    def test_import_checks_setup_access_deployment_and_environment_facts(self):
        self.sources();importer=Importer(self.snapshot(),self.base/'staging');report=importer.run()
        self.assertEqual(report['supporting_checks']['auxiliary_executions'],2)
        self.assertEqual(report['supporting_checks']['deployment_profiles'],1)
        self.assertEqual(report['supporting_checks']['setup_instances'],1)
        self.assertEqual(report['supporting_checks']['access_repositories'],1)
        self.assertEqual(report['supporting_checks']['environments'],1)
        self.assertNotIn('domain_mapping_pending',{row['code'] for row in report['findings']})
        self.assertFalse(report['activation_allowed'])
        self.assertEqual(Configuration(importer.store).get('factory.json')['settings']['repositories'][0]['id'],1)
        with self.assertRaises(ConfigurationConflict):
            Configuration(importer.store).replace('factory.json',{},expected_revision=1)

    def test_auxiliary_batch_crash_resumes_without_duplicate_execution_or_output(self):
        self.sources();source=self.snapshot()
        def fail(name,end):
            if name=='normalized:deployment_profiles':raise RuntimeError('killed after commit')
        with self.assertRaises(RuntimeError):Importer(source,self.base/'staging',after_batch=fail).run()
        importer=Importer(source,self.base/'staging');report=importer.run()
        self.assertEqual(report['supporting_checks']['auxiliary_executions'],2)
        with importer.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auxiliary_executions').fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM auxiliary_artifacts WHERE execution_id='strategist'").fetchone()[0],2)

    def test_changed_environment_and_probe_ownership_fail_parity(self):
        self.sources();importer=Importer(self.snapshot(),self.base/'staging');importer.run()
        with importer.store.write() as db:db.execute("UPDATE environment_scans SET status='failed'")
        with self.assertRaisesRegex(MigrationError,'environment_parity_mismatch'):importer.validate()
        with importer.store.write() as db:
            db.execute("UPDATE environment_scans SET status='completed'")
            with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE auxiliary_executions SET cleanup=0 WHERE id='probe'")
            with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE auxiliary_artifacts SET content=X'00'")

    def test_failure_evidence_is_lazy_and_not_limited_to_a_small_row(self):
        evidence={'log':'e'*2000000}
        self.ledger['jobs']['failed']=dict(id='failed',issue_key='1:1',role='build',cleanup=True,fenced=True,
            slot='slot',created_at=10,phase='cleaned',outcome='failed',requirements='requirements-hash',branch='work',target_branch='main')
        self.issue['failures']=[dict(attempt='failed',role='build',cause='tests',investigation='complete',retry_state='ready',
            evidence=evidence,findings={'approach':'repair'},at=11,charged=True)]
        importer=Importer(self.snapshot(),self.base/'staging');report=importer.run()
        self.assertEqual(report['supporting_checks']['attempt_failures'],1)
        with importer.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM job_artifacts WHERE kind='failure_evidence'").fetchone()[0],2)
            self.assertEqual(db.execute('SELECT evidence_kind FROM attempt_failures').fetchone()[0],'failure_evidence')

    def test_unknown_issue_fields_remain_explicit_blockers(self):
        self.issue['new_approval_protocol']={'approved':True}
        report=Importer(self.snapshot(),self.base/'staging').run()
        self.assertIn('unmapped_entity_fields',{row['code'] for row in report['findings']})
        self.assertFalse(report['activation_allowed'])


class ConfigurationTests(unittest.TestCase):
    def test_compare_and_swap_preserves_history_and_rejects_stale_writer(self):
        import tempfile
        from pathlib import Path
        from forge.store.db import Store
        with tempfile.TemporaryDirectory() as base:
            store=Store.create(Path(base)/'store');config=Configuration(store)
            self.assertEqual(config.replace('runtime',{'enabled':False},expected_revision=0),1)
            self.assertEqual(config.replace('runtime',{'enabled':True},expected_revision=1),2)
            with self.assertRaises(ConfigurationConflict):config.replace('runtime',{'enabled':False},expected_revision=1)
            self.assertTrue(config.get('runtime')['settings']['enabled'])
            with store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM configuration').fetchone()[0],2)


class AuxiliaryCapacityTests(unittest.TestCase):
    def test_pipeline_discussion_and_setup_share_capacity_until_verified_cleanup(self):
        from tests import test_store_schema as fixtures
        fixture=fixtures.StoreSchemaTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        db=fixture.db
        from forge.store.remote_time import register
        register(db)
        for path in sorted(fixtures.SCHEMA.parent.glob('*.sql'))[1:]:db.executescript(path.read_text())
        fixture.insert('auxiliary_executions',id='probe',domain='setup',owner_id='live',position=0,phase='reserved',fenced=0,cleanup=0)
        fixture.insert('auxiliary_leases',slot='shared',execution_id='probe',execution_fence='probe-fence',acquired_at=1)
        fixture.insert('discussions',id='discussion',title='',created_at=1,updated_at=1)
        fixture.insert('discussion_executions',id='chat',discussion_id='discussion',phase='reserved',created_at=1)
        with self.assertRaises(sqlite3.IntegrityError):
            fixture.insert('job_leases',slot='shared',job_id='ja',execution_fence='job-fence',acquired_at=1)
        with self.assertRaises(sqlite3.IntegrityError):
            fixture.insert('discussion_leases',slot='shared',execution_id='chat',execution_fence='chat-fence',acquired_at=1)
        with self.assertRaises(sqlite3.IntegrityError):db.execute("DELETE FROM auxiliary_leases WHERE execution_id='probe'")
        db.execute("UPDATE auxiliary_executions SET fenced=1,cleanup=1,phase='cleaned' WHERE id='probe'")
        db.execute("DELETE FROM auxiliary_leases WHERE execution_id='probe'")
        fixture.insert('job_leases',slot='shared',job_id='ja',execution_fence='job-fence',acquired_at=2)
        fixture.insert('auxiliary_executions',id='next-probe',domain='setup',owner_id='live',position=1,phase='reserved',fenced=0,cleanup=0)
        with self.assertRaises(sqlite3.IntegrityError):
            fixture.insert('auxiliary_leases',slot='shared',execution_id='next-probe',execution_fence='next-fence',acquired_at=2)


if __name__=='__main__':unittest.main()
