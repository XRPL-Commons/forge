import os, subprocess, tempfile, unittest
from observer import gitwatch

def run(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

class TestGitwatch(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        run(self.d, "git", "init", "-q", "-b", "main")
        run(self.d, "git", "config", "user.email", "t@t")
        run(self.d, "git", "config", "user.name", "t")
        with open(os.path.join(self.d, "a.txt"), "w") as f:
            f.write("one\n")
        run(self.d, "git", "add", "a.txt")
        run(self.d, "git", "commit", "-q", "-m", "first")
        self.first = subprocess.check_output(
            ["git", "-C", self.d, "rev-parse", "HEAD"]).decode().strip()

    def _second(self, content="two\n"):
        with open(os.path.join(self.d, "a.txt"), "w") as f:
            f.write(content)
        run(self.d, "git", "add", "a.txt")
        run(self.d, "git", "commit", "-q", "-m", "second")
        return subprocess.check_output(
            ["git", "-C", self.d, "rev-parse", "HEAD"]).decode().strip()

    def test_merge_diff_includes_branch_changes_against_first_parent(self):
        run(self.d, "git", "checkout", "-q", "-b", "feature")
        with open(os.path.join(self.d, "feature.txt"), "w") as f:
            f.write("dangerous_tail\n")
        run(self.d, "git", "add", "feature.txt")
        run(self.d, "git", "commit", "-q", "-m", "feature")
        run(self.d, "git", "checkout", "-q", "main")
        self._second()
        run(self.d, "git", "merge", "--no-ff", "-q", "feature", "-m", "merge")
        sha = subprocess.check_output(["git", "-C", self.d, "rev-parse", "HEAD"]).decode().strip()
        diff = gitwatch.commit_diff(self.d, sha)
        self.assertIn("+dangerous_tail", diff)
        self.assertNotIn("+two", diff)
        self.assertIn("feature.txt", gitwatch.changed_paths(self.d, sha))

    def test_failed_atomic_replace_keeps_previous_checkpoint(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as state:
            gitwatch.write_sha("r", "a" * 40, state)
            with patch("observer.gitwatch.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    gitwatch.write_sha("r", "b" * 40, state)
            self.assertEqual(gitwatch.read_sha("r", state), "a" * 40)
            self.assertEqual(os.listdir(state), ["r.sha"])

    def test_sha_roundtrip(self):
        with tempfile.TemporaryDirectory() as sd:
            self.assertIsNone(gitwatch.read_sha("r", sd))
            gitwatch.write_sha("r", "a" * 40, sd)
            self.assertEqual(gitwatch.read_sha("r", sd), "a" * 40)

    def test_new_commits_linear(self):
        second = self._second()
        commits = gitwatch.new_commits(self.d, self.first, second)
        self.assertEqual([c.sha for c in commits], [second])
        self.assertEqual(commits[0].subject, "second")

    def test_is_ancestor_true_then_false_on_rewrite(self):
        second = self._second()
        self.assertTrue(gitwatch.is_ancestor(self.d, self.first, second))
        run(self.d, "git", "reset", "--hard", "-q", self.first)
        rewritten = self._second("different\n")
        self.assertFalse(gitwatch.is_ancestor(self.d, second, rewritten))

    def test_commit_diff_contains_added_line(self):
        second = self._second()
        self.assertIn("+two", gitwatch.commit_diff(self.d, second))

if __name__ == "__main__":
    unittest.main()
