"""Review-only check authority never becomes merge authority."""
import copy
import json
import tempfile
import unittest
from unittest.mock import Mock

from forge.factory.config import validate, policy
from forge.factory.contracts import digest
from forge.factory.effect_controller import EffectController
from forge.factory.merge_controller import MergeAdmission, MergeController, candidate_snapshot
from forge.store.db import Store
from forge.store.effects import Effects, EffectQueries
from forge.store.models import Command, TransitionError
from forge.store.review_pulls import ReviewPullRequests, normalize_source
from forge.store.transitions import Lifecycle


class ReviewPolicyTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        self.store=Store.create(directory.name);self.now=1000;self.serial=0
        self.config=validate({'enabled':True,'image':'example/forge@sha256:'+'a'*64,'merge_enabled':False,'human_required':True})
        self.policy=digest(policy(self.config,1,repository_name='owner/repo'))
        self.effects=Effects(self.store,clock=lambda:self.now)
        self.observer=ReviewPullRequests(self.store,clock=lambda:self.now)
        self.github=Mock();self.remotes={};self.checks={}
        self.github.pr.side_effect=lambda item,number:copy.deepcopy(self.remotes[number])
        self.github.gates.return_value=True
        self.github.check.side_effect=self.check
        self.admission=MergeAdmission(self.store,self.github,config=lambda:self.config,
            repository_ready=lambda repo:False,review_repository_ready=lambda repo:True,clock=lambda:self.now)
        self.driver=EffectController(self.store,self.github,admission=self.admission,clock=lambda:self.now)
        with self.store.write() as db:db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'owner/repo')")

    def check(self,item,role,candidate,external_id,conclusion,summary,**options):
        if options.get('observe_only'):return self.checks.get(external_id)
        return self.checks.setdefault(external_id,len(self.checks)+1)

    def command(self,unit):
        self.serial+=1
        with self.store.read_snapshot() as db:version=db.execute('SELECT version FROM units WHERE id=?',(unit,)).fetchone()[0]
        return Command('test:'+str(self.serial),'operator',unit,version)

    def admit(self,number=7):
        self.remotes[number]=dict(number=number,title='Human change',body='Expected behavior',state='open',draft=False,
            merged=False,user={'id':44,'login':'alice','type':'User'},labels=[],
            head={'sha':'a'*40,'ref':'feature-'+str(number),'repo':{'id':2,'full_name':'alice/fork'}},
            base={'sha':'b'*40,'ref':'main','repo':{'id':1,'full_name':'owner/repo'}})
        row=self.observer.observe(normalize_source(self.remotes[number],repo_id=1,repo='owner/repo'),policy_hash=self.policy)
        with self.store.read_snapshot() as db:
            row['effect_id']=db.execute('SELECT id FROM external_effects WHERE unit_id=?',(row['unit_id'],)).fetchone()[0]
        return row

    def passed(self,row):
        self.driver.step(row['effect_id'])
        # Fixture represents independently accepted verdicts; composed pipeline tests
        # exercise actual worker capture, publication and result acceptance.
        with self.store.write() as db:
            db.execute("UPDATE units SET state='auditing' WHERE id=?",(row['unit_id'],))
            db.execute("UPDATE units SET state='merging' WHERE id=?",(row['unit_id'],))
            for role in ('review','audit'):
                db.execute('INSERT INTO candidate_evidence(id,unit_id,candidate_id,kind,verdict,actor_id,observed_at,details_json) '
                    'VALUES(?,?,?,?,?,?,?,?)',(row['candidate_id']+role,row['unit_id'],row['candidate_id'],role,'pass',role,self.now,
                    json.dumps({'check_id':10 if role=='review' else 11})))

    def authorization(self,row):return self.admission.evaluate_review_policy(row)

    def test_initial_check_releases_only_exact_owned_wait(self):
        row=self.admit();life=Lifecycle(self.store,clock=lambda:self.now)
        life.open_gate(self.command(row['unit_id']),'approval_required',details={'reason':'independent'},candidate_id=row['candidate_id'])
        result=self.driver.step(row['effect_id'])
        self.assertEqual(result['status'],'confirmed')
        with self.store.read_snapshot() as db:
            gates=[dict(g) for g in db.execute('SELECT * FROM unit_gates WHERE unit_id=?',(row['unit_id'],))]
            self.assertTrue(next(g for g in gates if g['code']=='observation_unavailable')['resolved_at'])
            self.assertIsNone(next(g for g in gates if g['code']=='approval_required')['resolved_at'])
            self.assertEqual(db.execute('SELECT count(*) FROM issues').fetchone()[0],0)

    def test_success_does_not_require_merge_permission_or_blanket_human_approval(self):
        row=self.admit();self.passed(row)
        self.assertIsNone(self.admission.evaluate(row))
        auth=self.authorization(row);self.assertIsNotNone(auth)
        result=MergeController(self.store,admission=self.admission,clock=lambda:self.now).step(row['unit_id'])
        receipt=self.driver.step(result['effect_id'])
        self.assertEqual(receipt['status'],'confirmed')
        self.assertTrue(receipt['payload']['review_only'])
        self.github.merge.assert_not_called();self.github.protections.assert_not_called();self.github.approvals.assert_not_called()
        with self.assertRaisesRegex(TransitionError,'Review-only'):
            self.effects.queue_merge(self.command(row['unit_id']),row['candidate_id'],policy_hash=self.policy,authorization=auth)

    def test_same_head_peer_blocks_until_all_independent_verdicts_pass(self):
        first=self.admit();self.passed(first);second=self.admit(8)
        self.assertIsNone(self.authorization(first))
        self.passed(second)
        for row in (first,second):
            auth=self.authorization(row);self.assertIsNotNone(auth)
            self.effects.queue_review_policy(self.command(row['unit_id']),row['candidate_id'],authorization=auth)
        self.remotes[8]['body']='Changed scope'
        self.assertIsNone(self.authorization(first))

    def test_new_peer_between_observation_and_claim_invalidates_success(self):
        first=self.admit();self.passed(first);auth=self.authorization(first)
        effect=self.effects.queue_review_policy(self.command(first['unit_id']),first['candidate_id'],authorization=auth)['effect_id']
        self.admit(8)
        value=EffectQueries(self.store).get(effect)
        with self.assertRaisesRegex(TransitionError,'peer candidates changed'):
            self.effects.claim(effect,expected_version=value['unit_version'],authorization=auth)
        self.assertEqual(EffectQueries(self.store).get(effect)['attempts'],0)

    def test_independent_hold_and_stale_source_cannot_authorize_check(self):
        row=self.admit();self.passed(row)
        auth=self.authorization(row)
        Lifecycle(self.store,clock=lambda:self.now).pause(self.command(row['unit_id']))
        with self.assertRaisesRegex(TransitionError,'unresolved obligations'):
            self.effects.queue_review_policy(self.command(row['unit_id']),row['candidate_id'],authorization=auth)
        self.remotes[7]['base']['sha']='c'*40
        self.assertIsNone(self.authorization(row))

    def test_new_same_head_candidate_has_distinct_in_progress_check(self):
        row=self.admit();self.passed(row)
        result=MergeController(self.store,admission=self.admission,clock=lambda:self.now).step(row['unit_id'])
        self.driver.step(result['effect_id'])
        self.remotes[7]['title']='Changed intent'
        updated=self.observer.observe(normalize_source(self.remotes[7],repo_id=1,repo='owner/repo'),policy_hash=self.policy)
        self.assertNotEqual(updated['candidate_id'],row['candidate_id'])
        with self.store.read_snapshot() as db:
            effect=db.execute('SELECT e.id,e.payload_json FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id '
                'WHERE c.candidate_id=?',(updated['candidate_id'],)).fetchone()
        self.assertIsNone(json.loads(effect['payload_json'])['conclusion'])
        self.assertEqual(self.driver.step(effect['id'])['status'],'confirmed')
        self.assertIsNone(self.authorization(updated))

    def test_explicit_audit_approval_hold_remains_until_fresh_human_approval(self):
        row=self.admit();self.passed(row)
        gate=Lifecycle(self.store,clock=lambda:self.now).open_gate(self.command(row['unit_id']),
            'approval_required',candidate_id=row['candidate_id'],details={'reason':'audit_required'})['gate_id']
        self.github.approvals.return_value=[]
        self.assertIsNone(self.authorization(row))
        self.github.approvals.return_value=[{'state':'APPROVED','counted':True}]
        result=MergeController(self.store,admission=self.admission,clock=lambda:self.now).step(row['unit_id'])
        self.assertIn('effect_id',result)
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(gate,)).fetchone()[0])

    def test_verification_uses_exact_pr_source_without_issue_lookup(self):
        from forge.factory.verification_controller import VerificationController
        row=self.admit();self.passed(row)
        gate=Lifecycle(self.store,clock=lambda:self.now).open_gate(self.command(row['unit_id']),
            'verification_human',candidate_id=row['candidate_id'],details={'what':'Check UI','ci':[]})['gate_id']
        verifier=VerificationController(self.store,self.github,profiles=lambda repo:[],
            policy=lambda item:policy(self.config,1,repository_name='owner/repo'),clock=lambda:self.now)
        self.github.approvals.return_value=[]
        item=verifier.queries.gate(gate)
        self.assertTrue(verifier._observe(item));self.github.issue.assert_not_called()
        self.remotes[7]['user']['id']=999
        self.assertFalse(verifier._observe(item))

    def test_exact_accepted_checks_allow_independent_same_commit_verdicts(self):
        from forge.factory.github import GitHub
        remote=Mock();remote.app_ids.return_value={10}
        remote.pages.return_value=[dict(id=n,name='forge/'+role,app={'id':10},head_sha='a'*40,
            status='completed',conclusion='success') for n,role in ((1,'review'),(2,'audit'),(3,'review'),(4,'audit'))]
        item=dict(candidate={'head':'a'*40},evidence={'review':{'check_id':1},'audit':{'check_id':2}},review_source={'version':1})
        self.assertTrue(GitHub.gates(remote,item))
        self.assertFalse(GitHub.gates(remote,{**item,'review_source':None}))
        remote.pages.return_value[0]['conclusion']='failure'
        self.assertFalse(GitHub.gates(remote,item))

    def test_lost_initial_check_reply_reconciles_after_due_without_duplicate_identity(self):
        from forge.factory.github import GitHubUnavailable
        row=self.admit();calls=[]
        def lost(*args,**kwargs):
            result=self.check(*args,**kwargs);calls.append(result)
            if len(calls)==1:raise GitHubUnavailable('Lost response')
            return result
        self.github.check.side_effect=lost
        with self.assertRaises(GitHubUnavailable):self.driver.step(row['effect_id'])
        self.assertEqual(self.driver.step(row['effect_id'])['status'],'uncertain')
        self.assertEqual(len(calls),1)
        self.now+=30
        restarted=EffectController(self.store,self.github,admission=self.admission,clock=lambda:self.now)
        self.assertEqual(restarted.step(row['effect_id'])['status'],'confirmed')
        self.assertEqual(calls,[1,1]);self.assertEqual(len(self.checks),1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL',
                (row['unit_id'],)).fetchone()[0],0)

    def test_peer_hold_after_observation_prevents_send_without_consuming_attempt(self):
        first=self.admit();self.passed(first);second=self.admit(8);self.passed(second)
        auth=self.authorization(first)
        effect=self.effects.queue_review_policy(self.command(first['unit_id']),first['candidate_id'],authorization=auth)['effect_id']
        Lifecycle(self.store,clock=lambda:self.now).open_gate(self.command(second['unit_id']),
            'approval_required',candidate_id=second['candidate_id'],details={'reason':'independent'})
        value=EffectQueries(self.store).get(effect)
        with self.assertRaisesRegex(TransitionError,'still has obligations'):
            self.effects.claim(effect,expected_version=value['unit_version'],authorization=auth)
        self.assertEqual(EffectQueries(self.store).get(effect)['attempts'],0)
