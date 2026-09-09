import copy
from datetime import datetime,timezone
import json
from types import SimpleNamespace
import time
import unittest
from unittest.mock import patch

from forge import execution_reconciliation as reconcile,execution_retirement as retirement,workflow
from tests import test_execution_retirement as fixtures


class ExecutionReconciliationTests(unittest.TestCase):
    def setUp(self):
        self._setup()

    def _setup(self,role='review',checkout_head=None):
        self.fixture=fixtures.ExecutionRetirementTests();self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        f=self.fixture;f.session='App-'+role;f.receipt.update(session=f.session,role=role);f.save_receipt()
        with (f.config/'app.repo').open('a') as config:config.write(f'CLONE_AUDIT={f.clone}\n')
        self.pr_ref={'number':9,'head':'a'*40,'base':'b'*40};now=time.time()-300
        workflow.reconcile(f.state,'owner/app','r',now,[{'number':1,'stage':role,'disposition':'pending','prs':[self.pr_ref]}],source_event_id='facts')
        self.a=workflow.reserve(f.state,'owner/app',role,f.session,'r',[1],pr=self.pr_ref,source_event_id='reserve',now=now)
        workflow.transition(f.state,self.a['id'],'bind',generation=f.gen['id'],provider='codex',source_event_id='bind',now=now)
        workflow.transition(f.state,self.a['id'],'submitted',generation=f.gen['id'],source_event_id='submitted',now=now)
        if checkout_head is not None:f.head=checkout_head
        result=f.release();self.assertTrue(result['released'],result)
        self.key=retirement.read_quarantines(f.state)[0]['id']
        self.pr={'number':9,'url':'https://github.com/Owner/App/pull/9','state':'OPEN','isDraft':False,
            'headRefOid':'a'*40,'baseRefOid':'b'*40,'labels':[{'name':'changes-requested'}],
            'author':{'login':'forge-bot'},'closingIssuesReferences':[{'number':1,'url':'https://github.com/Owner/App/issues/1'}],
            'reviews':[{'id':'R1','author':{'login':'forge-bot'},'state':'COMMENTED',
                'submittedAt':datetime.fromtimestamp(time.time()-150,timezone.utc).isoformat(),
                'commit':{'oid':'a'*40},'body':'Reviewer result: changes requested for this revision'}]}
        self.action_calls=[]
        self.merged_pr=None;self.associated=[];self.trees={}
        self.sessions=[];self.owners=[{'name':'auto:owned-by-test'}];self.reads=0;self.mutate=None;self.compare_status='ahead'

    def runner(self,args,**kwargs):
        self.action_calls.append(args)
        if args[:2]==['tmux','list-sessions']:return SimpleNamespace(returncode=0,stdout='\n'.join(self.sessions),stderr='')
        if args[0]=='gh':
            if args[:3]==['gh','pr','view']:
                self.reads+=1
                if self.mutate:self.mutate(self.reads)
                data=self.merged_pr if args[3]=='8' else self.pr
            elif '/labels?' in args[2]:data=self.owners
            elif '/git/commits/' in args[2]:
                sha=args[2].rsplit('/',1)[1];data={'sha':sha,'tree':{'sha':self.trees.get(sha,'f'*40)}}
            elif '/pulls?' in args[2]:data=self.associated
            elif '/compare/' in args[2]:data={'status':self.compare_status,'base_commit':{'sha':args[2].rsplit('/',1)[1].split('...')[0]},'behind_by':0}
            else:self.fail(args)
            return SimpleNamespace(returncode=0,stdout=json.dumps(data),stderr='')
        return self.fixture.runner(args,**kwargs)

    def run_reconcile(self,**kwargs):
        f=self.fixture
        return reconcile.reconcile(f.config,f.state,f.session,lock_fd=f.lock.fileno(),run=self.runner,**kwargs)

    def test_published_failure_reopens_clone_without_issue_completion_or_checkpoint_mutation(self):
        f=self.fixture;path=f.state/'execution-retirements'/'checkpoints'/(self.key+'.json');before=path.read_bytes()
        result=self.run_reconcile();self.assertTrue(result['reconciled'],result)
        self.assertEqual(path.read_bytes(),before);self.assertEqual(retirement.read_quarantines(f.state),[])
        self.assertEqual(workflow.read_state(f.state)['issues'][0]['stage'],'review')
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.action_calls))

    def test_changed_revision_does_not_erase_old_published_failure(self):
        self.pr['headRefOid']='c'*40;self.pr['labels']=[{'name':'ready-for-review'}]
        later=copy.deepcopy(self.pr['reviews'][0]);later.update(id='R2',commit={'oid':'c'*40},body='Reviewer PASS')
        self.pr['reviews'].append(later)
        self.assertTrue(self.run_reconcile()['reconciled'])

    def test_current_revision_pass_requires_applied_audit_stage(self):
        self.pr['reviews'][0]['body']='Reviewer PASS';self.pr['labels']=[{'name':'ready-for-review'}]
        self.assertFalse(self.run_reconcile()['reconciled'])
        self.pr['labels']=[{'name':'ready-for-audit'}]
        self.assertTrue(self.run_reconcile()['reconciled'])

    def test_missing_untrusted_dismissed_or_wrong_head_result_stays_quarantined(self):
        original=copy.deepcopy(self.pr)
        for change in [lambda p:p.update(reviews=[]),lambda p:p['reviews'][0].update(author={'login':'attacker'}),
            lambda p:p['reviews'][0].update(state='DISMISSED'),lambda p:p['reviews'][0].update(commit={'oid':'d'*40})]:
            self.pr=copy.deepcopy(original);change(self.pr)
            self.assertFalse(self.run_reconcile()['reconciled'])
        self.assertTrue(retirement.read_quarantines(self.fixture.state))

    def test_hold_wrong_owner_cross_repo_reference_refused(self):
        original=copy.deepcopy(self.pr)
        for change in [lambda p:p['labels'].append({'name':'hold'}),lambda p:p.update(author={'login':'other'}),
            lambda p:p['closingIssuesReferences'][0].update(repository={'nameWithOwner':'Other/App'})]:
            self.pr=copy.deepcopy(original);change(self.pr);self.assertFalse(self.run_reconcile()['reconciled'])
        self.pr=original;self.owners=[];self.assertFalse(self.run_reconcile()['reconciled'])

    def test_dirty_changed_branch_or_occupied_shared_clone_refused(self):
        f=self.fixture;f.status=' M file';self.assertFalse(self.run_reconcile()['reconciled'])
        f.status='';self.sessions=['App-build'];self.assertFalse(self.run_reconcile()['reconciled'])
        self.action_calls=[]
        self.sessions=[];f.head='d'*40;self.compare_status='diverged';self.assertFalse(self.run_reconcile()['reconciled'])

    def test_disabled_configured_worker_sharing_clone_blocks_reconciliation(self):
        f=self.fixture
        (f.config/'disabled.repo').write_text(f'REPO=Owner/Disabled\nOWNER_LABEL=auto:owned-by-test\nENABLED=false\nCLONE_REVIEW={f.clone}\n')
        self.sessions=['Disabled-review']
        result=self.run_reconcile()
        self.assertFalse(result['reconciled']);self.assertEqual(result['reason'],'reconciliation_workspace_occupied')
        self.assertTrue(retirement.read_quarantines(f.state))

    def test_canonical_published_descendant_can_be_reconciled(self):
        self.pr['headRefOid']='c'*40;self.fixture.head='c'*40;self.pr['labels']=[{'name':'ready-for-review'}]
        self.assertTrue(self.run_reconcile()['reconciled'])

    def test_second_pass_candidate_change_prevents_ack(self):
        self.mutate=lambda count:self.pr.update(headRefOid='c'*40) if count==2 else None
        result=self.run_reconcile();self.assertFalse(result['reconciled']);self.assertEqual(result['reason'],'reconciliation_evidence_changed')
        self.assertTrue(retirement.read_quarantines(self.fixture.state))

    def test_corrupt_ack_never_clears_quarantine(self):
        self.assertTrue(self.run_reconcile()['reconciled'])
        p=self.fixture.state/'execution-retirements'/'reconciled'/(self.key+'.json')
        value=json.loads(p.read_text());value['proof']['outcome']='tampered';p.write_text(json.dumps(value))
        with self.assertRaisesRegex(ValueError,'reconciliation_receipt_invalid'):retirement.read_quarantines(self.fixture.state)

    def test_unchanged_old_checkout_requires_canonical_base_ancestry(self):
        self._setup(checkout_head='e'*40)
        self.pr['reviews']=[];self.pr['labels']=[{'name':'ready-for-review'}]
        self.compare_status='diverged'
        self.assertFalse(self.run_reconcile(retry_checkpoint=self.key,retry_reason='Corrected role policy')['reconciled'])
        self.compare_status='ahead'
        result=self.run_reconcile(retry_checkpoint=self.key,retry_reason='Corrected role policy')
        self.assertTrue(result['reconciled'],result)

    def test_exact_published_squash_checkpoint_retry_and_identity_refusals(self):
        self._setup(checkout_head='e'*40)
        self.pr['reviews']=[];self.pr['labels']=[{'name':'ready-for-review'}];self.compare_status='diverged'
        self.associated=[{'number':8,'html_url':'https://github.com/Owner/App/pull/8',
            'base':{'repo':{'full_name':'Owner/App'}},'head':{'sha':'e'*40},'merged_at':'2026-01-01T00:00:00Z'}]
        self.merged_pr={'number':8,'url':'https://github.com/Owner/App/pull/8','state':'MERGED',
            'headRefOid':'e'*40,'mergeCommit':{'oid':'b'*40},'author':{'login':'forge-bot'}}
        original=copy.deepcopy(self.merged_pr)
        for change in [lambda p:p.update(headRefOid='d'*40),lambda p:p.update(state='OPEN'),
                       lambda p:p.update(url='https://github.com/Other/App/pull/8'),
                       lambda p:p.update(author={'login':'other'})]:
            self.merged_pr=copy.deepcopy(original);change(self.merged_pr)
            self.assertFalse(self.run_reconcile(retry_checkpoint=self.key,retry_reason='Corrected policy')['reconciled'])
        self.merged_pr=original;self.trees['b'*40]='a'*40
        self.assertFalse(self.run_reconcile(retry_checkpoint=self.key,retry_reason='Corrected policy')['reconciled'])
        self.trees={};self.associated[0]['base']['repo']['full_name']='Other/App'
        self.assertFalse(self.run_reconcile(retry_checkpoint=self.key,retry_reason='Corrected policy')['reconciled'])
        self.associated[0]['base']['repo']['full_name']='Owner/App'
        result=self.run_reconcile(retry_checkpoint=self.key,retry_reason='Corrected policy')
        self.assertTrue(result['reconciled'],result)
        ack=json.loads((self.fixture.state/'execution-retirements'/'reconciled'/(self.key+'.json')).read_text())
        self.assertEqual(ack['proof']['checkout_publication']['pr'],8)
        self.assertEqual(ack['proof']['checkout_basis'],'exact_tree_of_verified_merged_pr')

    def test_explicit_retry_is_exact_once_and_never_automatic(self):
        self.pr['reviews']=[];self.pr['labels']=[{'name':'ready-for-review'}]
        self.assertFalse(self.run_reconcile()['reconciled'])
        self.assertFalse(self.run_reconcile(retry_checkpoint='a'*64,retry_reason='Corrected role policy')['reconciled'])
        result=self.run_reconcile(retry_checkpoint=self.key,retry_reason='Corrected role policy')
        self.assertTrue(result['reconciled'],result)
        path=self.fixture.state/'execution-retirements'/'reconciled'/(self.key+'.json')
        proof=json.loads(path.read_text())['proof'];self.assertEqual(proof['source'],'operator_retry')
        self.assertEqual(len(proof['policy_sha256']),64)
        self.assertFalse(self.run_reconcile(retry_checkpoint=self.key,retry_reason='Repeat')['reconciled'])

    def test_prior_acknowledgement_cannot_clear_new_checkpoint(self):
        self.assertTrue(self.run_reconcile()['reconciled'])
        root=self.fixture.state/'execution-retirements'
        record=json.loads((root/(self.key+'.json')).read_text())
        checkpoint=json.loads((root/'checkpoints'/(self.key+'.json')).read_text())
        record['checkpoint_sha256']='e'*64
        with self.assertRaisesRegex(ValueError,'reconciliation_receipt_invalid'):
            reconcile.acknowledged(self.fixture.state,record,checkpoint)
        record['id']='f'*64
        self.assertFalse(reconcile.acknowledged(self.fixture.state,record,checkpoint))

    def test_retry_policy_resolves_runtime_or_source_and_rejects_bad_runtime(self):
        import hashlib
        root=self.fixture.state/'package';(root/'forge').mkdir(parents=True)
        source=root/'legacy'/'roles';source.mkdir(parents=True)
        (source/'reviewer.md').write_text('source policy')
        with patch.object(reconcile,'__file__',str(root/'forge'/'execution_reconciliation.py')):
            self.assertEqual(reconcile._policy_digest('review'),hashlib.sha256(b'source policy').hexdigest())
            runtime=root/'roles';runtime.mkdir();(runtime/'reviewer.md').write_text('runtime policy')
            self.assertEqual(reconcile._policy_digest('review'),hashlib.sha256(b'runtime policy').hexdigest())
            (runtime/'reviewer.md').unlink();(runtime/'reviewer.md').symlink_to(source/'reviewer.md')
            with self.assertRaisesRegex(ValueError,'operator_retry_policy_unverified'):reconcile._policy_digest('review')

    def test_audit_failure_exact_revision_reconciles_but_critical_pass_does_not(self):
        self._setup('audit')
        self.pr['reviews'][0]['body']='Audit FAIL '+self.pr_ref['head']+' '+self.pr_ref['base']
        self.pr['labels'].append({'name':'critical'})
        result=self.run_reconcile();self.assertTrue(result['reconciled'],result)
        self._setup('audit')
        self.pr['reviews'][0]['body']='Audit PASS '+self.pr_ref['head']+' '+self.pr_ref['base']+' awaiting human approval'
        self.pr['labels']=[{'name':'ready-for-audit'},{'name':'critical'}]
        self.assertFalse(self.run_reconcile()['reconciled'])
        self.assertFalse(self.run_reconcile(retry_checkpoint=self.key,retry_reason='retry')['reconciled'])

    def test_no_lock_no_network_or_ack(self):
        import fcntl
        fcntl.flock(self.fixture.lock,fcntl.LOCK_UN)
        self.assertEqual(self.run_reconcile()['reason'],'supervisor_lock_required');self.assertEqual(self.reads,0)


if __name__=='__main__':unittest.main()
