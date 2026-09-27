import concurrent.futures
import tempfile
import unittest
from pathlib import Path

from forge.state import AtomicStore, StateError


class AtomicStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AtomicStore(self.temp.name, "records")

    def increment(self):
        with self.store.transaction() as data:
            data["count"] = data.get("count", 0) + 1

    def test_concurrent_transactions_are_serialized(self):
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            list(pool.map(lambda _: self.increment(), range(32)))
        with self.store.transaction() as data:
            self.assertEqual(data["count"], 32)

    def test_lost_and_corrupt_initialized_state_fail_closed(self):
        self.increment()
        path = Path(self.temp.name) / "records.json"
        path.write_text("invalid json")
        with self.assertRaises(StateError):
            self.increment()
        path.unlink()
        with self.assertRaises(StateError):
            self.increment()

    def test_symlinked_state_is_refused(self):
        target = Path(self.temp.name) / "elsewhere.json"
        target.write_text("{}")
        (Path(self.temp.name) / "records.json").symlink_to(target)
        with self.assertRaises(StateError):
            self.increment()


if __name__ == "__main__":
    unittest.main()
