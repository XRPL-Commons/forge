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
        self.pane = '❯ '
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
        self.pane = 'Old turn esc to interrupt\nFinished\n❯ '
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
