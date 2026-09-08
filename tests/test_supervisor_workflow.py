"""Actual Bash dispatch functions and real workflow/startup CLIs; no host workers.

PATH contains only our tmux boundary and the current Python interpreter. Every
input boundary independently reads the committed SQLite projection, so an
in-memory mock cannot accidentally satisfy the durable-before-send contract.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from forge import workflow
from forge.control import atomic_json, snapshot_path

ROOT = Path(__file__).resolve().parents[1]
GEN = '$1|100|%1|1001'
SHORT = '$1|100|%1'

FAKE_TMUX = r'''import json, os, sys
from pathlib import Path
from forge import workflow
state=Path(os.environ['STATE']); args=sys.argv[1:]
mode=os.environ['FAULT']; log=state/'inputs.jsonl'
if args[0]=='list-panes':
    print('$2|200|%2|2002' if mode=='confirmation' and (state/'entered').exists() else os.environ['FULL_GENERATION'])
elif args[0]=='display-message':
    if args[-1]=='#{pane_current_command}': print('bash')
    elif args[-1]=='#{session_id}|#{session_created}|#{pane_id}': print(os.environ['SHORT_GENERATION'])
    else: raise SystemExit('Unexpected display request: '+repr(args))
elif args[0]=='send-keys':
    literal='-l' in args
    a=workflow.read_state(state)['assignments'][0]
    event={'kind':'literal' if literal else 'enter','state':a['state'],'reason':a['reason'],'text':args[-1]}
    with log.open('a') as f: f.write(json.dumps(event)+'\n')
    if a['state']!='uncertain' or a['reason']!='dispatch_in_progress': raise SystemExit(91)
    if not literal: (state/'entered').touch()
    if mode==('literal' if literal else 'enter'): raise SystemExit(1)
else:
    raise SystemExit('Unexpected tmux operation: '+repr(args))
'''


class SupervisorWorkflowTests(unittest.TestCase):
    def fixture(self, provider, fault='none', eligible=True):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        base=Path(temp.name); state=base/'state'; state.mkdir()
        config=base/'repos.d'; config.mkdir()
        (config/'fixture.repo').write_text('REPO=Fixture/Repo\nOWNER_LABEL=auto:owned-by-fixture\nRECOVERY_AUTHOR=fixture-bot\n')
        (state/'workflow.enabled').touch()
        now=time.time()
        workflow.reconcile(state,'Fixture/Repo','v1',now,[{'number':7,'stage':'queued'}],source_event_id='sync',now=now)
        a=workflow.reserve(state,'Fixture/Repo','build','Repo-build','v1',[7],source_event_id='reserve',now=now)
        workflow.transition(state,a['id'],'bind',generation=GEN,provider=provider,source_event_id='bind',now=now)
        atomic_json(snapshot_path(state,'Fixture/Repo'),{'repo':'Fixture/Repo','status':'ok','enabled':True,
            'observed_at':now,'schedule':{'eligible':[7] if eligible else [],'active':[]},'work':{'held':[]}})
        tools=base/'tools'; tools.mkdir(); (tools/'python3').symlink_to(sys.executable)
        tmux=tools/'tmux'; tmux.write_text('#!'+sys.executable+'\n'+FAKE_TMUX); tmux.chmod(0o700)
        source=(ROOT/'legacy/supervisor.sh').read_text()
        functions='\n'.join(source[source.index(start):source.index(end,source.index(start))] for start,end in (
            ('workflow_enabled(){','declare -A idle'), ('submit_task(){','_nav_prompts(){'),
            ('send_codex_command(){','watch_codex(){')))
        helpers='''
provider_allowed(){ return 0; }
session_generation(){ printf '%s\n' "$SHORT_GENERATION"; }
session_generation_full(){ printf '%s\n' "$FULL_GENERATION"; }
provider_state(){ printf 'idle\n'; }
sess_client(){ return 0; }
glog(){ :; }
activity(){ :; }
sleep(){ :; }
'''
        call='submit_task Repo-build "$SHORT_GENERATION" "Execute assigned issue" initial' if provider=='claude' else 'send_codex_command Repo-build "$FULL_GENERATION" "codex exec assigned issue"'
        env={**os.environ,'PATH':str(tools),'PYTHONPATH':str(ROOT),'STATE':str(state),'DIR':str(base),
             'HOME':str(base),'FULL_GENERATION':GEN,'SHORT_GENERATION':SHORT,'FAULT':fault,
             'BASH_ENV':'','ENV':''}
        script=functions+helpers+'\n'+call+'\n'
        return state,script,env

    def run_dispatch(self, state, script, env):
        result=subprocess.run(['/bin/bash','--noprofile','--norc','-c',script],env=env,cwd=ROOT,
                              capture_output=True,text=True,timeout=20)
        path=state/'inputs.jsonl'
        inputs=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        assignment=workflow.read_state(state)['assignments'][0]
        return result,inputs,assignment

    def test_guard_refusal_never_types_or_enters_for_either_provider(self):
        for provider in ('claude','codex'):
            with self.subTest(provider=provider):
                state,script,env=self.fixture(provider,eligible=False)
                result,inputs,a=self.run_dispatch(state,script,env)
                self.assertNotEqual(result.returncode,0,result.stderr)
                self.assertEqual(inputs,[])
                self.assertEqual(a['state'],'reserved')
                self.assertIn('assignment_no_longer_eligible',result.stderr)

    def test_committed_intent_precedes_literal_and_enter_then_submission(self):
        for provider in ('claude','codex'):
            with self.subTest(provider=provider):
                state,script,env=self.fixture(provider)
                result,inputs,a=self.run_dispatch(state,script,env)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual([x['kind'] for x in inputs],['literal','enter'])
                self.assertTrue(all(x['state']=='uncertain' and x['reason']=='dispatch_in_progress' for x in inputs))
                self.assertEqual(a['state'],'submitted')
                self.assertEqual(json.loads((state/'Repo-build.up').read_text())['status'],'submitted')
                if provider=='claude': self.assertIn('issue #7',inputs[0]['text'])

    def test_partial_delivery_remains_uncertain_and_retry_cannot_duplicate(self):
        for provider in ('claude','codex'):
            for fault in ('literal','enter','confirmation'):
                with self.subTest(provider=provider,fault=fault):
                    state,script,env=self.fixture(provider,fault)
                    result,inputs,a=self.run_dispatch(state,script,env)
                    self.assertNotEqual(result.returncode,0,result.stderr)
                    self.assertEqual(a['state'],'uncertain')
                    self.assertEqual(len(inputs),1 if fault=='literal' else 2)
                    env['FAULT']='none'
                    retry,repeated,a=self.run_dispatch(state,script,env)
                    self.assertNotEqual(retry.returncode,0,retry.stderr)
                    self.assertEqual(repeated,inputs)
                    self.assertEqual(a['state'],'uncertain')


if __name__=='__main__': unittest.main()
