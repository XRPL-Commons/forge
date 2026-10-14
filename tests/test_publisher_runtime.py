from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge.factory.publisher_runtime import PublicationRuntime, publication_authorization, publication_access
from forge.factory.result_controller import ResultController
from forge.factory.contracts import result
from forge.publication_artifacts import PublicationClaim
from forge.store.models import Candidate, TransitionError
from tests import test_publications as ledger, test_publisher as driver


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.job=dict(id='job',role='build',repo_id=1,repo='owner/repo',branch='forge/issue-1',target_branch='main',
                      input_manifest={'base_oid':ledger.BASE})
        self.bot={'id':42,'login':'controller[bot]','type':'Bot'}
        self.github=SimpleNamespace(identity=lambda role,repo_id:self.bot,publisher=lambda role:role+'er[bot]' if role=='build' else 'controller[bot]')
    def test_new_target_uses_host_controller_identity(self):
        value=publication_authorization(self.job,self.github)
        self.assertEqual(value,dict(kind='forge_owned',branch='forge/issue-1',expected_head_oid=None,pull_number=None,author=self.bot,replace_ref=False))
    def test_nonpublishing_roles_have_no_publication_authority(self):
        self.assertEqual(publication_authorization({**self.job,'role':'review'},self.github),{})
        self.assertEqual(publication_authorization({**self.job,'builder_stage':'planning'},self.github),{})
    def existing(self,author=None):
        author=author or {'id':99,'login':'builder[bot]','type':'Bot'}
        self.job.update(resume_pr=7,resume_head=ledger.HEAD,publication_author=author)
        pr=dict(number=7,state='open',draft=False,merged=False,user=author,
                head={'sha':ledger.HEAD,'ref':self.job['branch'],'repo':{'id':1}},
                base={'sha':ledger.BASE,'ref':'main','repo':{'id':1}})
        self.github.pr=lambda job,number:pr
        return pr
    def test_existing_target_requires_exact_head_author_and_base(self):
        pr=self.existing();self.assertTrue(publication_authorization(self.job,self.github)['replace_ref'])
        pr['head']['sha']='f'*40
        with self.assertRaises(PublicationClaim):publication_authorization(self.job,self.github)
    def test_human_target_needs_explicit_pinned_transfer(self):
        author={'id':55,'login':'human','type':'User'};self.existing(author)
        with self.assertRaises(PublicationClaim):publication_authorization(self.job,self.github)
        self.job['input_manifest'].update(publication_target={'kind':'existing','candidate_id':'c','ownership_transfer':True,'author':author},
            existing_candidates=[{'candidate_id':'c','pull_number':7,'head_oid':ledger.HEAD,'branch':self.job['branch'],'author':author}])
        self.assertEqual(publication_authorization(self.job,self.github)['kind'],'ownership_transfer')


class ResultPublicationTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ledger.PublicationLedgerTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;ledger.bind_source(f)
        self.github=driver.GitHub();self.controller=f.fixture.controller
        self.publisher=driver.Publisher(f.store,self.github,lambda job:f.output,
            signing_capability=lambda job:{'app_id':42,'strategy':'rest','qualified':True},clock=lambda:f.now)
        def ensure(job,report):
            value=self.publisher.ensure(job,f.output,f.target,report)
            f.fixture.github.pr_value=deepcopy(self.github.pull)
            return value
        self.controller.publisher=SimpleNamespace(ensure=ensure)
        f.fixture.github.publication=lambda *args:self.fail('Source-bound result used worker publication marker')
    def test_source_bound_candidate_promotes_from_retained_receipt(self):
        f=self.fixture;value=self.controller.step(f.job)
        self.assertTrue(value['accepted'],value)
        with f.store.read_snapshot() as db:
            row=db.execute("SELECT details_json FROM candidate_evidence WHERE kind='publication'").fetchone()
            self.assertIn('publication_receipt_id',row[0])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries WHERE kind=\'charge\'').fetchone()[0],0)
    def test_receipt_is_required_even_if_caller_supplies_verified_booleans(self):
        f=self.fixture;job=f.fixture.queries.job(f.job)
        # Use the actual requirement and manifest hashes so the receipt guard
        # is the sole missing authority.
        from forge.store.transitions import manifest_hash
        candidate=Candidate(7,f.target['branch'],ledger.HEAD,ledger.BASE,f.fixture.manifest['policy_hash'],
                            manifest_hash(job['requirements']),manifest_hash(job['input_manifest']))
        with self.assertRaisesRegex(TransitionError,'confirmed receipt'):
            f.fixture.life.finish_job(f.fixture.command(),f.job,execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],
                candidate=candidate,evidence={'verified':True,'pull_number':7,'head_oid':ledger.HEAD,'base_oid':ledger.BASE})
    def test_blocked_publication_keeps_capture_uncharged_and_scheduled(self):
        f=self.fixture;self.controller.publisher=SimpleNamespace(ensure=lambda *args:{'status':'blocked','claim':{'code':'git_lfs_unsupported'}})
        value=self.controller.step(f.job);self.assertEqual(value['pending'],'publication_recovery')
        self.assertEqual(value['publication_claim']['code'],'git_lfs_unsupported')
        self.assertIsNone(f.fixture.queries.job(f.job)['outcome']);self.assertEqual(f.fixture.charges(),0)
        with f.store.read_snapshot() as db:self.assertEqual(db.execute("SELECT reason FROM next_actions WHERE owner='results'").fetchone()[0],'publication_recovery')
    def test_changed_base_retains_controller_publication_for_new_assignment(self):
        f=self.fixture;original=self.controller.publisher.ensure
        def publish_then_base_moves(job,report):
            value=original(job,report)
            f.fixture.github.pr_value['base']['sha']='f'*40
            return value
        self.controller.publisher.ensure=publish_then_base_moves
        value=self.controller.step(f.job);self.assertEqual(value['pending'],'base_reconciliation')
        self.assertTrue(f.fixture.queries.job(f.job)['result_revoked']);self.assertEqual(f.fixture.charges(),0)
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM publication_receipts').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0],0)

    def test_missing_runtime_cannot_fall_back_to_marker(self):
        self.controller.publisher=None;value=self.controller.step(self.fixture.job)
        self.assertEqual(value['publication_claim']['code'],'publication_runtime_unavailable')
    def test_invalid_publishable_text_is_rejected_before_publication(self):
        job=self.fixture.fixture.queries.job(self.fixture.job)
        with self.assertRaises(ValueError):result({**self.fixture.report,'pr_title':None},job)
    def test_default_runtime_stops_before_artifact_or_app_mutations(self):
        f=self.fixture;runtime=PublicationRuntime(f.store,self.github,SimpleNamespace())
        value=runtime.ensure(f.fixture.queries.job(f.job),f.report)
        self.assertEqual(value['claim']['code'],'publication_signing_unqualified');self.assertEqual(self.github.writes,[])


