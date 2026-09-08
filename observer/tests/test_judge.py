import unittest
from observer.heuristics import Flag
from observer.judge import build_prompt, parse_verdict, judge, Verdict

class TestJudge(unittest.TestCase):
    def test_prompt_preserves_full_diff(self):
        p = build_prompt("X/y", "deadbeef", "add setup",
                         [Flag("cron_write", "* * * * *")], "D" * 20000)
        self.assertIn("cron_write", p)
        self.assertIn("deadbeef", p)
        self.assertIn("D" * 20000, p)

    def test_parse_valid_json(self):
        v = parse_verdict('{"level":"malicious","reason":"cron miner","findings":[{"file":"a","line":1,"side":"new","reason":"miner","evidence":"xmrig"}]}')
        self.assertEqual(v.level, "malicious")
        self.assertEqual(v.reason, "cron miner")
        self.assertEqual(v.confidence, "llm")

    def test_parse_json_embedded_in_text(self):
        v = parse_verdict('Sure!\n{"level":"benign","reason":"typo fix","findings":[]}\ndone')
        self.assertEqual(v.level, "benign")

    def test_parse_rejects_bad_level(self):
        with self.assertRaises(ValueError):
            parse_verdict('{"level":"evil","reason":"x"}')

    def test_judge_fallback_strong_flag_suspicious(self):
        v = judge("X/y", "abc", "s", [Flag("cron_write", "x")], "--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+x\n",
                  claude_bin=None)
        self.assertEqual(v.level, "suspicious")
        self.assertEqual(v.confidence, "heuristic-only")

    def test_judge_fallback_weak_flag_is_unknown(self):
        v = judge("X/y", "abc", "s", [Flag("base64_blob", "AAAA")], "diff",
                  claude_bin=None)
        self.assertEqual(v.level, "unknown")
        self.assertFalse(v.complete)
        self.assertEqual(v.confidence, "heuristic-only")

    def test_judge_uses_runner_output(self):
        def fake_runner(cmd):
            return '{"level":"benign","reason":"safe","findings":[]}'
        v = judge("X/y", "abc", "s", [], "diff",
                  claude_bin="/bin/claude", runner=fake_runner)
        self.assertEqual(v.level, "benign")
        self.assertEqual(v.confidence, "llm")

    def test_judge_falls_back_on_runner_error(self):
        def bad_runner(cmd):
            raise RuntimeError("claude exploded")
        v = judge("X/y", "abc", "s", [Flag("miner_string", "xmrig")], "--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+xmrig\n",
                  claude_bin="/bin/claude", runner=bad_runner)
        self.assertEqual(v.level, "suspicious")
        self.assertEqual(v.confidence, "heuristic-only")

    def test_strong_flag_floors_benign_llm(self):
        def benign_runner(cmd): return '{"level":"benign","reason":"looks fine","findings":[]}'
        v = judge("X/y", "abc", "s", [Flag("pipe_to_shell", "curl|sh")], "--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+curl|sh\n",
                  claude_bin="/bin/claude", runner=benign_runner)
        self.assertEqual(v.level, "suspicious")

    def test_weak_flag_does_not_floor_benign_llm(self):
        def benign_runner(cmd): return '{"level":"benign","reason":"ok","findings":[]}'
        v = judge("X/y", "abc", "s", [Flag("base64_blob", "AAAA")], "diff",
                  claude_bin="/bin/claude", runner=benign_runner)
        self.assertEqual(v.level, "benign")

    def test_strong_floor_keeps_malicious(self):
        def mal_runner(cmd): return '{"level":"malicious","reason":"bad","findings":[{"file":"a","line":1,"side":"new","reason":"miner","evidence":"xmrig"}]}'
        v = judge("X/y", "abc", "s", [Flag("pipe_to_shell", "x")], "--- a/a\n+++ b/a\n@@ -0,0 +1 @@\n+xmrig\n",
                  claude_bin="/bin/claude", runner=mal_runner)
        self.assertEqual(v.level, "malicious")



class WorkflowChangeStrongTest(unittest.TestCase):
    def test_ci_workflow_change_is_strong(self):
        from observer.judge import STRONG_FLAGS
        self.assertIn("ci_workflow_change", STRONG_FLAGS)


if __name__ == "__main__":
    unittest.main()