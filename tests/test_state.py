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

    def test_snapshot_does_not_rewrite_state_or_expose_mutable_shared_data(self):
        self.increment()
        before = self.store.path.stat()
        snapshot = self.store.read()
        snapshot['count'] = 999
        self.assertEqual(self.store.read(), {'count': 1})
        after = self.store.path.stat()
        self.assertEqual((before.st_ino, before.st_mtime_ns), (after.st_ino, after.st_mtime_ns))

    def test_snapshot_waits_for_committed_writes_and_never_rewrites_them(self):
        from threading import Event
        entered, release = Event(), Event()
        self.increment()
        def writer():
            with self.store.transaction() as data:
                data['count'] = 2
                entered.set()
                self.assertTrue(release.wait(5))
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            writing = pool.submit(writer)
            self.assertTrue(entered.wait(5))
            reading = pool.submit(self.store.read)
            self.assertFalse(reading.done())
            release.set(); writing.result()
            self.assertEqual(reading.result(), {'count': 2})

    def test_snapshot_fails_closed_for_lost_corrupt_and_symlinked_state(self):
        self.increment()
        self.store.path.write_text('invalid')
        with self.assertRaises(StateError): self.store.read()
        self.store.path.unlink()
        with self.assertRaises(StateError): self.store.read()
        target = self.store.root / 'elsewhere.json'; target.write_text('{}')
        self.store.path.symlink_to(target)
        with self.assertRaises(StateError): self.store.read()

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