class IntegratedPublicationTests(unittest.TestCase):
    def test_combined_tree_becomes_one_receipt_backed_candidate(self):
        import hashlib
        from forge.factory import source_inputs
        from forge.publication_artifacts import BaseObjects, inspect_output
        from tests import test_delivery_pipeline as pipeline
        f=pipeline.DeliveryPipelineTests();f.setUp();self.addCleanup(f.doCleanups)
        job=f.integrate();inputs=[row['commit_oid'] for row in f.jobs.input_refs(job)]
        for index,oid in enumerate(inputs):f.git('update-ref','refs/heads/input-'+str(index),oid)
        snapshot=f.root/'host.git';f.git('clone','--quiet','--bare',str(f.repo),str(snapshot),root=True)
        f.git('checkout','--quiet','--detach',f.base)
        for oid in inputs:f.git('merge','--quiet','--no-ff','-m','integrate',oid)
        head=f.git('rev-parse','HEAD').strip();bundle=f.root/'integrated.bundle'
        f.git('bundle','create',str(bundle),'HEAD','^'+f.base);content=bundle.read_bytes()
        source=source_inputs.create(repository_id=1,canonical_repository='example/repo',base_oid=f.base,input_oids=inputs,
            manifest=job['input_manifest'],dependency_policy_digest='1'*64,notes={})
        authority=dict(kind='forge_owned',branch=job['branch'],expected_head_oid=None,pull_number=None,
                       author={'id':42,'login':'controller[bot]','type':'Bot'},replace_ref=False)
        target=dict(repository_id=1,repository='example/repo',branch=job['branch'],base_branch='main',expected_head_oid=None,
            pull_number=None,app_id=42,strategy='rest',signing_qualified=True,authorization=authority)
        fixture=SimpleNamespace(store=f.store,source=source,job=job['id'],launch=job,target=target)
        ledger.bind_source(fixture)
        report=dict(version=1,attempt=job['id'],role='build',outcome='pass',summary='Combined change',delegation_plan=None,
                    output_commit=head,pr_title='Combined change',pr_body='Fixes #1\nFixes #2')
        export=dict(version=1,job_id=job['id'],input_manifest_hash=source['manifest_digest'],base_oid=f.base,local_head_oid=head,
                    object_format='sha1',source_digest=source_inputs.digest(source),bundle_digest=hashlib.sha256(content).hexdigest(),bundle_size=len(content))
        f.executions.collect(f.command(),job['id'],execution_fence=job['execution_fence'],capture={'result':report,'logs':'','worktree_bundle':content,'export_receipt':export})
        f.executions.verify_cleanup(f.command(),job['id'],execution_fence=job['execution_fence'],evidence={'process_stopped':True,'resources_released':True})
        output=inspect_output(content,head,source,BaseObjects(1,source_inputs.digest(source),'sha1',tuple([f.base,*inputs]),snapshot))
        github=driver.GitHub();github.base=f.base
        from contextlib import contextmanager
        from forge.mirror.policy import Repository
        @contextmanager
        def base_objects(repository,pins,source_digest):
            yield BaseObjects(repository.repository_id,source_digest,'sha1',tuple(pins),snapshot)
        preparation=SimpleNamespace(access=SimpleNamespace(resolve=lambda _:Repository(1,'example/repo',1,'Organization',43,actual_access=True)),
            git_store=SimpleNamespace(base_objects=base_objects))
        f.results.publisher=PublicationRuntime(f.store,github,preparation,signing_capability=lambda _:{'app_id':42,'strategy':'rest','qualified':True},clock=lambda:f.now)
        f.github.pr.side_effect=lambda item,number:github.call('controller',f.jobs.job(job['id']),'GET','/pulls/'+str(number))
        f.github.publication.side_effect=AssertionError('No worker publication authority')
        value=f.results.step(job['id']);self.assertTrue(value['accepted'],value)
        with f.store.read_snapshot() as db:
            candidate=db.execute('SELECT head_oid FROM candidates WHERE id=?',(value['candidate_id'],)).fetchone()
            self.assertEqual(candidate[0],github.head);self.assertNotEqual(candidate[0],head)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM publication_receipts').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='batch'").fetchone()[0],'reviewing')


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.fixture=ledger.PublicationLedgerTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
    def test_access_repair_retains_attempts_and_unknown_commit_cannot_retry(self):
        f=self.fixture;identifier=f.queue();commands=f.commands
        commands.prepare_step(identifier,'commit',{'tree':'d'*40});commands.claim_step(identifier,'commit')
        commands.block(identifier,{'code':'publication_refused','step':'commit','http_status':403})
        proof=dict(source='host_app_installation',repository_id=1,app_id=42,source_input_digest=f.output.identity()['source_input_digest'],
            output_digest=f.output.digest(),strategy='rest',signing_qualified=True,permissions={'contents':'write','pull_requests':'write'},observed_at=100)
        commands.recheck(f.fixture.command(),identifier,evidence=proof)
        self.assertEqual(commands.get(identifier)['steps']['commit']['attempts'],1)
        self.assertEqual(commands.get(identifier)['status'],'pending')
        commands.claim_step(identifier,'commit');commands.block(identifier,{'code':'publication_uncertain','step':'commit'})
        with self.assertRaises(TransitionError):commands.recheck(f.fixture.command(),identifier,evidence=proof)
    def test_preintent_claim_is_deduplicated_and_durable(self):
        f=self.fixture;claim={'code':'git_lfs_unsupported','paths':['.gitattributes']}
        self.assertEqual(f.commands.remember_claim(f.job,claim),f.commands.remember_claim(f.job,claim))
        with f.store.read_snapshot() as db:
            rows=list(db.execute("SELECT payload_json FROM decisions WHERE command='publication_claim'"))
            self.assertEqual(len(rows),1);self.assertIn('git_lfs_unsupported',rows[0][0])

    def test_capability_repair_does_not_clear_unknown_commit_write(self):
        f=self.fixture;identifier=f.queue();commands=f.commands
        commands.prepare_step(identifier,'commit',{'tree':'d'*40});commands.claim_step(identifier,'commit')
        commands.block(identifier,{'code':'publication_signing_unqualified'})
        proof=dict(source='host_app_installation',repository_id=1,app_id=42,source_input_digest=f.output.identity()['source_input_digest'],
            output_digest=f.output.digest(),strategy='rest',signing_qualified=True,permissions={'contents':'write','pull_requests':'write'},observed_at=100)
        commands.recheck(f.fixture.command(),identifier,evidence=proof)
        self.assertEqual(commands.get(identifier)['steps']['commit']['status'],'sending')
        with self.assertRaises(TransitionError):commands.claim_step(identifier,'commit')


