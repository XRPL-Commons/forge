"""Operator actions use real private receipts/locks and an isolated tmux boundary."""
import fcntl
import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import Mock, patch
from types import SimpleNamespace

from forge import slot_actions as actions
from tests.host_isolation import deny_host_tmux


class SlotActionsTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.state=self.root/'state';self.config=self.root/'config';self.config.mkdir()
        (self.config/'repo.repo').write_text('REPO=Owner/Repo\nOWNER_LABEL=auto:owned-by-test\n')
        self.session='Repo-build';self.generation='$1|100|%1|10';self.actual=self.generation;self.calls=[];self.interrupt_failure=False

    def fake_run(self,args,**kwargs):
        self.calls.append(args)
        if args[:2]==['tmux','list-panes']:return CompletedProcess(args,0,self.actual,'')
        if args==['tmux','send-keys','-t','%1','C-c']:return CompletedProcess(args,1 if self.interrupt_failure else 0,'','')
        if args==['tmux','kill-session','-t','$1']:return CompletedProcess(args,0,'','')
        self.fail('Unexpected external operation '+repr(args))

    def request(self,action='stop'):
        return actions.request(self.config,self.state,action,self.session,self.generation,run=self.fake_run,now=100)

    def process(self,now=101):
        with (self.root/'state-supervisor.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            return actions.process(self.config,self.state,self.session,lock_fd=lock.fileno(),run=self.fake_run,now=now)

    def test_stop_is_private_durable_exact_pane_and_pause_does_not_follow_replacement(self):
        result=self.request();self.assertTrue(result['ok'])
        path=self.state/'slot-actions'/(result['id']+'.json')
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777,0o700)
        self.assertEqual(actions.read_actions(self.state)[0]['status'],'queued')
        rows=self.process();self.assertEqual(rows[0]['status'],'succeeded');self.assertTrue(rows[0]['paused'])
        self.assertIn(['tmux','send-keys','-t','%1','C-c'],self.calls)
        self.assertFalse(any(x[:2]==['tmux','kill-session'] for x in self.calls))
        self.assertTrue(actions.is_paused(self.state,self.session,run=self.fake_run))
        self.actual='$2|200|%2|20'
        self.assertFalse(actions.is_paused(self.state,self.session,run=self.fake_run))
        self.assertEqual(self.process(),[])

    def test_stale_generation_and_expired_requests_never_send(self):
        self.request();self.actual='$2|200|%2|20'
        self.assertEqual(self.process()[0]['reason'],'action_generation_changed')
        self.assertFalse(any(x[1]=='send-keys' for x in self.calls))
        self.actual=self.generation;self.request()
        self.assertEqual(self.process(now=401)[0]['reason'],'action_expired')
        self.assertFalse(any(x[1]=='send-keys' for x in self.calls))

    def test_missing_lock_does_not_process_queued_action(self):
        self.request();self.calls.clear()
        with self.assertRaisesRegex(actions.Refused,'supervisor_lock_required'):
            actions.process(self.config,self.state,self.session,lock_fd=-1,run=self.fake_run,now=101)
        self.assertEqual(self.calls,[]);self.assertEqual(actions.read_actions(self.state)[0]['status'],'queued')

    def test_unknown_private_session_and_stale_request_identity_refused(self):
        with self.assertRaisesRegex(actions.Refused,'configured_worker_required'):
            actions.request(self.config,self.state,'stop','private-build',self.generation,run=self.fake_run,now=100)
        self.assertEqual(self.calls,[])
        self.actual='$2|200|%2|20'
        with self.assertRaisesRegex(actions.Refused,'generation_changed'):self.request()
        self.assertEqual(actions.read_actions(self.state),[])

    def test_disabled_configured_survivor_can_be_stopped(self):
        with (self.config/'repo.repo').open('a') as f:f.write('ENABLED=false\n')
        self.request();self.assertEqual(self.process()[0]['status'],'succeeded')

    def test_failed_interrupt_keeps_generation_paused_and_is_not_retried(self):
        self.request();self.interrupt_failure=True
        result=self.process()[0];self.assertEqual(result['reason'],'interrupt_delivery_uncertain')
        self.assertTrue(result['paused']);self.assertTrue(actions.is_paused(self.state,self.session,run=self.fake_run))
        self.assertEqual(self.process(),[])

    def test_dirty_or_unpublished_release_is_refused_without_kill(self):
        for reason in ('worktree_not_clean','head_not_published'):
            with self.subTest(reason=reason):
                self.request('release')
                with patch('forge.handoff.release',return_value={'released':False,'reason':reason}),patch('forge.blocked_work',SimpleNamespace(release=Mock(return_value={'released':False,'reason':reason})),create=True):
                    row=self.process()[0]
                self.assertEqual(row['status'],'refused');self.assertIn(reason,row['proof_reasons'])
                self.assertFalse(any(x[:2]==['tmux','kill-session'] for x in self.calls))

    def test_verified_release_cannot_retarget_replacement_generation(self):
        self.request('release')
        def proof(*args,**kwargs):
            self.actual='$2|200|%2|20'
            kwargs['run'](['tmux','kill-session','-t','$2'])
            self.fail('Replacement kill must be rejected')
        with patch('forge.handoff.release',side_effect=proof):row=self.process()[0]
        self.assertEqual(row['reason'],'release_generation_changed')
        self.assertFalse(any(x[:2]==['tmux','kill-session'] for x in self.calls))

    def test_assignment_change_after_queue_prevents_input(self):
        from forge import workflow as w
        self.request()
        w.reconcile(self.state,'Owner/Repo','v1',100,[{'number':1,'stage':'build'}],source_event_id='sync',now=100)
        w.reserve(self.state,'Owner/Repo','build',self.session,'v1',[1],source_event_id='reserve',now=100)
        self.assertEqual(self.process()[0]['reason'],'action_assignment_changed')
        self.assertFalse(any(x[1]=='send-keys' for x in self.calls))

    def test_real_paused_cli_observes_only_fake_tmux_and_replacement_clears_match(self):
        import os
        import subprocess
        import sys
        self.request();self.process()
        tools=self.root/'tools';tools.mkdir()
        tmux=tools/'tmux';tmux.write_text('#!'+sys.executable+'\nimport os,sys\nassert sys.argv[1]=="list-panes"\nprint(os.environ["FAKE_GENERATION"])\n');tmux.chmod(0o700)
        env={**os.environ,'PATH':str(tools),'PYTHONPATH':str(Path(__file__).resolve().parents[1]),'FAKE_GENERATION':self.generation}
        command=[sys.executable,'-m','forge.slot_actions','paused','--state',str(self.state),'--session',self.session]
        self.assertEqual(subprocess.run(command,env=env,capture_output=True,timeout=10).returncode,0)
        env['FAKE_GENERATION']='$2|200|%2|20'
        self.assertEqual(subprocess.run(command,env=env,capture_output=True,timeout=10).returncode,1)

    def fill_terminal_history(self):
        from forge.control import atomic_json
        directory=self.state/'slot-actions'
        for n in range(150):
            identifier=format(n+1,'032x')
            atomic_json(directory/(identifier+'.json'),{'schema':1,'id':identifier,'session':'Other-build',
                'generation':'$2|200|%2|20','action':'stop','status':'succeeded','requested_at':200+n,'updated_at':200+n})

    def test_pending_request_older_than_100_newer_terminal_rows_is_processed_then_expired(self):
        for now,expected in ((101,'operator_stopped'),(401,'action_expired')):
            with self.subTest(now=now):
                request=self.request();self.fill_terminal_history()
                self.assertNotIn(request['id'],[r['id'] for r in actions.read_actions(self.state)])
                with patch('forge.slot_actions._read',wraps=actions._read) as reads:
                    row=self.process(now=now)[0]
                self.assertEqual(row['id'],request['id']);self.assertEqual(row['reason'],expected)
                self.assertLess(reads.call_count,10) # Never scan terminal JSON per role.
                self.assertFalse((self.state/'slot-actions'/'pending'/self.session/(row['id']+'.json')).exists())

    def test_legacy_unindexed_pending_request_migrates_once_despite_newer_history(self):
        request=self.request();directory=self.state/'slot-actions'
        (directory/'pending'/self.session/(request['id']+'.json')).unlink()
        (directory/'pending'/self.session).rmdir();(directory/'pending').rmdir();(directory/'.pending-indexed').unlink()
        self.fill_terminal_history()
        self.assertEqual(self.process()[0]['id'],request['id'])
        with patch('forge.slot_actions._read',wraps=actions._read) as reads:self.assertEqual(self.process(),[])
        self.assertEqual(reads.call_count,0)

    def test_request_deduplication_and_bounded_outstanding_actions(self):
        first=self.request();again=self.request()
        self.assertEqual(first['id'],again['id']);self.assertTrue(again['deduplicated'])
        self.request('release');self.request('resume')
        self.actual='$2|200|%2|20'
        actions.request(self.config,self.state,'stop',self.session,self.actual,run=self.fake_run,now=101)
        with self.assertRaisesRegex(actions.Refused,'too_many_pending_actions'):
            actions.request(self.config,self.state,'release',self.session,self.actual,run=self.fake_run,now=102)
        self.assertEqual(len(list((self.state/'slot-actions'/'pending'/self.session).glob('*.json'))),4)

    def test_resume_clears_only_matching_generation_pause_without_sending_input(self):
        self.request();self.process();self.calls.clear()
        self.request('resume');row=self.process()[0]
        self.assertEqual(row['reason'],'operator_resumed');self.assertFalse(row['paused'])
        self.assertFalse(actions.is_paused(self.state,self.session,run=self.fake_run))
        self.assertFalse(any(x[1] in {'send-keys','kill-session'} for x in self.calls))

    def test_resume_cannot_clear_a_replacement_or_unrelated_pause(self):
        self.request();self.process();self.request('resume')
        pause=self.state/'slot-actions'/('pause-'+self.session+'.json')
        original=pause.read_text();self.actual='$2|200|%2|20'
        self.assertEqual(self.process()[0]['reason'],'action_generation_changed')
        self.assertEqual(pause.read_text(),original)
        self.actual=self.generation
        saved=json.loads(original);saved['generation']='$3|300|%3|30';pause.write_text(json.dumps(saved))
        self.request('resume')
        self.assertEqual(self.process()[0]['reason'],'pause_generation_changed')
        self.assertEqual(json.loads(pause.read_text())['generation'],'$3|300|%3|30')

    def test_existing_verified_release_result_is_recorded(self):
        self.request('release')
        def proof(*args,**kwargs):
            kwargs['run'](['tmux','kill-session','-t','$1'])
            return {'released':True,'reason':'verified_handoff'}
        with patch('forge.handoff.release',side_effect=proof):row=self.process()[0]
        self.assertEqual(row['status'],'succeeded');self.assertEqual(row['release_reason'],'verified_handoff')
        self.assertIn(['tmux','kill-session','-t','$1'],self.calls)


if __name__=='__main__':unittest.main()
