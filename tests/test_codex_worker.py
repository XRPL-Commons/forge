import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

from forge import codex_worker as worker


class CodexWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root/'state'
        self.clone = self.root/'clone'; self.clone.mkdir()
        self.prompt = self.root/'prompt'; self.prompt.write_text('Private task instruction SECRET_PROMPT')
        self.executable = self.root/'fake-codex'
        self.thread = str(uuid.uuid4())
        self.generation = '$2|100|%3|9999999'
        self.calls = []
        self.script()

    def script(self, body=None):
        body = body or f'''import json,sys
prompt=sys.stdin.read()
assert 'SECRET_PROMPT' in prompt
assert 'SECRET_PROMPT' not in ' '.join(sys.argv)
assert '--approve-for-me' in sys.argv and '--sandbox' not in sys.argv
assert '--dangerously-bypass-approvals-and-sandbox' not in sys.argv
assert '--model' not in sys.argv and '--ignore-user-config' not in sys.argv
print(json.dumps({{'type':'thread.started','thread_id':'{self.thread}'}}))
print(json.dumps({{'type':'item.completed','item':{{'type':'agent_message','text':'SECRET_OUTPUT'}}}}))
print(json.dumps({{'type':'turn.completed'}}))
print('SECRET_STDERR',file=sys.stderr)
'''
        self.executable.write_text('#!'+sys.executable+'\n'+body)
        self.executable.chmod(0o700)

    def verify(self, session, generation):
        self.calls.append((session,generation))
        self.assertEqual(generation['id'], self.generation)

    def run_worker(self, **kwargs):
        return worker.run_once(self.state,'repo-build','owner/repo','build',self.clone,
            self.prompt,self.executable,self.generation,verify=self.verify,**kwargs)

    def receipt(self):
        return json.loads((self.state/'codex-workers/repo-build.json').read_text())

    def test_structured_execution_private_logs_and_no_task_completion_claim(self):
        result = self.run_worker()
        self.assertEqual(result['state'],'completed')
        self.assertEqual(result['thread_id'],self.thread)
        self.assertTrue(result['execution_only'])
        self.assertEqual(len(self.calls),2)
        serialized=json.dumps(result)
        for secret in ('SECRET_PROMPT','SECRET_OUTPUT','SECRET_STDERR'):
            self.assertNotIn(secret,serialized)
        raw=Path(result['raw_log'])
        self.assertIn('SECRET_OUTPUT',raw.read_text())
        self.assertEqual(raw.stat().st_mode & 0o777,0o600)
        self.assertEqual((self.state/'codex-workers/repo-build.json').stat().st_mode & 0o777,0o600)
        self.assertGreater(result['provider_process']['start_ticks'],0)

    def test_known_cli_flag_conflict_is_startup_configuration_only(self):
        error = "error: the argument '--sandbox <SANDBOX_MODE>' cannot be used with '--approve-for-me'"
        self.script("import sys; print(" + repr(error) + ",file=sys.stderr); sys.exit(2)")
        result = self.run_worker()
        self.assertEqual(result['reason'], 'startup_configuration')
        self.assertTrue(worker.startup_configuration_failure(result))
        for changes in ({'thread_id':self.thread}, {'event_count':1}, {'exit_code':1},
                        {'remaining_processes':[{'pid':1}]}, {'protocol_error':True}):
            self.assertFalse(worker.startup_configuration_failure(dict(result, **changes)))
        Path(result['raw_log']).write_text('task output')
        self.assertFalse(worker.startup_configuration_failure(result))

    def test_exec_and_resume_do_not_mix_mutually_exclusive_sandbox_flags(self):
        for thread in (None,self.thread):
            args = worker.command(self.executable,self.clone,thread)
            self.assertIn('--approve-for-me',args)
            self.assertNotIn('--sandbox',args)
            self.assertNotIn('--dangerously-bypass-approvals-and-sandbox',args)

    def test_typed_missing_code_mode_host_fails_even_when_turn_completes(self):
        message = 'Code Mode is unavailable because failed to spawn code-mode host /private/runtime/codex-code-mode-host: host executable was not found. Code mode will fail closed; enable features.code_mode_host and install codex-code-mode-host.'
        events = [{'type':'thread.started','thread_id':self.thread},
                  {'type':'item.completed','item':{'type':'error','message':message}},
                  {'type':'turn.completed'}]
        self.script('import json\nfor event in '+repr(events)+': print(json.dumps(event))')
        result=self.run_worker()
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['reason'],'runtime_unavailable')
        self.assertEqual(result['runtime_failure'],'code_mode_host_unavailable')
        self.assertIsNone(result['provider_failure'])
        self.assertEqual(result['thread_id'],self.thread)
        self.assertNotIn('/private/runtime',json.dumps(result))
        def gone(pid): raise ProcessLookupError(pid)
        observed=worker.observe(self.state,'repo-build',generation=self.generation,inspect_process=gone)
        self.assertEqual(observed['state'],'blocked')
        self.assertEqual(observed['reason'],'runtime_unavailable')
        for item in ({'type':'agent_message','text':message},
                     {'type':'command_execution','message':message},
                     {'type':'error','message':'A test assertion failed'}):
            receipt={'event_count':0}
            worker.apply_event(receipt,{'type':'item.completed','item':item})
            self.assertNotIn('runtime_failure',receipt)
            self.assertNotIn('turn_failed',receipt)

    def test_zero_exit_without_completed_turn_is_incomplete(self):
        self.script('print("{}")')
        result=self.run_worker()
        self.assertEqual(result['state'],'failed')
        self.assertEqual(result['reason'],'execution_incomplete')
        self.assertIsNone(result['provider_failure'])

    def test_quota_is_explicit_provider_failure_without_raw_error(self):
        self.script("import json;print(json.dumps({'type':'turn.failed','error':{'code':'usage_limit_reached','message':'PRIVATE_ACCOUNT_DETAILS'}}))")
        result=self.run_worker()
        self.assertEqual(result['provider_failure'],'usage_limit')
        self.assertNotIn('PRIVATE_ACCOUNT_DETAILS',json.dumps(result))
        self.assertEqual(result['state'],'failed')
        self.assertIsNone(worker.provider_failure({'type':'turn.failed','error':{'message':'Tests failed'}}))
        self.assertIsNone(worker.provider_failure({'type':'item.completed','message':'File permission denied'}))

    def test_exact_resume_requires_same_bound_receipt(self):
        with self.assertRaises(worker.Refused): self.run_worker(resume_thread=self.thread)
        self.run_worker()
        self.assertEqual(self.run_worker(resume_thread=self.thread)['state'],'completed')
        with self.assertRaises(worker.Refused): self.run_worker(resume_thread=str(uuid.uuid4()))
        args=worker.command(self.executable,self.clone,self.thread)
        self.assertEqual(args[-3:],['resume',self.thread,'-'])
        self.assertNotIn('--last',args)

    def test_generation_replacement_never_spawns(self):
        with patch('forge.codex_worker.subprocess.Popen') as spawn:
            with self.assertRaises(worker.Refused):
                worker.run_once(self.state,'repo-build','owner/repo','build',self.clone,
                    self.prompt,self.executable,self.generation,verify=lambda *_: (_ for _ in ()).throw(worker.Refused('generation_changed')))
        spawn.assert_not_called()

    def test_duplicate_runner_lock_is_fail_closed(self):
        directory=self.state/'codex-workers'; directory.mkdir(parents=True,mode=0o700)
        with (directory/'repo-build.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            with self.assertRaisesRegex(worker.Refused,'worker_already_running'): self.run_worker()

    def test_deadline_stops_only_fake_provider_group_and_records_failure(self):
        self.script('import time;time.sleep(30)')
        start=time.monotonic()
        result=self.run_worker(max_seconds=0.2)
        self.assertLess(time.monotonic()-start,6)
        self.assertEqual(result['reason'],'execution_deadline_exceeded')
        self.assertEqual(result['state'],'failed')
        with self.assertRaises(FileNotFoundError): worker.process_identity(result['provider_process']['pid'])

    def test_closed_output_does_not_bypass_execution_deadline(self):
        self.script('import os,time;os.close(1);os.close(2);time.sleep(30)')
        self.assertEqual(self.run_worker(max_seconds=0.2)['reason'],'execution_deadline_exceeded')

    def test_log_size_cap_keeps_draining_and_never_copies_payload_to_receipt(self):
        with patch('forge.codex_worker.MAX_LOG',16): result=self.run_worker()
        self.assertTrue(result['logs_truncated'])
        self.assertEqual(Path(result['raw_log']).stat().st_size,16)
        self.assertEqual(result['state'],'completed')

    def test_wrong_resumed_thread_and_malformed_events_fail_closed(self):
        self.script("print('not JSON')")
        self.assertTrue(self.run_worker()['protocol_error'])
        self.script();self.run_worker()
        self.script("import json;print(json.dumps({'type':'thread.started','thread_id':'"+str(uuid.uuid4())+"'}));print(json.dumps({'type':'turn.completed'}))")
        self.assertEqual(self.run_worker(resume_thread=self.thread)['state'],'failed')

    def test_observation_requires_generation_and_process_birth(self):
        result=self.run_worker()
        def absent(_): raise FileNotFoundError()
        observation=worker.observe(self.state,'repo-build',generation=self.generation,repo='owner/repo',inspect_process=absent)
        self.assertEqual(observation['state'],'idle')
        self.assertEqual(observation['generation_id'],self.generation)
        self.assertEqual(observation['started_at'],result['started_at'])
        wrong=worker.observe(self.state,'repo-build',generation='$9|100|%3|9999999',inspect_process=absent)
        self.assertNotIn('provider',wrong)
        result['state']='running';worker.private_json(self.state/'codex-workers/repo-build.json',result)
        matching=lambda pid: next(p for p in (result['runner'],result['provider_process']) if p['pid']==pid)
        self.assertEqual(worker.observe(self.state,'repo-build',generation=self.generation,inspect_process=matching)['state'],'busy')
        self.assertEqual(worker.observe(self.state,'repo-build',generation=self.generation,inspect_process=lambda pid:{'start_ticks':0})['reason'],'codex_runner_lost')

    def test_old_completed_receipt_not_stale_when_exact_generation_is_still_current(self):
        result=self.run_worker()
        def absent(_): raise FileNotFoundError()
        observation=worker.observe(self.state,'repo-build',generation=self.generation,now=result['observed_at']+3600,inspect_process=absent)
        self.assertEqual(observation['state'],'idle')
        result['state']='running';worker.private_json(self.state/'codex-workers/repo-build.json',result)
        self.assertEqual(worker.observe(self.state,'repo-build',generation=self.generation,now=result['observed_at']+3600,inspect_process=absent)['reason'],'codex_receipt_stale')

    def test_missing_receipt_cli_never_probes_tmux_or_claims_provider(self):
        command=[sys.executable,'-m','forge.codex_worker','observe','--state',str(self.state),'--session','repo-build','--state-only']
        result=subprocess.run(command,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0)
        self.assertEqual(result.stdout.strip(),'unknown')


    def test_background_children_remain_reserved_and_block_next_execution(self):
        self.script(f"""import subprocess,sys,json
subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
print(json.dumps({{'type':'thread.started','thread_id':'{self.thread}'}}))
print(json.dumps({{'type':'turn.completed'}}))
""")
        result=self.run_worker()
        remaining=result['remaining_processes']
        try:
            self.assertEqual(result['reason'],'execution_background_processes')
            self.assertEqual(len(remaining),1)
            with self.assertRaisesRegex(worker.Refused,'prior_background_work_running'):
                self.run_worker()
        finally:
            import signal
            for recorded in remaining:
                try:
                    if worker.process_identity(recorded['pid'])['start_ticks']==recorded['start_ticks']:
                        os.kill(recorded['pid'],signal.SIGTERM)
                except FileNotFoundError:
                    pass


if __name__=='__main__':unittest.main()