class SigningCapabilityTests(unittest.TestCase):
    def setUp(self):
        from forge.store.db import Store
        from forge.store.configuration import Configuration
        from forge.factory.publisher_runtime import SigningCapabilities
        import tempfile
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.store=Store.create(directory.name);self.configuration=Configuration(self.store)
        self.connection={'app_id':42,'installation_id':43}
        self.reader=SigningCapabilities(self.store,SimpleNamespace(connection=lambda *args:self.connection))
        self.job={'repo_id':1}
        self.proof=dict(version=1,repository_id=1,app_id=42,installation_id=43,probe_repository_id=2,strategy='graphql',
            commit_oid='a'*40,tree_oid='b'*40,verified=True,exact_ref=True,signing_rule_accepted=True,observed_at=100)
    def save(self,proof):
        row=self.configuration.get('publication_signing','1')
        self.configuration.replace('publication_signing',proof,subject='1',expected_revision=row['revision'] if row else 0)
    def test_absent_or_incomplete_proof_cannot_select_strategy(self):
        self.assertFalse(self.reader(self.job)['qualified'])
        for field,value in [('verified',False),('exact_ref',False),('signing_rule_accepted',False),('tree_oid',''),('repository_id',2)]:
            self.save({**self.proof,field:value})
            self.assertFalse(self.reader(self.job)['qualified'],field)
    def test_matching_proof_survives_restart_but_not_app_installation_change(self):
        self.save(self.proof)
        self.assertEqual(self.reader(self.job)['strategy'],'graphql')
        self.assertTrue(self.reader(self.job)['qualified'])
        self.connection['installation_id']=99
        self.assertFalse(self.reader(self.job)['qualified'])


class DraftAuthorizationTests(AuthorizationTests):
    def test_exact_owned_draft_has_explicit_repair_claim(self):
        pr=self.existing();pr['draft']=True
        with self.assertRaises(PublicationClaim) as raised:publication_authorization(self.job,self.github)
        self.assertEqual(raised.exception.code,'publication_draft_requires_ready')
        pr['head']['sha']='f'*40
        with self.assertRaises(PublicationClaim) as raised:publication_authorization(self.job,self.github)
        self.assertEqual(raised.exception.code,'publication_target_changed')


class ObservedFactsTests(ResultPublicationTests):
    def test_review_receives_only_exact_confirmed_publication_facts(self):
        from dataclasses import replace
        from forge.factory.contracts import assignment_note
        f=self.fixture
        facts={'protected_paths':['.github/workflows/check.yml'],'deleted_tests':['tests/test_login.py'],
               'added_test_markers':[{'path':'tests/test_api.py','markers':['skip']}]}
        f.output=replace(f.output,facts=facts)
        accepted=self.controller.step(f.job);self.assertTrue(accepted['accepted'],accepted)
        review=f.fixture.reserve('review');job=f.fixture.queries.job(review)
        observed=job['observed_by_forge']
        candidate_id=f.fixture.view()['current_candidate_id']
        self.assertEqual(observed['candidate_id'],candidate_id)
        self.assertEqual(observed['facts'],facts)
        note=assignment_note(job)
        self.assertEqual(note['observed_by_forge'],observed)
        self.assertIn('Observed by Forge',note['instructions'])
        # A caller cannot apply this receipt to a changed head.
        from forge.store.executions import observed_facts_in
        with f.store.read_snapshot() as db:
            candidate=dict(db.execute('SELECT * FROM candidates WHERE id=?',(candidate_id,)).fetchone())
            with self.assertRaisesRegex(TransitionError,'do not match'):
                observed_facts_in(db,job,{**candidate,'head_oid':'f'*40})
    def test_legacy_candidate_has_no_invented_host_facts(self):
        from tests.test_store_result_controller import ResultControllerTests
        self.fixture.patch.stop()
        f=ResultControllerTests();f.setUp();self.addCleanup(f.doCleanups)
        build=f.reserve();f.publish(build);f.capture(build,f.report(build))
        self.assertTrue(f.controller.step(build)['accepted'])
        review=f.reserve('review')
        self.assertIsNone(f.queries.job(review)['observed_by_forge'])
