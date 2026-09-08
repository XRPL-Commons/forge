import json
from pathlib import Path
import tempfile
import unittest
from observer import judge, report, run
from observer.config import Repo
from observer.tests.test_run import FakeGit, C, deps_with


class CoverageTests(unittest.TestCase):
    def test_no_truncation_late_finding_reaches_judge(self):
        diff = 'diff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -0,0 +1,2 @@\n+' + 'x' * 15000 + '\n+dangerous_tail'
        def runner(cmd):
            self.assertIn('dangerous_tail', cmd[2])
            self.assertIn('--tools', cmd)
            self.assertEqual(cmd[cmd.index('--tools') + 1], '')
            return '{"level":"malicious","reason":"late finding","findings":[{"file":"a","line":2,"side":"new","reason":"danger","evidence":"dangerous_tail"}]}'
        verdict = judge.judge('X/y', 'a', 's', [], diff, '/claude', runner)
        self.assertEqual(verdict.level, 'malicious')
        self.assertTrue(verdict.complete)

    def test_oversize_and_binary_are_incomplete_without_calling_model(self):
        for diff in ('x' * 100001, 'GIT binary patch\nliteral 10'):
            verdict = judge.judge('X/y', 'a', 's', [], diff, '/claude', lambda cmd: self.fail('model called'))
            self.assertEqual(verdict.level, 'unknown')
            self.assertFalse(verdict.complete)

    def test_unknown_does_not_advance_checkpoint(self):
        git = FakeGit('NEW', commits=[C('NEW')]); git._pre = 'OLD'
        deps, _ = deps_with(git, 'unknown')
        with tempfile.TemporaryDirectory() as state:
            result = run.scan_repo(Repo('r', 'X/y', '/build', True, '/mirror'), state, '/claude', '/notify', deps=deps)
        self.assertEqual(result['status'], 'scan_error')
        self.assertEqual(result['scanned'], 0)
        self.assertEqual(git.written.get('r', 'OLD'), 'OLD')

    def test_missing_trusted_mirror_is_blocked_before_fetch(self):
        with tempfile.TemporaryDirectory() as state:
            result = run.scan_repo(Repo('r', 'X/y', '/build', True), state, None, '/notify')
        self.assertEqual(result['status'], 'mirror_required')

    def test_findings_exist_before_failed_enqueue(self):
        with tempfile.TemporaryDirectory() as state:
            def runner(cmd):
                record = json.loads(Path(state, 'findings.jsonl').read_text())
                self.assertEqual(record['level'], 'unknown')
                return False
            with self.assertRaisesRegex(RuntimeError, 'enqueue failed'):
                report.report('X/y', 'sha', 'a', 's', judge.Verdict('unknown', 'unavailable', 'incomplete', False), state, '/notify', runner)

    def test_repeated_unknown_reports_enqueue_once(self):
        calls = []
        with tempfile.TemporaryDirectory() as state:
            for _ in range(3):
                report.report('X/y', 'sha', 'a', 's', judge.Verdict('unknown', 'unavailable', 'incomplete', False), state, '/notify', lambda cmd: calls.append(cmd) or True)
        self.assertEqual(len(calls), 1)

    def test_malicious_missing_or_forged_line_evidence_is_unknown(self):
        patch = '--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+harmless\n'
        answers = [
            {'level': 'malicious', 'reason': 'bad', 'findings': []},
            {'level': 'malicious', 'reason': 'bad', 'findings': [{'file': 'a', 'line': 2, 'side': 'new', 'reason': 'bad', 'evidence': 'harmless'}]},
            {'level': 'malicious', 'reason': 'bad', 'findings': [{'file': 'a', 'line': 1, 'side': 'new', 'reason': 'bad', 'evidence': 'invented'}]},
        ]
        for answer in answers:
            verdict = judge.judge('X/y', 'sha', 's', [], patch, '/claude', lambda cmd: json.dumps(answer))
            self.assertEqual(verdict.level, 'unknown')
            self.assertFalse(verdict.complete)

    def test_header_looking_source_line_keeps_real_anchor(self):
        patch = '--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+++ token\n'
        locations = judge.diff_locations(patch)
        self.assertEqual(locations[('a', 1, 'new')], '++ token')

    def test_judge_restricted_flags_and_trusted_working_directory(self):
        from unittest.mock import patch
        from types import SimpleNamespace
        def subprocess_runner(cmd, **kwargs):
            self.assertIn('--safe-mode', cmd)
            self.assertIn('--restricted', cmd)
            self.assertEqual(Path(kwargs['cwd']).resolve(), Path(judge.__file__).resolve().parent)
            return SimpleNamespace(returncode=0, stdout='{"level":"benign","reason":"safe","findings":[]}', stderr='')
        with patch('observer.judge.subprocess.run', side_effect=subprocess_runner):
            verdict = judge.judge('X/y', 'sha', 's', [], '', '/claude')
        self.assertTrue(verdict.complete)
