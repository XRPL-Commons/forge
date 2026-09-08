import os, tempfile, unittest
from observer.config import Repo
from observer.judge import Verdict
from observer import run

class FakeGit:
    def __init__(self, head, old_is_ancestor=True, commits=None, diffs=None):
        self._head = head
        self._anc = old_is_ancestor
        self._commits = commits or []
        self._diffs = diffs or {}
        self.written = {}
    def verify_mirror(self, clone, repository): pass
    def fetch(self, clone): pass
    def head_sha(self, clone): return self._head
    def read_sha(self, name, sd): return self.written.get(name, self._pre)
    def write_sha(self, name, sha, sd): self.written[name] = sha
    def is_ancestor(self, clone, old, new): return self._anc
    def new_commits(self, clone, old, new): return self._commits
    def commit_diff(self, clone, sha): return self._diffs.get(sha, "")
    def changed_paths(self, clone, sha): return []

class C:
    def __init__(self, sha, author="a", subject="s"):
        self.sha, self.author, self.subject = sha, author, subject

def deps_with(git, verdict_level="benign"):
    class J:
        @staticmethod
        def judge(*a, **k): return Verdict(verdict_level, "r", "llm")
    class H:
        @staticmethod
        def scan_diff(d): return []
    reported = []
    class R:
        @staticmethod
        def report(repo, sha, author, subject, verdict, sd, ns, **k):
            if verdict.level != "benign": reported.append((sha, verdict.level))
    class Coverage:
        @staticmethod
        def verify_checkpoint(*args): pass
        @staticmethod
        def record_scan(*args): pass
    return {"gitwatch": git, "judge": J, "heuristics": H, "report": R, "coverage": Coverage}, reported

class FakeGitFetchFails:
    def verify_mirror(self, clone, repository): pass
    def fetch(self, clone):
        raise RuntimeError("network unreachable")
    def head_sha(self, clone): return "HEAD1"
    def read_sha(self, name, sd): return None
    def write_sha(self, name, sha, sd): pass
    def is_ancestor(self, clone, old, new): return True
    def new_commits(self, clone, old, new): return []
    def commit_diff(self, clone, sha): return ""
    def changed_paths(self, clone, sha): return []

class TestScanRepo(unittest.TestCase):
    def setUp(self):
        self.repo = Repo("academy", "example-org/academy", "/tmp/academy", True, "/tmp/trusted-academy")

    def test_fetch_failure_alerts_once_across_repeated_runs(self):
        g = FakeGitFetchFails()
        deps, reported = deps_with(g)
        with tempfile.TemporaryDirectory() as sd:
            for _ in range(6):
                r = run.scan_repo(self.repo, sd, "/c", "/n", deps=deps)
                self.assertEqual(r["status"], "fetch_failed")
        self.assertEqual(len(reported), 1)
        self.assertEqual(reported[0][1], "suspicious")

    def test_missing_baseline_never_seeds(self):
        g = FakeGit("HEAD1"); g._pre = None
        deps, reported = deps_with(g)
        with tempfile.TemporaryDirectory() as sd:
            r = run.scan_repo(self.repo, sd, "/c", "/n", deps=deps)
        self.assertEqual(r["status"], "baseline_required")
        self.assertEqual(g.written, {})
        self.assertEqual(reported, [])

    def test_force_push_reports_and_reseeds(self):
        g = FakeGit("NEW", old_is_ancestor=False); g._pre = "OLD"
        deps, reported = deps_with(g)
        with tempfile.TemporaryDirectory() as sd:
            r = run.scan_repo(self.repo, sd, "/c", "/n", deps=deps)
        self.assertEqual(r["status"], "force_push")
        self.assertEqual(reported, [("NEW", "malicious")])
        self.assertEqual(g.written, {})

    def test_scans_new_commits_and_advances(self):
        g = FakeGit("NEW", commits=[C("c1"), C("NEW")]); g._pre = "OLD"
        deps, reported = deps_with(g, verdict_level="suspicious")
        with tempfile.TemporaryDirectory() as sd:
            r = run.scan_repo(self.repo, sd, "/c", "/n", deps=deps)
        self.assertEqual(r["status"], "scanned")
        self.assertEqual(r["scanned"], 2)
        self.assertEqual(len(reported), 2)
        self.assertEqual(g.written["academy"], "NEW")

    def test_up_to_date_noop(self):
        g = FakeGit("SAME"); g._pre = "SAME"
        deps, reported = deps_with(g)
        with tempfile.TemporaryDirectory() as sd:
            r = run.scan_repo(self.repo, sd, "/c", "/n", deps=deps)
        self.assertEqual(r["status"], "up_to_date")

    def test_force_push_scans_rewritten_commits(self):
        g = FakeGit("NEW", old_is_ancestor=False, commits=[C("evil1")]); g._pre = "OLD"
        deps, reported = deps_with(g, verdict_level="malicious")
        with tempfile.TemporaryDirectory() as sd:
            r = run.scan_repo(self.repo, sd, "/c", "/n", deps=deps)
        self.assertEqual(r["status"], "force_push")
        # both the force-push alert (sha NEW) AND the rewritten commit evil1 are reported
        self.assertIn(("NEW", "malicious"), reported)
        self.assertIn(("evil1", "malicious"), reported)
        self.assertEqual(g.written, {})

if __name__ == "__main__":
    unittest.main()
