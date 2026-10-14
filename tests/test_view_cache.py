from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
import threading
from unittest.mock import Mock, patch
from contextlib import nullcontext

from forge.view_cache import ViewCache
from forge.factory.issues import IssueInventory
from tests.sqlite_fixture import selected_store


class ViewCacheTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = selected_store(self.root)
        self.now = 0.0
        self.cache = ViewCache(clock=lambda: self.now, max_age=10)
        self.builds = 0

    def commit(self, value):
        with self.store.write() as db:
            db.execute("INSERT OR REPLACE INTO store_metadata VALUES('view_cache_test',?)", (value,))

    def build(self):
        self.builds += 1
        return {'build': self.builds}

    def test_reuses_a_view_until_state_changes_or_it_ages(self):
        self.assertEqual(self.cache.get(self.root, 'factory', self.build), {'build': 1})
        self.now = 5
        self.assertEqual(self.cache.get(self.root, 'factory', self.build), {'build': 1})
        self.commit('changed')
        self.assertEqual(self.cache.get(self.root, 'factory', self.build), {'build': 2})
        self.now = 16
        self.assertEqual(self.cache.get(self.root, 'factory', self.build), {'build': 3})

    def test_keys_are_cached_separately(self):
        self.cache.get(self.root, ('issues', 1), self.build)
        self.cache.get(self.root, ('issues', 2), self.build)
        self.cache.get(self.root, ('issues', 1), self.build)
        self.assertEqual(self.builds, 2)

    def test_conditional_response_skips_build_even_after_eviction(self):
        tag, body = self.cache.response(self.root, ('issues', 1), self.build, 'session-a')
        self.assertIn(b'session-a', body)
        self.cache.entries.clear()
        self.assertEqual(self.cache.response(self.root, ('issues', 1), self.build, 'session-a', tag), (tag, None))
        self.assertEqual(self.builds, 1)
        for key, token in [(('issues', 2), 'session-a'), (('issues', 1), 'session-b')]:
            changed, body = self.cache.response(self.root, key, self.build, token, tag)
            self.assertNotEqual(changed, tag)
            self.assertIsNotNone(body)
        self.now = 11
        self.assertNotEqual(self.cache.response(self.root, ('issues', 1), self.build, 'session-a', tag)[0], tag)

    def test_state_changes_restart_and_runtime_roots_invalidate_validators(self):
        tag, _ = self.cache.response(self.root, 'factory', self.build, 'session')
        self.commit('changed')
        self.assertNotEqual(self.cache.response(self.root, 'factory', self.build, 'session', tag)[0], tag)
        restarted = ViewCache(clock=lambda: self.now)
        self.assertNotEqual(restarted.response(self.root, 'factory', self.build, 'session', tag)[0], tag)
        self.cache.get(self.root, 'factory', self.build)
        (self.root / 'other').mkdir(mode=0o700)
        selected_store(self.root / 'other')
        self.cache.get(self.root / 'other', 'factory', self.build)
        self.assertEqual(self.builds, 5)

    def test_concurrent_polls_share_one_build(self):
        entered, release = threading.Event(), threading.Event()
        def build():
            entered.set()
            self.assertTrue(release.wait(3))
            return self.build()
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(self.cache.get, self.root, 'factory', build) for _ in range(4)]
            self.assertTrue(entered.wait(3))
            release.set()
            self.assertEqual([f.result() for f in futures], [{'build': 1}] * 4)

    def test_nested_views_expire_with_the_response_clock_bucket(self):
        self.now = 9
        build = lambda: self.cache.get(self.root, 'factory', self.build)
        tag, _ = self.cache.response(self.root, 'board', build, 'session')
        self.now = 10
        changed, _ = self.cache.response(self.root, 'board', build, 'session', tag)
        self.assertNotEqual(changed, tag)
        self.assertEqual(self.builds, 2)


class ReadOnlyViewTests(unittest.TestCase):
    def test_refresh_initializes_its_configuration_and_github_only_when_used(self):
        settings = Mock()
        settings.locked.side_effect = lambda *args, **kwargs: nullcontext()
        settings.paused.return_value = False
        settings.view.return_value = {'repositories': []}
        with tempfile.TemporaryDirectory() as root, \
                patch('forge.factory.issues.load', return_value={'hold_labels': []}) as runtime, \
                patch('forge.factory.issues.GitHub') as github, \
                patch('forge.factory.store_inventory.StoreInventory') as store_inventory:
            selected_store(root)
            inventory = IssueInventory(root, settings=settings)
            runtime.assert_not_called()
            github.assert_not_called()
            inventory.refresh()
            inventory.refresh(force=True)
            runtime.assert_called_once_with(Path(root))
            github.assert_called_once_with(settings, runtime.return_value)
            self.assertEqual(store_inventory.return_value.refresh.call_count, 2)

    def test_refresh_threads_are_spaced_unless_forced(self):
        inventory = IssueInventory.__new__(IssueInventory)
        with patch('forge.factory.issues.threading.Thread') as thread, \
                patch.object(IssueInventory, '_refresh_started', 0.0), \
                patch('forge.factory.issues.time.monotonic', side_effect=[1000.0, 1001.0, 1002.0]):
            inventory.start_refresh(); inventory.start_refresh(); inventory.start_refresh(force=True)
        self.assertEqual(thread.call_count, 2)


if __name__ == '__main__':
    unittest.main()
