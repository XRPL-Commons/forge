import json
import unittest

from forge.factory.config import validate, policy
from forge.factory.observation_controller import ObservationController
from forge.factory.reconciliation_controller import ReconciliationController
from forge.store.migration.importer import Importer
from forge.store.models import Command, TransitionError
from forge.store.transitions import Lifecycle, manifest_hash, project_unit
from tests import test_store_import as fixtures


class GitHub:
    def __init__(self):
        self.remote=dict(number=1,state='open',title='Change',body='Fresh complete body',updated_at='2026-10-08T00:00:00Z',labels=[],assignees=[])
        self.requirements='requirements-hash';self.calls=0;self.runs=[]
        self.pull=dict(number=12,state='open',merged=False,draft=False,head=dict(sha='head',ref='work',repo={'id':1}),base=dict(sha='base',ref='main',repo={'id':1}))
    def issue(self,item):self.calls+=1;return self.remote.copy(),self.requirements
    def pulls(self,item):return []
    def target(self,item):return 'main'
    def protections(self,item,candidate):return True
    def pr(self,item,number):return self.pull
    def call(self,*args):return {'commit':{'sha':'base'}}
    def pages(self,*args):return self.runs
    def connection(self,role):return {'app_id':10 if role=='review' else 11}


class ReconciliationTests(unittest.TestCase):
    setUp=fixtures.ImportTests.setUp
    write=fixtures.ImportTests.write
    snapshot=fixtures.ImportTests.snapshot
    unit='legacy:unit:1:1'
    def setup_store(self,*,authorized=True,candidate=False,phase='built'):
        self.config=validate({'enabled':True,'image':'sha256:'+'1'*64})
        if candidate:
            self.issue['state']=phase
            self.issue['candidate']=dict(pr=12,head='head',base='base',policy=manifest_hash(policy(self.config)),requirements='requirements-hash',key='identity',build_attempt='build',accepted_at=12,branch='work')
            self.ledger['jobs']['build']=dict(id='build',issue_key='1:1',role='build',cleanup=True,fenced=True,slot='slot',created_at=11,phase='cleaned',outcome='accepted',branch='work',target_branch='main')
        importer=Importer(self.snapshot(),self.base/'staging');importer.run();self.store=importer.store
        self.now=2000000000.0
        with self.store.write() as db:
            db.execute("UPDATE store_metadata SET value='false' WHERE key='import_only'")
            if authorized:
                db.execute("INSERT INTO runtime_operations VALUES('activation','activate','complete','source','manifest',1,1,'{}')")
                db.execute("INSERT INTO store_metadata VALUES('migration_reconciliation_authorization',?)",(json.dumps(dict(operation_id='activation',source_manifest='manifest',actor_id='operator:cli:0')),))
        self.github=GitHub()
        self.controller=ReconciliationController(self.store,self.github,config=self.config,admission=lambda item:True,clock=lambda:self.now)
        return self.store
    def view(self):
        with self.store.read_snapshot() as db:return project_unit(db,self.unit)
    def test_fresh_issue_rebinds_requirements_without_budget_or_hold_changes(self):
        self.setup_store()
        life=Lifecycle(self.store,clock=lambda:self.now)
        life.pause(Command('pause','operator',self.unit,self.view()['version']))
        before=self.view()
        result=self.controller.step(self.unit)
        self.assertTrue(result['reconciled']);after=self.view()
        self.assertEqual(after['reason_code'],'operator_pause');self.assertEqual(after['charged_failures'],before['charged_failures'])
        self.assertEqual(after['budget_generation'],before['budget_generation'])
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT r.* FROM unit_issues m JOIN issue_revisions r ON r.id=m.requirements_revision_id WHERE m.unit_id=?',(self.unit,)).fetchone()
            self.assertEqual(json.loads(row['requirements_json']),'requirements-hash')
            self.assertEqual(row['body'],'Fresh complete body')
        self.assertEqual(self.github.calls,1)
    def test_no_activation_authority_never_reads_github_or_clears_boundary(self):
        self.setup_store(authorized=False)
        self.assertEqual(self.controller.step(self.unit)['pending'],'migration_authorization_required')
        self.assertEqual(self.github.calls,0);self.assertEqual(self.view()['reason_code'],'legacy_reconciliation')
    def test_ordinary_observer_preserves_imported_candidate_until_reconciled(self):
        self.setup_store(candidate=True)
        observer=ObservationController(self.store,self.github,admission=lambda item:True,clock=lambda:self.now)
        self.assertEqual(observer.step(self.unit)['pending'],'legacy_reconciliation')
        self.assertEqual(self.github.calls,0);self.assertEqual(self.view()['current_candidate_id'],'legacy:candidate:identity')
    def test_same_candidate_rebound_with_independent_verification_gate(self):
        self.issue['verification_request']=dict(candidate='identity',ci=[],what='Confirm behavior',created_at=13,resume_state='built')
        self.setup_store(candidate=True)
        result=self.controller.step(self.unit)
        self.assertTrue(result['reconciled']);self.assertNotEqual(result['candidate_id'],'legacy:candidate:identity')
        self.assertEqual(self.view()['reason_code'],'verification_human')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],2)
            selected=db.execute('SELECT * FROM candidates WHERE id=?',(result['candidate_id'],)).fetchone()
            self.assertEqual(selected['requirements_hash'],manifest_hash('requirements-hash'))
            self.assertEqual(selected['head_oid'],'head')
    def test_changed_requirements_restarts_build_without_resetting_budget_or_hold(self):
        self.setup_store(candidate=True)
        Lifecycle(self.store,clock=lambda:self.now).pause(Command('pause','operator',self.unit,self.view()['version']))
        before=self.view();self.github.requirements='changed'
        result=self.controller.step(self.unit)
        self.assertEqual(result['invalidated'],'requirements_changed')
        self.assertEqual(self.view()['state'],'building');self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.view()['reason_code'],'operator_pause')
        self.assertEqual(self.view()['budget_generation'],before['budget_generation'])
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],1)
    def test_changed_head_requires_known_unmerged_state_before_rework(self):
        self.setup_store(candidate=True)
        self.github.pull['head']['sha']='changed';self.github.pull['merged']=None
        self.assertEqual(self.controller.step(self.unit)['pending'],'legacy_candidate_unverified')
        self.assertEqual(self.view()['current_candidate_id'],'legacy:candidate:identity')
        self.github.pull['merged']=False
        self.assertEqual(self.controller.step(self.unit)['invalidated'],'candidate_changed')
    def test_completed_elsewhere_is_done_without_fabricated_merge(self):
        self.setup_store(candidate=True)
        self.github.remote.update(state='closed',state_reason='completed')
        result=self.controller.step(self.unit)
        self.assertEqual(result['close_code'],'github_completed')
        self.assertEqual(self.view()['members'][0]['projected_state'],'done')
        self.assertIsNone(self.view()['merge_sha'])
    def test_not_planned_is_dropped_and_unknown_closure_stays_gated(self):
        self.setup_store()
        self.github.remote.update(state='closed',state_reason=None)
        self.assertEqual(self.controller.step(self.unit)['pending'],'legacy_closure_provenance_unknown')
        self.github.remote['state_reason']='not_planned'
        self.assertTrue(self.controller.step(self.unit)['closed'])
        self.assertEqual(self.view()['members'][0]['projected_state'],'dropped')
    def test_closed_issue_with_merged_candidate_is_not_falsely_closed(self):
        self.setup_store(candidate=True)
        self.github.remote.update(state='closed',state_reason='completed');self.github.pull['merged']=True
        self.assertEqual(self.controller.step(self.unit)['pending'],'legacy_merge_receipt_unverified')
        self.assertEqual(self.view()['state'],'reviewing')
    def test_verified_technical_check_is_required_to_keep_completed_review(self):
        self.issue['evidence']={'review':dict(candidate='identity',outcome='pass',check_id=21)}
        self.setup_store(candidate=True,phase='reviewed')
        self.assertEqual(self.controller.step(self.unit)['pending'],'legacy_review_check_unavailable')
        self.github.runs=[dict(id=21,name='forge/review',app={'id':10},head_sha='head',status='completed',conclusion='success')]
        result=self.controller.step(self.unit);self.assertTrue(result['reconciled'])
        with self.store.read_snapshot() as db:
            evidence=json.loads(db.execute("SELECT details_json FROM candidate_evidence WHERE candidate_id=? AND kind='review'",(result['candidate_id'],)).fetchone()[0])
            self.assertEqual(evidence['check_id'],21);self.assertEqual(evidence['migration_verification']['app_id'],10)
        self.assertEqual(self.view()['state'],'auditing')
    def test_latest_check_replacement_blocks_old_accepted_evidence(self):
        self.issue['evidence']={'review':dict(candidate='identity',outcome='pass',check_id=21)}
        self.setup_store(candidate=True,phase='reviewed')
        self.github.runs=[dict(id=22,name='forge/review',app={'id':10},head_sha='head',status='completed',conclusion='success')]
        self.assertEqual(self.controller.step(self.unit)['pending'],'legacy_review_check_changed')
    def test_pending_investigation_is_never_invented_complete(self):
        self.issue['state']='investigating'
        self.setup_store()
        with self.assertRaisesRegex(TransitionError,'no accountable failure'):
            self.controller.step(self.unit)
        self.assertEqual(self.view()['reason_code'],'legacy_reconciliation')
    def test_exact_imported_remote_merge_records_receipt_without_a_new_merge(self):
        self.setup_store(candidate=True)
        self.github.remote.update(state='closed',state_reason='completed')
        self.github.pull.update(state='closed',merged=True,merge_commit_sha='merged-exact')
        answer=self.controller.step(self.unit)
        self.assertTrue(answer['merged']);self.assertEqual(self.view()['merge_sha'],'merged-exact')
        self.assertEqual(self.view()['members'][0]['projected_state'],'done')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM external_effects").fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidate_evidence WHERE kind='merge'").fetchone()[0],1)

    def test_exact_external_merge_of_candidate_waiting_for_rework_is_recorded(self):
        self.setup_store(candidate=True,phase='ready')
        self.github.pull.update(state='closed',merged=True,merge_commit_sha='already-merged')
        self.assertTrue(self.controller.step(self.unit)['merged'])
        self.assertEqual(self.view()['merge_sha'],'already-merged')

    def test_generic_gate_resolution_cannot_release_migration_authority(self):
        self.setup_store()
        with self.store.read_snapshot() as db:gate=db.execute("SELECT id FROM unit_gates WHERE code='legacy_reconciliation'").fetchone()[0]
        with self.assertRaises(TransitionError):
            Lifecycle(self.store,clock=lambda:self.now).resolve_gate(Command('unsafe','operator',self.unit,self.view()['version']),gate,release='reconciliation',evidence={'verified':True})
