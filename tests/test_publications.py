import hashlib
import json
import sqlite3
import unittest
from unittest.mock import patch

from forge.factory.source_inputs import create, digest
from forge.publication_artifacts import VerifiedOutput
from forge.store.executions import ExecutionQueries
from forge.store.models import IdempotencyConflict, TransitionError
from forge.store.publications import Publications
from forge.store.executions import _artifact
from tests import test_store_result_controller as fixtures
BASE,HEAD=fixtures.BASE,fixtures.HEAD


class PublicationLedgerTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.ResultControllerTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;self.store=f.store;self.now=100
        self.job=f.reserve();self.launch=f.queries.job(self.job)
        self.source=create(repository_id=1,canonical_repository='owner/repo',base_oid=BASE,input_oids=[],
            manifest=f.manifest,dependency_policy_digest='1'*64,notes={})
        self.output=VerifiedOutput(self.source,hashlib.sha256(b'fixture').hexdigest(),7,HEAD,'c'*40,'d'*40,(),{})
        self.report=f.report(self.job,pr_title='Change',pr_body='Details',output_commit=HEAD)
        export=dict(version=1,job_id=self.job,input_manifest_hash=self.source['manifest_digest'],base_oid=BASE,
            local_head_oid=HEAD,object_format='sha1',source_digest=digest(self.source),bundle_digest=self.output.bundle_digest,bundle_size=7)
        f.capture(self.job,self.report,export_receipt=export,bundle={'digest':self.output.bundle_digest,'size':7,'chunks':1})
        with self.store.write() as db:_artifact(db,self.job,'worktree_bundle',b'fixture',100,sequence=0)
        original=ExecutionQueries.job
        def projection(query,job_id):
            job=original(query,job_id)
            if job_id==self.job:job['source_input']=self.source
            return job
        self.patch=patch.object(ExecutionQueries,'job',projection);self.patch.start();self.addCleanup(self.patch.stop)
        self.target=dict(repository_id=1,repository='owner/repo',branch=self.launch['branch'],base_branch='main',
            expected_head_oid=None,pull_number=None,app_id=42,strategy='rest',signing_qualified=True,authorization=None)
        self.commands=Publications(self.store,clock=lambda:self.now,reliability={'effect_max_attempts':2,'effect_uncertainty_seconds':60})

    def queue(self,**changes):
        values=dict(execution_fence=self.launch['execution_fence'],input_manifest=self.fixture.manifest,
                    output=self.output,target=self.target,report=self.report);values.update(changes)
        return self.commands.queue(self.fixture.command(),self.job,**values)['intent_id']

    def test_queue_exact_capture_replay_and_immutable_identity(self):
        identifier=self.queue();self.assertEqual(self.queue(),identifier)
        with self.store.write() as db:
            with self.assertRaises(sqlite3.IntegrityError):db.execute("UPDATE publication_intents SET output_digest=? WHERE id=?",('0'*64,identifier))
        with self.assertRaises(TransitionError):self.queue(report={**self.report,'pr_body':'Different'})
        with self.assertRaises(TransitionError):self.queue(target={**self.target,'branch':'elsewhere'})

    def test_missing_strategy_or_head_cannot_queue(self):
        with self.assertRaises(TransitionError):self.queue(target={**self.target,'signing_qualified':False})
        with self.assertRaises(TransitionError):self.queue(report={**self.report,'output_commit':'f'*40})

    def test_send_receipt_is_durable_and_unsafe_retry_is_refused(self):
        identifier=self.queue();self.commands.prepare_step(identifier,'commit',{'tree':'d'*40})
        self.commands.claim_step(identifier,'commit')
        self.now+=31
        with self.assertRaises(TransitionError):self.commands.claim_step(identifier,'commit')
        self.commands.uncertain(identifier,'commit');self.assertEqual(self.commands.get(identifier)['status'],'uncertain')
        self.now+=31;self.commands.uncertain(identifier,'commit')
        self.assertEqual(self.commands.get(identifier)['status'],'blocked')

    def test_content_addressed_retry_has_backoff_and_attempt_bound(self):
        identifier=self.queue();self.commands.prepare_step(identifier,'blob',{'sha':HEAD})
        self.commands.claim_step(identifier,'blob')
        with self.assertRaises(TransitionError):self.commands.claim_step(identifier,'blob',repeat_safe=True)
        self.now+=31;self.commands.claim_step(identifier,'blob',repeat_safe=True)
        self.commands.uncertain(identifier,'blob');self.assertEqual(self.commands.get(identifier)['status'],'blocked')

    def test_request_and_receipt_cannot_be_replaced(self):
        identifier=self.queue();self.commands.prepare_step(identifier,'ref',{'before':BASE})
        with self.assertRaises(IdempotencyConflict):self.commands.prepare_step(identifier,'ref',{'before':HEAD})
        self.commands.claim_step(identifier,'ref');self.commands.record_step(identifier,'ref',{'sha':HEAD})
        with self.assertRaises(IdempotencyConflict):self.commands.record_step(identifier,'ref',{'sha':BASE})

    def test_confirmation_requires_tree_signature_pr_ref_and_freshness(self):
        identifier=self.queue();value=self.commands.get(identifier)
        receipt=dict(intent_id=identifier,job_id=self.job,output_digest=self.output.digest(),repository_id=1,
            branch=self.target['branch'],base_oid=BASE,tree_oid=self.output.tree_oid,head_oid=HEAD,app_id=42,
            bundle_digest=self.output.bundle_digest,source_input_digest=value['output']['source_input_digest'],
            facts_digest=value['output']['facts_digest'],verified=True,exact_ref=True,exact_pr=True,pull_number=7,observed_at=100)
        for field,value in [('verified',False),('tree_oid',HEAD),('exact_pr',False),('observed_at',0)]:
            with self.assertRaises(TransitionError):self.commands.confirm(identifier,{**receipt,field:value})
        self.commands.confirm(identifier,receipt);self.commands.confirm(identifier,receipt)
        self.assertEqual(self.commands.get(identifier)['receipt'],receipt)
