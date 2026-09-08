"""Exercise the complete shell supervisor with fake GitHub/tmux, never live work."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import unittest.mock

ROOT = Path(__file__).parents[1]


class SupervisorTests(unittest.TestCase):
    def run_pass(self, labels, *, gh_fail=False, capacity_allowed=True, config_extra="", foreground="claude", capacity_sequence=None, human_wait_verified=False, existing_sessions=(), provider_available=True, pane="❯\n────\n ⏵⏵ bypass permissions on", capture_fail=False, task_send_fail=None, empty_inventory=False, selected_provider="claude"):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for directory in ('repos.d', 'bin', 'roles', 'mockbin'):
                (root / directory).mkdir()
            shutil.copy2(ROOT / 'legacy/supervisor.sh', root / 'supervisor.sh')
            shutil.copy2(ROOT / 'bin/forge', root / 'bin/forge')
            shutil.copytree(ROOT / 'forge', root / 'forge', ignore=shutil.ignore_patterns('__pycache__'))
            (root / 'repos.d/fixture.repo').write_text('REPO="o/fixture"\nOWNER_LABEL="auto:owned-by-me"\n' + config_extra)
            (root / 'notify.sh').write_text('#!/bin/sh\nexit 0\n')
            (root / 'notify.sh').chmod(0o755)
            executable = '''#!/usr/bin/python3
import json, os, sys
name=os.path.basename(sys.argv[0])
with open(os.environ['CALLS'], 'a') as f: f.write(json.dumps([name]+sys.argv[1:])+'\\n')
if name=='python3':
 if sys.argv[1:3]==['-m','forge.routing']:
  if 'select' in sys.argv: print(os.environ['SELECTED_PROVIDER']); sys.exit(0)
  if 'current' in sys.argv: sys.exit(1)
  sys.exit(0)
 if sys.argv[1:4]==['-m','forge.human_wait','check'] and os.environ['HUMAN_WAIT_VERIFIED']=='1': sys.exit(0)
 if sys.argv[1:4]==['-m','forge.capacity','check']:
  sequence=json.loads(os.environ['CAPACITY_SEQUENCE'])
  count=sum(1 for line in open(os.environ['CALLS']) if json.loads(line)[:4]==['python3','-m','forge.capacity','check'])
  allowed=sequence[min(count-1,len(sequence)-1)] if sequence else os.environ['CAPACITY_ALLOWED']=='1'
  sys.exit(0 if allowed else 1)
 os.execv('/usr/bin/python3', ['/usr/bin/python3']+sys.argv[1:])
elif name=='gh':
 if os.environ['GH_FAIL']=='1': sys.exit(1)
 if '/labels?' in sys.argv[-1]: print('[[{"name":"auto:owned-by-me"}]]')
 elif os.environ['EMPTY_INVENTORY']=='1': print('[]')
 else: print(json.dumps([[{'number':1,'pull_request':{},'labels':[{'name':x} for x in json.loads(os.environ['TEST_LABELS'])],'user':{'login':'forge-bot'}}]]))
elif name=='tmux':
 if sys.argv[1]=='display-message':
  print('$1|100|%1|10' if sys.argv[-1]=='#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}' else '$1|100|%1' if sys.argv[-1]=='#{session_id}|#{session_created}|#{pane_id}' else os.environ['FOREGROUND'])
 if sys.argv[1]=='list-panes':
  fmt=sys.argv[-1]
  if fmt=='#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}|#{pane_current_command}|#{pane_current_path}':
   name=sys.argv[sys.argv.index('-t')+1].lstrip('=');print('$1|100|%1|10|'+os.environ['FOREGROUND']+'|'+os.environ['HOME']+'/'+name)
 if sys.argv[1]=='capture-pane':
  if os.environ['CAPTURE_FAIL']=='1': sys.exit(1)
  print(os.environ['TEST_PANE'])
 if sys.argv[1]=='send-keys' and '%1' in sys.argv:
  failure=os.environ['TASK_SEND_FAIL']
  if failure=='literal' and '-l' in sys.argv or failure=='enter' and sys.argv[-1]=='Enter': sys.exit(1)
 if sys.argv[1]=='has-session': sys.exit(0 if sys.argv[-1].lstrip('=') in json.loads(os.environ['EXISTING_SESSIONS']) else 1)
 if sys.argv[1]=='ls': sys.exit(1)
elif name=='git' and 'get-url' in sys.argv: print('git@github.com:o/fixture.git')
elif name=='git' and '--show-toplevel' in sys.argv: print(sys.argv[sys.argv.index('-C')+1])
elif name=='ps': print('10 1 bash')
'''
            for name in ('gh', 'tmux', 'git', 'sleep', 'python3', 'claude', 'codex', 'ps'):
                path = root / 'mockbin' / name
                path.write_text(executable)
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(root / 'mockbin') + ':' + os.environ['PATH'],
                       FORGE_ONCE='1', FORGE_QUIET_START='1', SELECTED_PROVIDER=selected_provider, FORGE_CODEX_BIN=str(root/'mockbin/codex'), FORGE_CLAUDE_BIN=str(root/'mockbin'/('claude' if provider_available else 'missing-claude')), HOME=str(root), CALLS=str(root / 'calls'),
                       TEST_PANE=pane, CAPTURE_FAIL='1' if capture_fail else '0', TASK_SEND_FAIL=task_send_fail or '', EMPTY_INVENTORY='1' if empty_inventory else '0', TEST_LABELS=json.dumps(labels), GH_FAIL='1' if gh_fail else '0',
                       CAPACITY_ALLOWED='1' if capacity_allowed else '0', FOREGROUND=foreground, CAPACITY_SEQUENCE=json.dumps(capacity_sequence or []), HUMAN_WAIT_VERIFIED='1' if human_wait_verified else '0', EXISTING_SESSIONS=json.dumps(existing_sessions))
            result = subprocess.run(['bash', str(root / 'supervisor.sh')], env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((root / 'executed').exists(), 'repository configuration executed shell code')
            self.last_markers={p.name:json.loads(p.read_text()) for p in (root/'state').glob('*.up')}
            self.last_uncertain=list((root/'state/dispatch-uncertain').glob('*.json'))
            return [json.loads(line) for line in (root / 'calls').read_text().splitlines()]

    def test_audit_ready_launches_only_auditor(self):
        calls = self.run_pass(['ready-for-audit'])
        launches = [c for c in calls if c[:2] == ['tmux', 'new-session']]
        self.assertEqual(len(launches), 1)
        self.assertIn('fixture-audit', launches[0])
        self.assertEqual(len([c for c in calls if c[0] == 'gh']), 5)  # snapshot + fresh ownership/inventory/current PR checks

    def test_native_executable_gets_full_initial_role_dispatch(self):
        calls=self.run_pass(['ready-for-audit'],foreground='claude.exe')
        commands=[c[-1] for c in calls if c[:2]==['tmux','send-keys']]
        self.assertTrue(any('Read your role file at ' in value for value in commands))
        self.assertFalse(any(c[:4]==['python3','-m','forge.legacy_health','start_failed'] for c in calls))

    def test_verified_human_wait_does_not_launch_or_nudge_auditor(self):
        calls = self.run_pass(['ready-for-audit'], human_wait_verified=True)
        self.assertTrue(any(c[:4] == ['python3', '-m', 'forge.human_wait', 'check'] for c in calls))
        self.assertFalse(any(c[:2] in (['tmux', 'new-session'], ['tmux', 'send-keys']) for c in calls))
        self.assertFalse(any(c[:4] == ['python3', '-m', 'forge.capacity', 'check'] for c in calls))

    def test_empty_auditor_startup_checked_before_human_wait(self):
        calls=self.run_pass(['ready-for-audit'], human_wait_verified=True,
                            existing_sessions=['fixture-audit'], foreground='bash')
        startup=next(i for i,c in enumerate(calls) if c[:4]==['python3','-m','forge.startup','release'])
        human=next(i for i,c in enumerate(calls) if c[:4]==['python3','-m','forge.human_wait','check'])
        self.assertLess(startup,human)
        self.assertFalse(any(c[:2]==['tmux','send-keys'] for c in calls))

    def test_completed_auditor_is_considered_for_cleanup_while_other_role_has_work(self):
        calls = self.run_pass([], existing_sessions=['fixture-audit'])
        retirement = [c for c in calls if c[:4] == ['python3', '-m', 'forge.handoff', 'release']]
        self.assertTrue(any(c[-1] == 'fixture-audit' for c in retirement))

    def test_hold_launches_nobody(self):
        self.assertFalse(any(c[:2] == ['tmux', 'new-session'] for c in self.run_pass(['ready-for-audit', 'hold'])))

    def test_github_failure_launches_nobody(self):
        self.assertFalse(any(c[:2] == ['tmux', 'new-session'] for c in self.run_pass([], gh_fail=True)))

    def test_capacity_denial_does_not_launch_or_clone(self):
        calls = self.run_pass(['ready-for-audit'], capacity_allowed=False)
        self.assertFalse(any(c[:2] == ['tmux', 'new-session'] or c[0] == 'git' for c in calls))
        self.assertTrue(any(c[:4] == ['python3', '-m', 'forge.capacity', 'check'] for c in calls))

    def test_shell_configuration_never_executes_and_blocks_admission(self):
        calls = self.run_pass(['ready-for-audit'], config_extra='touch "$HOME/executed"\n')
        self.assertFalse(any(c[0] == 'gh' or c[:2] == ['tmux', 'new-session'] for c in calls))

    def test_empty_builder_startup_is_checked_before_generic_shell_parking(self):
        calls = self.run_pass([], foreground='bash', existing_sessions=('fixture-build',))
        recovery = next(i for i,c in enumerate(calls) if c[:4] == ['python3','-m','forge.startup','release'])
        self.assertGreaterEqual(recovery,0)
        self.assertFalse(any(c[:4] == ['python3','-m','forge.legacy_health','park'] for c in calls))
        self.assertFalse(any(c[:2] == ['tmux','kill-session'] for c in calls))
        self.assertFalse(any('Read your role' in arg for c in calls for arg in c))

    def test_failed_client_start_never_receives_role_text_in_shell(self):
        calls = self.run_pass(['ready-for-audit'], foreground='bash')
        sent = [c for c in calls if c[:2] == ['tmux', 'send-keys']]
        self.assertEqual(len(sent), 2)
        self.assertTrue(any('--dangerously-skip-permissions' in arg for arg in sent[0]))
        self.assertTrue(any(c[:4] == ['python3','-m','forge.legacy_health','start_failed'] for c in calls))
        self.assertFalse(any('Read your role' in arg for c in sent for arg in c))

    def test_missing_provider_is_detected_before_session_or_launch_budget(self):
        calls=self.run_pass(['ready-for-audit'],provider_available=False)
        self.assertFalse(any(c[:2]==['tmux','new-session'] for c in calls))
        self.assertFalse(any(c[:4]==['python3','-m','forge.legacy_health','launch'] for c in calls))
        self.assertTrue(any(c[:4]==['python3','-m','forge.legacy_health','start_failed'] for c in calls))

    def test_unready_or_unreadable_provider_never_gets_task_or_marker(self):
        for kwargs in ({'pane':'Initializing...'}, {'pane':'❯ typed input\n────\n ⏵⏵ bypass permissions on'}, {'capture_fail':True}):
            with self.subTest(kwargs=kwargs):
                calls=self.run_pass(['ready-for-audit'],**kwargs)
                self.assertFalse(any(c[:2]==['tmux','send-keys'] and 'Read your role' in c[-1] for c in calls))
                self.assertEqual(self.last_markers,{})

    def test_partial_task_send_is_uncertain_without_marker_or_dispatch(self):
        for step in ('literal','enter'):
            with self.subTest(step=step):
                calls=self.run_pass(['ready-for-audit'],task_send_fail=step)
                self.assertEqual(self.last_markers,{})
                self.assertTrue(self.last_uncertain)
                self.assertFalse(any('--event' in c and 'dispatch' in c for c in calls))

    def test_rejected_cleanup_never_falls_through_to_unconditional_teardown(self):
        with unittest.mock.patch.dict(os.environ,{'SLEEP_AFTER_EMPTY_POLLS':'1'}):
            calls=self.run_pass([],empty_inventory=True,existing_sessions=['fixture-review'])
        self.assertTrue(any(c[:4]==['python3','-m','forge.handoff','release'] for c in calls))
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in calls))

    def test_selected_codex_submits_structured_runner_and_binds_provider(self):
        calls=self.run_pass(['ready-for-audit'],selected_provider='codex',foreground='bash',pane='user@host:~/fixture-audit$')
        commands=[c[-1] for c in calls if c[:2]==['tmux','send-keys'] and '-l' in c]
        self.assertTrue(any('forge.codex_worker run' in c and '--prompt-file' in c for c in commands))
        self.assertFalse(any('--dangerously-skip-permissions' in c for c in commands))
        self.assertTrue(any(c[:3]==['python3','-m','forge.routing'] and 'bind' in c and 'codex' in c for c in calls))
        self.assertEqual(self.last_markers['fixture-audit.up']['status'],'submitted')

    def test_capacity_is_rechecked_after_clone_without_charging_launch_budget(self):
        calls = self.run_pass(['ready-for-audit'], capacity_sequence=[True, False])
        self.assertTrue(any(c[:2] == ['git', 'clone'] for c in calls))
        self.assertFalse(any(c[:2] == ['tmux', 'new-session'] for c in calls))
        self.assertFalse(any(c[:4] == ['python3', '-m', 'forge.legacy_health', 'launch'] for c in calls))


class WatchLifecycleTests(unittest.TestCase):
    def simulate(self, operations):
        source=(ROOT/'legacy/supervisor.sh').read_text()
        watch=source[source.index('watch(){'):source.index('\nrole_reconcile(){')]
        script="""set -uo pipefail
        exec 3>&1
        declare -A idle nudges seen_generation prev last_progress
        GEN='$1|100|%1'; OBS=idle; FAIL=0; PROGRESS=base; STATE=/tmp; DIR=/tmp
        python3(){ echo "$PROGRESS"; }
        actual_provider(){ echo claude; }
        route_success(){ :; }
        session_generation(){ echo "$GEN"; }
        provider_state(){ echo "$OBS"; }
        tmux(){ echo 'ready'; }
        glog(){ :; }
        activity(){ :; }
        health(){ [ "$1" != observe ] || cat >/dev/null; [ "$1" != park ] || echo PARK >&3; return 0; }
        submit_task(){ [ "$FAIL" = 0 ] || return 1; echo SENT; }
        """+watch+'\n'+operations
        result=subprocess.run(['bash'],input=script,text=True,capture_output=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        return (result.stdout+result.stderr).splitlines()

    def test_unknown_ui_never_nudged_or_parked(self):
        self.assertEqual(self.simulate('OBS=unknown; for i in {1..8}; do watch s short 1 task; done'),[])

    def test_failed_send_does_not_consume_delivered_nudge_budget(self):
        self.assertEqual(self.simulate('FAIL=1; for i in {1..8}; do watch s short 1 task; done'),[])

    def test_new_generation_resets_previous_generation_nudge_budget(self):
        lines=self.simulate("for i in {1..4}; do watch s short 1 task; done; GEN='$2|200|%2'; for i in {1..2}; do watch s short 1 task; done")
        self.assertEqual(lines,['SENT','SENT','SENT'])

    def test_two_successful_nudges_without_observed_progress_are_bounded(self):
        self.assertEqual(self.simulate('for i in {1..6}; do watch s short 1 task; done'),['SENT','SENT','PARK'])


    def test_authoritative_progress_between_polls_resets_nudge_budget(self):
        lines=self.simulate("for i in {1..4}; do watch s short 1 task; done; PROGRESS=new; for i in {1..2}; do watch s short 1 task; done")
        self.assertEqual(lines,['SENT','SENT','SENT'])

    def test_unknown_progress_does_not_refund_nudges(self):
        lines=self.simulate("for i in {1..4}; do watch s short 1 task; done; PROGRESS=''; for i in {1..2}; do watch s short 1 task; done")
        self.assertEqual(lines,['SENT','SENT','PARK'])
