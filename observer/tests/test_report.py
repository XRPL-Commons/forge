import json, os, tempfile, unittest
from observer.judge import Verdict
from observer import report

class TestReport(unittest.TestCase):
    def test_format_alert_has_url_and_level(self):
        v = Verdict("malicious", "cron miner", "llm")
        a = report.format_alert("example-org/academy", "abcd1234",
                                "mallory", "add setup", v)
        self.assertIn("MALICIOUS", a)
        self.assertIn("example-org/academy", a)
        self.assertIn("github.com/example-org/academy/commit/abcd1234", a)
        self.assertIn("cron miner", a)

    def test_append_finding_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "findings.jsonl")
            report.append_finding({"sha": "x", "level": "suspicious"}, p)
            report.append_finding({"sha": "y", "level": "malicious"}, p)
            lines = open(p).read().splitlines()
            self.assertEqual(len(lines), 2)
            self.assertEqual(json.loads(lines[1])["sha"], "y")

    def test_report_benign_is_silent(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            report.report("X/y", "s", "a", "subj",
                          Verdict("benign", "ok", "llm"), d, "/notify.sh",
                          runner=lambda cmd: calls.append(cmd) or True)
        self.assertEqual(calls, [])
        self.assertFalse(os.path.exists(os.path.join(d, "findings.jsonl")))

    def test_report_suspicious_notifies_and_logs(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            report.report("X/y", "sha1", "a", "subj",
                          Verdict("suspicious", "why", "heuristic-only"),
                          d, "/notify.sh",
                          runner=lambda cmd: calls.append(cmd) or True)
            self.assertEqual(len(calls), 1)
            self.assertTrue(os.path.exists(os.path.join(d, "findings.jsonl")))

if __name__ == "__main__":
    unittest.main()
