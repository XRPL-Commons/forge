import fcntl
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from forge import execution_retirement as retirement, workflow, workflow_control
from forge.codex_worker import parse_generation
from tests.host_isolation import deny_host_tmux


class ExecutionRetirementTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.state=self.root/'state';self.state.mkdir()
        self.config=self.root/'config';self.config.mkdir();self.clone=self.root/'clone';self.clone.mkdir()
        (self.config/'app.repo').write_text(f'REPO=Owner/App\nOWNER_LABEL=auto:owned-by-test\nCLONE_BUILD={self.clone}\nCLONE_REVIEW={self.clone}\n')
        self.session='App-build';self.gen=parse_generation('$1|100|%1|10');self.head='a'*40
        self.status='';self.diff='';self.untracked='';self.prompt='user@host:~/clone$';self.children=False
        self.calls=[];self.kill_fail=False;self.identities=0;self.change=None
        self.receipt={'provider':'codex','state':'completed','role':'build','repo':'Owner/App','clone':str(self.clone),
            'session':self.session,'generation':self.gen,'provider_process':{'pid':50,'start_ticks':1,'process_session':50},'run_id':'11111111-1111-4111-8111-111111111111',
            'thread_id':'22222222-2222-4222-8222-222222222222','started_at':time.time()-200,'completed_at':time.time()-100}
        self.save_receipt()
        self.observe=patch.object(retirement.codex_worker,'observe',side_effect=lambda *a,**k:
            {'provider':'codex','state':'idle','run_id':self.receipt['run_id']})
        self.observe.start();self.addCleanup(self.observe.stop)
        self.group=patch.object(retirement.codex_worker,'remaining_group',return_value=[])
        self.group_mock=self.group.start();self.addCleanup(self.group.stop)
        self.lock=(self.root/'state-supervisor.lock').open('a');self.addCleanup(self.lock.close)
        fcntl.flock(self.lock,fcntl.LOCK_EX)

    def save_receipt(self):
        p=self.state/'codex-workers';p.mkdir(exist_ok=True)
        (p/(self.session+'.json')).write_text(json.dumps(self.receipt))

    def runner(self,args,**kwargs):
        self.calls.append(args)
        if args[:2]==['tmux','list-panes']:
            self.identities+=1
            if self.change:self.change(self.identities)
            out=self.session+'|'+self.gen['id']+'|bash'
        elif args[:2]==['tmux','capture-pane']:out=self.prompt
        elif args[0]=='ps':out='10 1 bash\n'+('11 10 node\n' if self.children else '')
        elif args[:2]==['tmux','kill-session']:
            return SimpleNamespace(returncode=int(self.kill_fail),stdout='',stderr='')
        elif args[:3]==['git','-C',str(self.clone)]:
            command=args[3:]
            if command==['rev-parse','--show-toplevel']:out=str(self.clone)
            elif command==['remote','get-url','origin']:out='git@github.com:Owner/App.git'
            elif command==['rev-parse','HEAD']:out=self.head
            elif command==['rev-parse','--abbrev-ref','HEAD']:out='unfinished-work'
            elif command[0]=='status':out=self.status
            elif command[0]=='diff':out=self.diff
            elif command[0]=='ls-files':out=self.untracked
            else:self.fail(args)
        else:self.fail('Unexpected external command: '+str(args))
        return SimpleNamespace(returncode=0,stdout=out,stderr='')

    def release(self):return retirement.release(self.config,self.state,self.session,lock_fd=self.lock.fileno(),run=self.runner)

    def assign(self):
        now=time.time()-300
        workflow.reconcile(self.state,'owner/app','r',now,[{'number':9,'stage':'build','disposition':'pending'}],source_event_id='facts')
        a=workflow.reserve(self.state,'owner/app','build',self.session,'r',[9],source_event_id='reserve',now=now)
        workflow.transition(self.state,a['id'],'bind',generation=self.gen['id'],provider='codex',source_event_id='bind',now=now)
        workflow.transition(self.state,a['id'],'submitted',generation=self.gen['id'],source_event_id='submit',now=now)
        return a

    def test_dirty_unpublished_work_preserved_quarantine_frees_capacity_not_issue(self):
        a=self.assign();self.status=' M tracked\0?? notes.txt\0';self.diff='unpublished patch';self.untracked='notes.txt\0'
        file=self.clone/'notes.txt';file.write_text('unfinished work')
        result=self.release();self.assertTrue(result['released'],result)
        self.assertEqual(file.read_text(),'unfinished work')
        view=workflow.read_state(self.state)
        self.assertEqual(view['slots'][0]['state'],'free');self.assertEqual(view['issues'][0]['stage'],'build')
        self.assertEqual(view['assignments'][0]['state'],'retired')
        rows=retirement.read_quarantines(self.state);self.assertEqual(rows[0]['assignment_id'],a['id'])
        self.assertEqual(rows[0]['issue_numbers'],[9]);self.assertTrue(rows[0]['released'])
        self.assertTrue(retirement.is_quarantined(self.config,self.state,'App-review'))
        with self.assertRaisesRegex(ValueError,'preserved_workspace_requires_reconciliation'):
            workflow_control.reserve(self.config,self.state,self.session,run=self.runner)
        self.assertFalse(any(call[0]=='gh' for call in self.calls))
        workflow.reconcile(self.state,'owner/other','r',time.time(),[{'number':1,'stage':'build','disposition':'pending'}],source_event_id='other')
        other=workflow.reserve(self.state,'owner/other','build','Other-build','r',[1],source_event_id='other-reserve',max_slots=1)
        self.assertEqual(other['issue_numbers'],[1])

    def test_legacy_identity_not_invented_and_other_clone_unblocked(self):
        workflow.observe_slot(self.state,self.session,'owner/app','build',self.gen['id'],'retiring',source_event_id='legacy',bootstrap=True)
        result=self.release();self.assertTrue(result['released'],result)
        row=retirement.read_quarantines(self.state)[0]
        self.assertEqual(row['identity_basis'],'legacy_identity_unknown');self.assertEqual(row['issue_numbers'],[])
        self.assertEqual(workflow.read_state(self.state)['assignments'],[])
        (self.config/'other.repo').write_text(f'REPO=Owner/Other\nOWNER_LABEL=auto:owned-by-test\nCLONE_BUILD={self.root}/other\n')
        self.assertFalse(retirement.is_quarantined(self.config,self.state,'Other-build'))

    def test_lock_required_before_any_evidence_or_kill(self):
        fcntl.flock(self.lock,fcntl.LOCK_UN)
        self.assertEqual(self.release()['reason'],'supervisor_lock_required');self.assertEqual(self.calls,[])

    def test_failed_terminal_execution_releases_capacity_without_success_claim(self):
        self.receipt['state']='failed';self.receipt['reason']='turn_failed';self.save_receipt()
        result=self.release();self.assertTrue(result['released'],result)
        self.assertEqual(retirement.read_quarantines(self.state)[0]['execution_state'],'failed')

    def test_exact_assignment_execution_mismatch_keeps_worker(self):
        self.assign()
        value=workflow.read_state(self.state)
        value['assignments'][0]['execution']={'run_id':'different-execution'}
        with patch.object(workflow,'read_state',return_value=value):
            result=self.release()
        self.assertFalse(result['released']);self.assertEqual(result['reason'],'assignment_execution_changed')
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_unknown_or_live_background_processes_keep_worker(self):
        for reason in ('background_inventory_unavailable','background_inventory_unbounded'):
            self.receipt['reason']=reason;self.save_receipt()
            self.assertEqual(self.release()['reason'],'background_inventory_unverified')
        self.receipt['reason']='execution_failed';self.save_receipt()
        self.group_mock.side_effect=PermissionError('process inventory unavailable')
        self.assertEqual(self.release()['reason'],'background_inventory_unverified')
        self.group_mock.side_effect=None;self.group_mock.return_value=[{'pid':77,'process_session':50}]
        self.assertEqual(self.release()['reason'],'execution_background_processes')
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_late_reparented_background_process_is_rechecked(self):
        self.group_mock.side_effect=[[],[],[],[{'pid':77,'process_session':50}]]
        self.assertEqual(self.release()['reason'],'execution_background_processes')
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_terminal_grace_and_invalid_times_keep_worker(self):
        for value in [time.time()-5,time.time()+5,float('nan'),None]:
            self.receipt['completed_at']=value;self.save_receipt()
            self.assertFalse(self.release()['released'])
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_busy_child_typed_input_and_running_receipt_keep_worker(self):
        for mode in ['child','input','running']:
            self.children=mode=='child';self.prompt='user@host:~/clone$ work' if mode=='input' else 'user@host:~/clone$'
            self.receipt['state']='running' if mode=='running' else 'completed';self.save_receipt()
            self.assertFalse(self.release()['released'])
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_dirty_content_change_between_proofs_prevents_kill(self):
        self.untracked='note\0';file=self.clone/'note';file.write_text('before')
        self.change=lambda count:file.write_text('after') if count==3 else None
        result=self.release();self.assertFalse(result['released']);self.assertEqual(result['reason'],'retirement_evidence_changed')
        self.assertTrue(retirement.is_quarantined(self.config,self.state,self.session))
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_generation_change_and_late_child_refuse(self):
        self.change=lambda count:setattr(self,'children',True) if count==4 else None
        result=self.release();self.assertFalse(result['released']);self.assertEqual(result['reason'],'processes_not_quiescent')
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_failed_kill_never_frees_assignment_and_prepared_quarantine_blocks(self):
        self.assign();self.kill_fail=True;result=self.release();self.assertFalse(result['released'])
        self.assertNotEqual(workflow.read_state(self.state)['slots'][0]['state'],'free')
        row=retirement.read_quarantines(self.state)[0];self.assertEqual(row['phase'],'prepared');self.assertFalse(row['released'])

    def test_postkill_ledger_error_retains_outbox_and_quarantine(self):
        self.assign()
        with patch.object(workflow,'transition',side_effect=OSError('db unavailable')):result=self.release()
        self.assertTrue(result['released']);self.assertEqual(result['workflow']['status'],'pending')
        self.assertTrue(retirement.is_quarantined(self.config,self.state,self.session))
        self.assertEqual(len(list((self.state/'workflow-releases').glob('*.json'))),1)

    def test_corrupt_checkpoint_blocks_admission(self):
        self.assertTrue(self.release()['released'])
        p=next((self.state/'execution-retirements'/'checkpoints').glob('*.json'));p.write_text('{}')
        with self.assertRaisesRegex(ValueError,'quarantine_checkpoint_changed'):
            retirement.is_quarantined(self.config,self.state,self.session)

    def test_deadline_after_last_read_keeps_worker(self):
        with patch.object(retirement,'bounded_run',side_effect=lambda run,seconds: __import__('forge.handoff',fromlist=['bounded_run']).bounded_run(run,.000001)):
            self.assertFalse(self.release()['released'])
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))


if __name__=='__main__':unittest.main()
