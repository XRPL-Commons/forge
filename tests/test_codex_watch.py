"""Exercise the real shell watch function without any host commands or workers."""
from pathlib import Path
import subprocess
import unittest


class CodexWatchTests(unittest.TestCase):
    def watch(self, *, state='idle', release=False, session='sample-build'):
        source=(Path(__file__).parents[1]/'legacy/supervisor.sh').read_text()
        function=source[source.index('watch_codex(){'):source.index('\nprovider_state(){')]
        setup=r'''set -u
STATE=/fake; DIR=/fake; FORGE_CODEX_BIN=/fake/codex
session_generation_full(){ echo '$1|100|%1|10'; }
route_success(){ :; }
route_failure(){ :; }
health(){ :; }
glog(){ echo "log:$*"; }
send_codex_command(){ echo sent; }
release_worker(){ echo cleanup; return RELEASE; }
python3(){
  case "$*" in
    *"codex-observe"*) echo STATE_VALUE ;;
    *"codex-command"*) echo '{"reason":"resume_requires_changed_work"}'; return 1 ;;
    *"forge.activity"*) echo "activity:$*" >&2 ;;
    *"forge.lifecycle"*) echo "lifecycle:$*" >&2 ;;
    *"-c"*) echo '{"reason":"resume_requires_changed_work","phase":"codex_resume"}' ;;
    *) return 99 ;;
  esac
}
'''.replace('STATE_VALUE',state).replace('return RELEASE', 'return '+('0' if release else '1'))
        result=subprocess.run(['/bin/bash','-c',setup+function+'\nwatch_codex '+session+' role prompt'],capture_output=True,text=True,timeout=5,env={'PATH':'/nonexistent'})
        self.assertEqual(result.returncode,0,result.stderr)
        return result

    def test_completed_builder_released_before_any_resume(self):
        result=self.watch(release=True)
        self.assertIn('cleanup',result.stdout)
        self.assertIn('released completed builder',result.stdout)
        self.assertNotIn('deferred',result.stdout)
        self.assertNotIn('sent',result.stdout)

    def test_busy_execution_is_preserved(self):
        result=self.watch(state='busy',release=True)
        self.assertNotIn('cleanup',result.stdout)
        self.assertNotIn('sent',result.stdout)

    def test_retained_builder_records_resume_without_replacing_cleanup(self):
        result=self.watch()
        self.assertIn('cleanup',result.stdout)
        self.assertIn('activity:',result.stderr)
        self.assertNotIn('lifecycle:',result.stderr)
        self.assertIn('resume_requires_changed_work',result.stdout)
        self.assertNotIn('sent',result.stdout)

    def test_reviewer_resume_uses_its_own_durable_decision(self):
        result=self.watch(session='sample-review')
        self.assertNotIn('cleanup',result.stdout)
        self.assertIn('lifecycle:',result.stderr)
        self.assertIn('codex_resume',result.stderr)
