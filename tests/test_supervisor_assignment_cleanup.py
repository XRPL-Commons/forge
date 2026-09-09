"""One-assignment workers release before unrelated queued work is admitted."""
from pathlib import Path
import os
import subprocess
import unittest
from tests.host_isolation import deny_host_tmux


class AssignmentCleanupTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)

    def run_role(self, role='review', *, tracked=True, busy=False, released=True, terminal=False, handover=False):
        source=(Path(__file__).resolve().parents[1]/'legacy/supervisor.sh').read_text()
        function=source[source.index('role_reconcile(){'):source.index('\nglog "=== supervisor started')]
        retirement=source[source.index('retire_finished_worker(){'):source.index('launch(){')]
        script='''occupied=1
quarantined=0
DIR=/unused
STATE=/unused
workflow_enabled(){ return 0; }
sess_up(){ [ "$occupied" = 1 ]; }
maybe_handover(){ if [ "$HANDOVER" = 1 ]; then occupied=0; echo handover; return 0; fi; return 1; }
actual_provider(){ echo codex; }
sess_client(){ return 0; }
workflow_call(){ [ "$TRACKED" = 1 ]; }
provider_state(){ if [ "$BUSY" = 1 ]; then echo busy; else echo idle; fi; }
release_worker(){ echo "release $1"; if [ "$1" = forge.execution_retirement ] && [ "$TERMINAL" = 1 ]; then occupied=0; quarantined=1; return 0; fi; if [ "$RELEASED" = 1 ]; then occupied=0; return 0; fi; return 1; }
workspace_available(){ [ "$quarantined" = 0 ]; }
glog(){ :; }
health(){ return 0; }
python3(){ return 1; }
watch(){ echo watch; }
launch(){ echo launch; }
declare -A human_waiting
'''+retirement+function+'\nrole_reconcile "fixture-$ROLE" /unused role.md task 2 fixture Org/Fixture\n'
        env={**os.environ,'ROLE':role,'TRACKED':str(int(tracked)),'BUSY':str(int(busy)),'RELEASED':str(int(released)),'TERMINAL':str(int(terminal)),'HANDOVER':str(int(handover))}
        result=subprocess.run(['/bin/bash','-eu','-c',script],env=env,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
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

    def test_codex_shell_state_comes_from_receipt_not_claude_classifier(self):
        source=(Path(__file__).resolve().parents[1]/'legacy/supervisor.sh').read_text()
        function=source[source.index('provider_state(){'):source.index('release_worker(){')]
        for state in ('idle','busy','blocked','unknown'):
            script='DIR=/unused\nSTATE=/unused\ntmux(){ echo bash; }\nsession_generation_full(){ echo \'$1|100|%1|10\'; }\npython3(){ if [ "$3" != codex-observe ]; then echo wrong_classifier >&2; return 2; fi; echo "$EXPECTED_STATE"; }\n'+function+'\nprovider_state fixture-build\n'
            result=subprocess.run(['/bin/bash','-eu','-c',script],env={**os.environ,'EXPECTED_STATE':state},capture_output=True,text=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),state)


if __name__=='__main__':unittest.main()
