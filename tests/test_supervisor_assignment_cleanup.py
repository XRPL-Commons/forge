"""One-assignment workers release before unrelated queued work is admitted."""
from pathlib import Path
import os
import subprocess
import unittest
from tests.host_isolation import deny_host_tmux


class AssignmentCleanupTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)

    def run_role(self, role='review', *, tracked=True, busy=False, released=True):
        source=(Path(__file__).resolve().parents[1]/'legacy/supervisor.sh').read_text()
        function=source[source.index('role_reconcile(){'):source.index('\nglog "=== supervisor started')]
        script='''occupied=1
DIR=/unused
STATE=/unused
workflow_enabled(){ return 0; }
sess_up(){ [ "$occupied" = 1 ]; }
maybe_handover(){ return 1; }
actual_provider(){ echo codex; }
sess_client(){ return 0; }
workflow_call(){ [ "$TRACKED" = 1 ]; }
provider_state(){ if [ "$BUSY" = 1 ]; then echo busy; else echo idle; fi; }
release_worker(){ echo "release $1"; if [ "$RELEASED" = 1 ]; then occupied=0; return 0; fi; return 1; }
glog(){ :; }
health(){ return 0; }
python3(){ return 1; }
watch(){ echo watch; }
launch(){ echo launch; }
declare -A human_waiting
'''+function+'\nrole_reconcile "fixture-$ROLE" /unused role.md task 2 fixture Org/Fixture\n'
        env={**os.environ,'ROLE':role,'TRACKED':str(int(tracked)),'BUSY':str(int(busy)),'RELEASED':str(int(released))}
        result=subprocess.run(['/bin/bash','-eu','-c',script],env=env,capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        return result.stdout.splitlines()

    def test_completed_review_releases_before_next_queued_assignment(self):
        self.assertEqual(self.run_role(),['release forge.handoff','launch'])

    def test_completed_critical_audit_releases_while_other_audits_wait(self):
        self.assertEqual(self.run_role('audit'),['release forge.human_wait','launch'])

    def test_failed_proof_keeps_worker_and_never_admits_replacement(self):
        self.assertEqual(self.run_role(released=False),['release forge.handoff','watch'])

    def test_busy_or_untracked_worker_is_not_released_by_assignment_path(self):
        self.assertEqual(self.run_role(busy=True),['watch'])
        self.assertEqual(self.run_role(tracked=False),['watch'])


if __name__=='__main__':unittest.main()
