import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.state import AtomicStore
from forge.view_cache import ViewCache
from forge.factory.issues import IssueInventory


class ViewCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'state/factory').mkdir(parents=True)
        self.ledger = self.root / 'state/factory/factory-ledger.json'
        self.ledger.write_text('{}')
        self.now = 0.0
        self.cache = ViewCache(clock=lambda: self.now, max_age=10)
        self.builds = 0

    def build(self):
        self.builds += 1
        return {'build': self.builds}

    def test_reuses_a_view_until_state_changes_or_it_ages(self):
        self.assertEqual(self.cache.get(self.root, 'factory', self.build), {'build': 1})
        self.now = 5
        self.assertEqual(self.cache.get(self.root, 'factory', self.build), {'build': 1})
        self.ledger.write_text('{"changed": true}')
        os.utime(self.ledger, ns=(1, 1))
        self.assertEqual(self.cache.get(self.root, 'factory', self.build), {'build': 2})
        self.now = 16
        self.assertEqual(self.cache.get(self.root, 'factory', self.build), {'build': 3})

    def test_keys_are_cached_separately(self):
        self.cache.get(self.root, ('issues', 1), self.build)
        self.cache.get(self.root, ('issues', 2), self.build)
        self.cache.get(self.root, ('issues', 1), self.build)
        self.assertEqual(self.builds, 2)


class ReadOnlyViewTests(unittest.TestCase):
    def test_action_statuses_never_rewrite_the_store(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with AtomicStore(root / 'state/issues', 'actions').transaction() as data:
                data['a'] = {'key': '1:1', 'status': 'pending', 'created_at': 1}
            path = root / 'state/issues/actions.json'
            before = path.stat().st_mtime_ns
            inventory = IssueInventory.__new__(IssueInventory)
            inventory.root = root
            self.assertEqual(inventory.action_statuses()[0]['status'], 'pending')
            self.assertEqual(path.stat().st_mtime_ns, before)

    def test_refresh_threads_are_spaced_unless_forced(self):
        inventory = IssueInventory.__new__(IssueInventory)
        with patch('forge.factory.issues.threading.Thread') as thread, \
                patch.object(IssueInventory, '_refresh_started', 0.0), \
                patch('forge.factory.issues.time.monotonic', side_effect=[1000.0, 1001.0, 1002.0]):
            inventory.start_refresh(); inventory.start_refresh(); inventory.start_refresh(force=True)
        self.assertEqual(thread.call_count, 2)


if __name__ == '__main__':
    unittest.main()
