"""One-assignment workers release before unrelated queued work is admitted."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
from tests.host_isolation import deny_host_tmux


class AssignmentCleanupTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)

    def run_role(self, role='review', *, tracked=True, busy=False, released=True, terminal=False, handover=False, reconciled=False, initial_quarantine=False):
        source=(Path(__file__).resolve().parents[1]/'legacy/supervisor.sh').read_text()
        function=source[source.index('role_reconcile(){'):source.index('\nglog "=== supervisor started')]
        retirement=source[source.index('reconcile_finished_workspace(){'):source.index('launch(){')]
        script='''occupied=1
if [ "$INITIAL_QUARANTINE" = 1 ]; then touch "$STATE/quarantine"; occupied=0; fi
DIR=/unused
workflow_enabled(){ return 0; }
sess_up(){ [ "$occupied" = 1 ]; }
maybe_handover(){ if [ "$HANDOVER" = 1 ]; then occupied=0; echo handover; return 0; fi; return 1; }
actual_provider(){ echo codex; }
sess_client(){ return 0; }
workflow_call(){ [ "$TRACKED" = 1 ]; }
provider_state(){ if [ "$BUSY" = 1 ]; then echo busy; else echo idle; fi; }
release_worker(){ echo "release $1"; if [ "$1" = forge.execution_retirement ] && [ "$TERMINAL" = 1 ]; then occupied=0; touch "$STATE/quarantine"; return 0; fi; if [ "$RELEASED" = 1 ]; then occupied=0; return 0; fi; return 1; }
workspace_available(){ [ ! -f "$STATE/quarantine" ]; }
glog(){ echo "$*" >> "$STATE/log"; }
health(){ return 0; }
python3(){
  if [ "${2:-}" = forge.execution_reconciliation ]; then
    echo reconcile >> "$STATE/actions"
    if [ "$RECONCILED" = 1 ]; then rm "$STATE/quarantine"; return 0; fi
  fi
  return 1
}
watch(){ echo watch; }
launch(){ echo launch; }
declare -A human_waiting
'''+retirement+function+'\nrole_reconcile "fixture-$ROLE" /unused role.md task 2 fixture Org/Fixture\n'
        env={**os.environ,'ROLE':role,'TRACKED':str(int(tracked)),'BUSY':str(int(busy)),'RELEASED':str(int(released)),'TERMINAL':str(int(terminal)),'HANDOVER':str(int(handover)),'RECONCILED':str(int(reconciled)),'INITIAL_QUARANTINE':str(int(initial_quarantine))}
        with tempfile.TemporaryDirectory() as state:
            result=subprocess.run(['/bin/bash','-eu','-c',script],env={**env,'STATE':state},capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            actions=Path(state)/'actions'
            self.reconcile_calls=actions.read_text().splitlines() if actions.exists() else []
            self.quarantine_retained=(Path(state)/'quarantine').exists()
            self.logged=(Path(state)/'log').read_text() if (Path(state)/'log').exists() else ''
            return result.stdout.splitlines()

    def test_completed_review_releases_before_next_queued_assignment(self):
        self.assertEqual(self.run_role(),['release forge.handoff','launch'])

    def test_completed_critical_audit_releases_while_other_audits_wait(self):
        self.assertEqual(self.run_role('audit'),['release forge.human_wait','launch'])

    def test_failed_proof_keeps_worker_and_never_admits_replacement(self):
        self.assertEqual(self.run_role(released=False),['release forge.handoff','release forge.execution_retirement','watch'])

    def test_busy_worker_is_preserved_and_legacy_requires_verified_handoff(self):
        self.assertEqual(self.run_role(busy=True),['watch'])
        self.assertEqual(self.run_role(tracked=False),['release forge.handoff','launch'])

    def test_terminal_failure_releases_slot_without_reusing_its_workspace(self):
        self.assertEqual(self.run_role(released=False,terminal=True),['release forge.handoff','release forge.execution_retirement'])

    def test_provider_fallback_preserves_work_without_quarantining_it(self):
        self.assertEqual(self.run_role(released=False,terminal=True,handover=True),['handover','launch'])

    def test_completed_published_result_reconciles_then_launches(self):
        self.assertEqual(self.run_role(released=False,terminal=True,reconciled=True),
            ['release forge.handoff','release forge.execution_retirement','launch'])
        self.assertEqual(self.reconcile_calls,['reconcile'])
        self.assertFalse(self.quarantine_retained)
        self.assertEqual(self.run_role(initial_quarantine=True,reconciled=True),['launch'])
        self.assertEqual(self.reconcile_calls,['reconcile'])

    def test_refused_reconciliation_retains_local_quarantine(self):
        self.assertEqual(self.run_role(initial_quarantine=True),[])
        self.assertEqual(self.reconcile_calls,['reconcile'])
        self.assertTrue(self.quarantine_retained)
        self.assertIn('workspace reconciliation deferred',self.logged)

    def test_builder_never_automatically_reconciles(self):
        self.assertEqual(self.run_role('build',initial_quarantine=True,reconciled=True),[])
        self.assertEqual(self.reconcile_calls,[])
        self.assertTrue(self.quarantine_retained)

    def test_busy_worker_does_not_enter_result_reconciliation(self):
        self.assertEqual(self.run_role(busy=True,reconciled=True),['watch'])
        self.assertEqual(self.reconcile_calls,[])
        self.assertFalse(self.quarantine_retained)

    def test_codex_shell_state_comes_from_receipt_not_claude_classifier(self):
        source=(Path(__file__).resolve().parents[1]/'legacy/supervisor.sh').read_text()
        function=source[source.index('provider_state(){'):source.index('release_worker(){')]
        for state in ('idle','busy','blocked','unknown'):
            script='DIR=/unused\nSTATE=/unused\ntmux(){ echo bash; }\nsession_generation_full(){ echo \'$1|100|%1|10\'; }\npython3(){ if [ "$3" != codex-observe ]; then echo wrong_classifier >&2; return 2; fi; echo "$EXPECTED_STATE"; }\n'+function+'\nprovider_state fixture-build\n'
            result=subprocess.run(['/bin/bash','-eu','-c',script],env={**os.environ,'EXPECTED_STATE':state},capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),state)


if __name__=='__main__':unittest.main()
