import fcntl
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tests.host_isolation import deny_host_tmux
from forge import provider_handover as handover
from forge.control import atomic_json,snapshot_path
from forge.handoff import session_identity,Unverified


class ProviderHandoverTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'state';self.config=self.root/'repos';self.config.mkdir()
        self.clone=self.root/'clone';self.clone.mkdir()
        (self.config/'repo.repo').write_text(f'REPO=owner/repo\nOWNER_LABEL=auto:owned-by-luc\nCLONE_BUILD={self.clone}\n')
        self.snapshot={'repo':'owner/repo','status':'ok','enabled':True,'observed_at':99,
                       'schedule':{'active':[1]},'work':{k:[] for k in ('ready','wip','review','audit','sentback','held','limbo','conflicts')}}
        self.snapshot['work']['wip']=[1]
        atomic_json(snapshot_path(self.state,'owner/repo'),self.snapshot)
        self.banner="⎿ You've reached your Claude limit. Run /usage-credits to continue or switch models with /model."
        self.pane='❯ continue\n'+self.banner+'\n❯\n────\n⏵⏵ bypass permissions on (shift+tab to cycle)'
        self.identity='repo-build|$7|50|%7|100|claude'
        self.processes='100 1 bash\n101 100 claude'
        self.head='a'*40;self.status=' M unfinished.txt\0?? draft.txt\0'
        (self.clone/'unfinished.txt').write_text('UNFINISHED_PRIVATE_WORK')
        (self.clone/'draft.txt').write_text('PRESERVE_UNTRACKED')
        (self.state/'repo-build.up').write_text('dispatch-1')
        self.guard={'account_until':9000,'account_reason':'account_guard','sessions':{'repo-build':{
            'reason':'blocked_model_usage','until':9000,'launches':[40],
            'error_fingerprint':hashlib.sha256(('dispatch-1\n'+self.banner).encode()).hexdigest()},
            'other-build':{'reason':'blocked_quota','until':9000,'launches':[30]}}}
        atomic_json(self.state/'legacy-health/guard.json',self.guard)
        self.calls=[];self.head_reads=0;self.change=None

    def fake(self,args,**kwargs):
        self.calls.append(args);out=''
        if args[:2]==['tmux','list-panes']:out=self.identity
        elif args[:2]==['tmux','capture-pane']:out=self.pane
        elif args[:2]==['tmux','kill-session']:pass
        elif args[0]=='ps':out=self.processes
        elif args[0]=='git':
            op=args[3:]
            if op==['rev-parse','--show-toplevel']:out=str(self.clone)
            elif op==['remote','get-url','origin']:out='git@github.com:owner/repo.git'
            elif op==['rev-parse','HEAD']:
                self.head_reads+=1
                if self.head_reads==2 and self.change:self.change()
                out=self.head
            elif op==['rev-parse','--abbrev-ref','HEAD']:out='issue-1'
            elif op[0]=='status':out=self.status
            elif op[0]=='diff':out='private staged diff'
            else:self.fail(args)
        else:self.fail(args)
        return subprocess.CompletedProcess(args,0,out,'')

    def release(self):
        with (self.state.parent/'state-supervisor.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return handover.release(self.config,self.state,'repo-build','claude',run=self.fake,now=100,lock_fd=lock.fileno())

    def test_dirty_work_preserved_exact_generation_released_and_matching_guard_only_cleared(self):
        result=self.release()
        self.assertTrue(result['released'],result)
        self.assertEqual([c for c in self.calls if c[:2]==['tmux','kill-session']],[['tmux','kill-session','-t','$7']])
        self.assertEqual((self.clone/'unfinished.txt').read_text(),'UNFINISHED_PRIVATE_WORK')
        self.assertEqual((self.clone/'draft.txt').read_text(),'PRESERVE_UNTRACKED')
        path=self.state/'provider-handovers/repo-build.json';manifest=json.loads(path.read_text())
        self.assertTrue(manifest['released']);self.assertEqual(manifest['claims'],[1])
        self.assertNotIn('UNFINISHED_PRIVATE_WORK',path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        guard=json.loads((self.state/'legacy-health/guard.json').read_text())
        self.assertEqual(guard['account_until'],9000)
        self.assertEqual(guard['sessions']['other-build'],self.guard['sessions']['other-build'])
        self.assertEqual(guard['sessions']['repo-build']['launches'],[40])
        self.assertNotIn('reason',guard['sessions']['repo-build'])
        self.assertFalse((self.state/'repo-build.up').exists())

    def test_unlocked_release_has_no_proof_or_kill(self):
        result=handover.release(self.config,self.state,'repo-build','claude',run=self.fake,lock_fd=-1)
        self.assertEqual(result['reason'],'supervisor_lock_required');self.assertEqual(self.calls,[])

    def test_dirty_file_or_generation_change_preserves_worker(self):
        for change in (lambda:(self.clone/'draft.txt').write_text('changed'),lambda:setattr(self,'identity','repo-build|$8|51|%8|102|claude')):
            self.head_reads=0;self.change=change
            result=self.release();self.assertFalse(result['released'],result)
            self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_typed_input_children_or_historical_error_never_authorize_handover(self):
        for attribute,value in [('pane',self.pane.replace('❯\n','❯ unsent instruction\n')),
                                ('processes',self.processes+'\n102 101 bash'),
                                ('pane',self.pane.replace(self.banner,self.banner+'\n● Work completed successfully'))]:
            old=getattr(self,attribute);setattr(self,attribute,value)
            self.assertFalse(self.release()['released'])
            setattr(self,attribute,old)
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_guard_with_different_fingerprint_or_permission_reason_is_preserved(self):
        self.guard['sessions']['repo-build']['error_fingerprint']='other-generation'
        atomic_json(self.state/'legacy-health/guard.json',self.guard)
        result=self.release();self.assertTrue(result['released'])
        self.assertEqual(json.loads((self.state/'legacy-health/guard.json').read_text()),self.guard)

    def test_failure_class_is_bounded_and_manifest_precedes_teardown(self):
        result=handover.check(self.config,self.state,'repo-build','claude',run=self.fake,now=100)
        self.assertEqual(result['failure_class'],'blocked_model_usage')
        fake=self.fake
        def checked(args,**kwargs):
            if args[:2]==['tmux','kill-session']:
                manifest=json.loads((self.state/'provider-handovers/repo-build.json').read_text())
                self.assertFalse(manifest['released'])
            return fake(args,**kwargs)
        with patch.object(self,'fake',side_effect=checked):self.assertTrue(self.release()['released'])

    def test_codex_terminal_identity_requires_receipt_empty_bash_and_no_children(self):
        self.identity='repo-build|$7|50|%7|100|bash';self.processes='100 1 bash'
        self.pane='luc@host:~/clone$'
        with patch('forge.codex_worker.observe',return_value={'provider':'codex','reason':'codex_execution_completed'}):
            identity=session_identity('repo-build',self.fake,state=self.state)
            self.assertEqual(identity['processes'],[100])
            self.processes+='\n102 100 sleep'
            with self.assertRaises(Unverified):session_identity('repo-build',self.fake,state=self.state)
            self.processes='100 1 bash';self.pane+=' unsent'
            with self.assertRaises(Unverified):session_identity('repo-build',self.fake,state=self.state)
        self.pane='luc@host:~/clone$'
        with patch('forge.codex_worker.observe',return_value={'state':'unknown'}):
            with self.assertRaises(Unverified):session_identity('repo-build',self.fake,state=self.state)

    def test_failed_codex_quota_can_hand_over_without_clearing_claude_guard(self):
        self.identity='repo-build|$7|50|%7|100|bash';self.processes='100 1 bash'
        self.pane='luc@host:~/clone$'
        observation={'provider':'codex','reason':'codex_execution_failed','provider_failure':'usage_limit',
                     'run_id':'run1','thread_id':'thread1'}
        with patch('forge.codex_worker.observe',return_value=observation), patch('forge.provider_handover.observe',return_value=observation):
            with (self.state.parent/'state-supervisor.lock').open('a') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX)
                result=handover.release(self.config,self.state,'repo-build','codex',run=self.fake,now=100,lock_fd=lock.fileno())
        self.assertTrue(result['released'],result)
        self.assertEqual(json.loads((self.state/'legacy-health/guard.json').read_text()),self.guard)
        self.assertEqual((self.clone/'draft.txt').read_text(),'PRESERVE_UNTRACKED')


if __name__=='__main__':unittest.main()
