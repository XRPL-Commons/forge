import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forge import activity


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)/'state'
        self.config = Path(self.tmp.name)/'repos.d'
        self.config.mkdir()
        (self.config/'test.repo').write_text('REPO=o/test\nOWNER_LABEL=auto:owned-by-test\n')
        self.calls = []
        self.pane = 'Working… esc to interrupt'
        self.command = 'claude'

    def runner(self,args):
        self.calls.append(args)
        output = ''
        if args[0]=='list-sessions':
            output = 'test-build\t100\nprivate-dev\t80\n'
        elif args[0]=='list-panes':
            output = '%1\t'+self.command+'\t0\n'
        elif args[0]=='capture-pane':
            output = self.pane
        return subprocess.CompletedProcess(args,0,output,'')

    def sample(self,now):
        return activity.sample(self.state,self.config,now,self.runner)

    def test_existing_busy_has_observation_not_execution_start(self):
        value = self.sample(200)['sessions']['test-build']
        self.assertIsNone(value['execution_started_at'])
        self.assertIsNone(value['dispatched_at'])
        self.assertEqual(value['observed_active_since'],activity.stamp(200))
        self.assertEqual(value['session_started_at'],activity.stamp(100))
        self.assertFalse(any('private-dev' in str(c) for c in self.calls))
        self.assertEqual(os.stat(self.state/'activity.sqlite3').st_mode & 0o777,0o600)

    def test_dispatch_start_and_idle_are_not_completion(self):
        first = activity.record(self.state,'dispatch','test-build','o/test','build',now=150)
        busy = self.sample(200)['sessions']['test-build']
        self.assertEqual(busy['dispatched_at'],activity.stamp(150))
        self.assertIsNone(busy['execution_started_at'])
        self.assertEqual(busy['run_id'],first['run_id'])
        self.pane = '❯ \n  bypass permissions on'
        idle = self.sample(250)['sessions']['test-build']
        self.assertEqual(idle['state'],'idle')
        self.assertEqual(idle['run_id'],first['run_id'])
        self.assertIsNone(idle['observed_active_since'])
        second = activity.record(self.state,'dispatch','test-build','o/test','build',now=300)
        self.assertNotEqual(second['run_id'],first['run_id'])
        self.assertEqual(second['dispatched_at'],activity.stamp(300))
        self.assertFalse(any(e['event']=='completed' for e in activity.read_status(self.state)['events']))

    def test_stable_sample_deduplicates_and_timestamp_advances(self):
        first = self.sample(200)
        second = self.sample(230)
        self.assertEqual(first['events'],second['events'])
        self.assertEqual(second['observed_at'],activity.stamp(230))
        self.assertEqual(first['sessions']['test-build']['last_activity_at'],second['sessions']['test-build']['last_activity_at'])

    def test_old_interrupt_before_new_prompt_is_idle(self):
        self.pane = 'Old turn esc to interrupt\nFinished\n❯ \n  bypass permissions on'
        self.assertEqual(self.sample(200)['sessions']['test-build']['state'],'idle')

    def test_timeout_unknown_not_exited(self):
        self.sample(200)
        def timeout(args):
            raise subprocess.TimeoutExpired(args,3)
        result = activity.sample(self.state,self.config,230,timeout)
        self.assertEqual(result['sessions']['test-build']['state'],'unknown')
        self.assertEqual(result['status'],'degraded')

    def test_read_does_not_create_and_filters_private_sessions(self):
        self.assertEqual(activity.read_status(self.state)['status'],'unavailable')
        self.assertFalse(self.state.exists())
        activity.record(self.state,'dispatch','private-dev',metadata={'secret':'hidden','terminal':'password'},now=100)
        activity.record(self.state,'dispatch','test-build',now=110)
        data = activity.read_status(self.state,{'test-build'})
        self.assertEqual(set(data['sessions']),{'test-build'})
        self.assertEqual([e['session'] for e in data['events']],['test-build'])
        self.assertNotIn('hidden',(self.state/'activity.sqlite3').read_bytes().decode('latin1'))

    def test_pipeline_changed_observation_not_assignment(self):
        path = activity.snapshot_path(self.state,'o/test')
        path.parent.mkdir(parents=True)
        snapshot = {'status':'ok','work':{'wip':[12],'ready':[13]},'counts':{'wip':1,'ready':1}}
        path.write_text(json.dumps(snapshot))
        self.sample(200)
        before = self.sample(230)
        events = [e for e in before['events'] if e['event']=='pipeline_observed' and e['session']=='test-build']
        self.assertEqual(len(events),1)
        self.assertEqual(events[0]['metadata']['claimed'],[12])
        snapshot['work']['wip']=[14]
        path.write_text(json.dumps(snapshot))
        after = self.sample(260)
        self.assertEqual(len([e for e in after['events'] if e['event']=='pipeline_observed' and e['session']=='test-build']),2)

    def test_restart_invalidates_old_run(self):
        self.sample(200)
        activity.record(self.state,'dispatch','test-build',now=205)
        original = self.runner
        def restarted(args):
            result = original(args)
            if args[0]=='list-sessions':
                result.stdout = 'test-build\t210\n'
            return result
        result = activity.sample(self.state,self.config,230,restarted)
        self.assertIsNone(result['sessions']['test-build']['run_id'])
        self.assertIsNone(result['sessions']['test-build']['dispatched_at'])
        self.assertEqual(result['sessions']['test-build']['observed_active_since'],activity.stamp(230))

    def observed(self, birth, now, state='busy'):
        return activity.record(self.state, 'activity_changed', 'test-build',
            metadata={'session_started_at':activity.stamp(birth), 'state':state}, now=now)

    def test_announced_replacement_preserves_new_dispatch_and_rejects_old_capture(self):
        self.observed(100, 200)
        activity.record(self.state, 'session_stopped', 'test-build', now=210)
        activity.record(self.state, 'session_created', 'test-build', now=220)
        dispatch = activity.record(self.state, 'dispatch', 'test-build', now=225)
        late = self.observed(100, 230)
        self.assertIsNone(late['session_started_at'])
        self.assertEqual(late['run_id'], dispatch['run_id'])
        fresh = self.observed(220, 240)
        self.assertEqual(fresh['dispatched_at'], activity.stamp(225))
        self.assertEqual(fresh['run_id'], dispatch['run_id'])
        self.assertEqual(fresh['observed_active_since'], activity.stamp(240))
        self.assertEqual(self.observed(100, 250), fresh)
        self.assertEqual(self.observed(220, 215), fresh)

    def test_tmux_identity_observation_contains_exact_generation_and_ansi_capture(self):
        original = self.runner
        def runner(args):
            result = original(args)
            if args[0] == 'list-sessions':
                result.stdout = 'test-build\t100\t$7\n'
            elif args[0] == 'list-panes':
                result.stdout = '%9\tclaude\t0\t1234\n'
            return result
        result = activity.sample(self.state, self.config, 200, runner)['sessions']['test-build']
        self.assertEqual(result['generation_id'], '$7|100|%9|1234')
        self.assertEqual(result['session_started_epoch'], 100)
        self.assertTrue(any(args[0] == 'capture-pane' and '-e' in args for args in self.calls))

    def test_capture_begun_before_create_cannot_clear_new_dispatch(self):
        original = self.runner
        def runner(args):
            if args[0] == 'list-sessions':
                activity.record(self.state, 'session_created', 'test-build', now=220)
                activity.record(self.state, 'dispatch', 'test-build', now=225)
                return subprocess.CompletedProcess(args, 0, '', '')
            return original(args)
        result = activity.sample(self.state, self.config, 210, runner)['sessions']['test-build']
        self.assertEqual(result['state'], 'starting')
        self.assertEqual(result['dispatched_at'], activity.stamp(225))

    def test_same_second_replacement_uses_exact_generation(self):
        def observe(generation, now):
            return activity.record(self.state, 'activity_changed', 'test-build',
                metadata={'state':'busy', 'session_started_at':activity.stamp(100),
                          'generation_id':generation}, now=now)
        observe('$1|100|%1|1000', 101)
        activity.record(self.state, 'dispatch', 'test-build', now=102)
        fresh = observe('$2|100|%2|2000', 103)
        self.assertIsNone(fresh['run_id'])
        self.assertEqual(fresh['generation_id'], '$2|100|%2|2000')
        self.assertEqual(observe('$1|100|%1|1000', 104), fresh)

    def test_late_capture_cannot_resurrect_stopped_generation(self):
        self.observed(100, 200)
        stopped = activity.record(self.state, 'session_stopped', 'test-build', now=210)
        self.assertEqual(self.observed(100, 230), stopped)
        replacement = self.observed(220, 240)
        self.assertEqual(replacement['state'], 'busy')
        self.assertIsNone(replacement['dispatched_at'])

    def test_dispatch_during_busy_requires_new_observation(self):
        self.sample(200)
        dispatch = activity.record(self.state, 'dispatch', 'test-build', now=210)
        self.assertIsNone(dispatch['observed_active_since'])
        fresh = self.sample(230)['sessions']['test-build']
        self.assertEqual(fresh['observed_active_since'], activity.stamp(230))
        self.assertEqual(fresh['run_id'], dispatch['run_id'])

    def test_lifecycle_decision_is_history_without_worker_progress(self):
        self.sample(200)
        before = activity.record(self.state, 'dispatch', 'test-build', now=210)
        after = activity.record(self.state, 'lifecycle_decision', 'test-build',
            metadata={'phase':'cleanup', 'outcome':'retained', 'reason':'not_idle',
                      'generation_id':'$1|100|%1|456', 'since':215, 'secret':'never'}, now=240)
        self.assertEqual(after, before)
        event = activity.read_status(self.state)['events'][0]
        self.assertEqual(event['metadata']['since'], 215)
        self.assertNotIn('secret', event['metadata'])
        self.assertEqual(activity.metadata_safe({'since':float('nan')}), {})
        self.assertEqual(activity.metadata_safe({'execution_started_at':None}), {})

    def test_classifier_rejects_stale_markers_typed_input_and_other_programs(self):
        cases = [
            ('claude', 'Old esc to interrupt\n❯ typed command\nbypass permissions on', 'unknown'),
            ('vim', 'esc to interrupt', 'unknown'),
            ('claude-helper', 'esc to interrupt', 'unknown'),
            ('claude', '❯\nAllow permission? yes / no', 'unknown'),
            ('claude', '❯\nWorking esc to interrupt', 'busy'),
            ('claude.exe', '❯ \x1b[2mContinue\x1b[0m\nbypass permissions on', 'idle'),
            ('claude', '❯ Continue\nbypass permissions on', 'unknown'),
        ]
        for command, pane, expected in cases:
            with self.subTest(command=command, pane=pane):
                self.assertEqual(activity.classify_pane(command, pane)['state'], expected)

    def test_codex_uses_receipt_generation_and_process_birth_never_terminal_text(self):
        from forge.codex_worker import observe, parse_generation
        receipt = dict(provider='codex',session='test-build',repo='o/test',
            generation=parse_generation('$7|100|%9|1234'),run_id='11111111-1111-4111-8111-111111111111',
            thread_id='22222222-2222-4222-8222-222222222222',state='running',started_at=190,observed_at=195,
            runner={'pid':2000,'parent_pid':1234,'start_ticks':10},
            provider_process={'pid':2001,'parent_pid':2000,'start_ticks':20})
        path=self.state/'codex-workers/test-build.json'
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(receipt))
        self.command='python3'
        def runner(args):
            if args[0]=='list-sessions':
                return subprocess.CompletedProcess(args,0,'test-build\t100\t$7\n','')
            if args[0]=='list-panes':
                return subprocess.CompletedProcess(args,0,'%9\tpython3\t0\t1234\n','')
            self.fail('Codex must never be classified through terminal capture')
        def process(pid):
            return {'pid':pid,'start_ticks':10 if pid==2000 else 20}
        def inspect(*args,**kwargs):
            return observe(*args,**kwargs,now=200,inspect_process=process)
        value=activity.observe_tmux({'test-build'},runner,state=self.state,codex_observer=inspect)['test-build']
        self.assertEqual(value['state'],'busy')
        self.assertEqual(value['provider'],'codex')
        self.assertEqual(value['provider_run_id'],receipt['run_id'])
        self.assertEqual(value['session_started_at'],activity.stamp(100))
        # PID reuse is not the old execution still running.
        receipt['state']='completed';path.write_text(json.dumps(receipt))
        def reused(pid): return {'pid':pid,'start_ticks':999}
        def inspect_finished(*args,**kwargs):
            return observe(*args,**kwargs,now=200,inspect_process=reused)
        value=activity.observe_tmux({'test-build'},runner,state=self.state,codex_observer=inspect_finished)['test-build']
        self.assertEqual(value['state'],'idle')
        # A receipt from another exact tmux generation never labels this worker Codex.
        receipt['generation']=parse_generation('$8|100|%9|1234');path.write_text(json.dumps(receipt))
        value=activity.observe_tmux({'test-build'},runner,state=self.state,codex_observer=inspect)['test-build']
        self.assertEqual(value['state'],'unknown')
        self.assertEqual(value['provider'],'unknown')

    def test_claude_foreground_is_not_overridden_by_old_codex_receipt(self):
        path=self.state/'codex-workers/test-build.json'
        path.parent.mkdir(parents=True);path.write_text('{}')
        def forbidden(*args,**kwargs): self.fail('Claude should use its own classifier')
        value=activity.observe_tmux({'test-build'},self.runner,state=self.state,codex_observer=forbidden)['test-build']
        self.assertEqual(value['provider'],'claude')
        self.assertEqual(value['state'],'busy')

    def test_classifier_real_module_cli_needs_no_state_or_config(self):
        result = subprocess.run([os.sys.executable, '-m', 'forge.activity', 'classify',
            '--command', 'claude', '--state-only'], input='❯\nbypass permissions on',
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, 'idle\n')
        self.assertFalse(self.state.exists())

    def test_append_only_and_shell(self):
        self.command='bash'
        self.assertEqual(self.sample(200)['sessions']['test-build']['state'],'shell')
        db=sqlite3.connect(self.state/'activity.sqlite3')
        self.addCleanup(db.close)
        with self.assertRaises(sqlite3.IntegrityError):
            db.execute('DELETE FROM events')

    def test_cli_rejects_private_session_and_keeps_capacity_reason(self):
        common=['activity','--state',str(self.state),'--config',str(self.config),'event']
        with patch('sys.argv',common+['--session','private-dev','--event','dispatch']):
            with self.assertRaises(SystemExit):
                activity.main()
        self.state.mkdir()
        (self.state/'capacity.json').write_text(json.dumps(dict(requested_session='test-build',reason='role_session_limit',admitted=False,fleet_count=4,secret='do-not-store',role_counts={'build':2},limits={'max_sessions':6})))
        with patch('sys.argv',common+['--session','test-build','--event','admission_deferred','--reason','capacity_guard']):
            activity.main()
        event=activity.read_status(self.state)['events'][0]
        self.assertEqual(event['metadata']['capacity']['reason'],'role_session_limit')
        self.assertNotIn('secret',event['metadata']['capacity'])


if __name__=='__main__':
    unittest.main()
