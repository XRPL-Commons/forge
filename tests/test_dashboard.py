import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from forge.control import atomic_json, snapshot_path

spec = importlib.util.spec_from_file_location("forge_dashboard", Path(__file__).parents[1] / "legacy/forge-status.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "repos.d").mkdir()
        for name, enabled in (("one", "true"), ("parked", "false")):
            (self.root / "repos.d" / (name + ".repo")).write_text(f'REPO="o/{name}"\nOWNER_LABEL="auto:owned-by-me"\nENABLED={enabled}\n')
        self.patch = patch.object(dashboard, "WD", str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_private_and_disabled_sessions_are_not_read(self):
        with patch.object(dashboard, "sh", return_value="one-build relay parked-audit") as shell:
            sessions = dashboard.tmux_sessions()
            self.assertEqual(set(sessions), {"one-build"})
            self.assertEqual(shell.call_count, 2)
        self.assertIn("one-audit", dashboard.allowed_sessions())
        self.assertNotIn("relay", dashboard.allowed_sessions())

    def test_counts_are_snapshot_only_and_stale_is_unknown(self):
        path = snapshot_path(self.root / "state", "o/one")
        atomic_json(path, {"status": "ok", "observed_at": time.time(), "counts": {"audit": 2}})
        with patch.object(dashboard, "sh", side_effect=AssertionError("No GitHub calls")):
            self.assertEqual(dashboard.gh_counts("o/one"), {"audit": 2})
        atomic_json(path, {"status": "ok", "observed_at": 1, "counts": {"audit": 0}})
        self.assertEqual(dashboard.gh_counts("o/one")["audit"], "?")

    def test_focus_uses_same_snapshot_pr_metadata_before_history_collection(self):
        rows = [{'repo': 'o/one', 'queue_status': 'ok', 'observed_at': 123,
                 'build': {'up': False}, 'review': {'up': True}, 'audit': {'up': False},
                 'work': {'review': [22]}, 'schedule': {},
                 'item_details': {'22': {'number': 22, 'kind': 'pr', 'title': 'New PR',
                                         'body': 'Closes #16', 'updated_at': '2026-09-08T00:00:00Z'}}}]
        with patch.object(dashboard, 'sh', side_effect=AssertionError('No external reads')):
            dashboard.current_work_context(rows)
        focus = rows[0]['focus']['review']
        self.assertEqual(focus['items'][0]['title'], 'New PR')
        self.assertEqual(focus['items'][0]['metadata_observed_at'], 123)
        self.assertEqual(focus['basis'], 'queued')


class DashboardHistoryTests(unittest.TestCase):
    def test_repository_details_are_local_only(self):
        with patch.object(dashboard, 'read_repository', return_value={'items': []}) as read, \
             patch.object(dashboard, 'sh', side_effect=AssertionError('No command execution')):
            self.assertEqual(dashboard.repository_detail('o/one'), {'items': []})
            read.assert_called_once_with(Path(dashboard.WD) / 'state', 'o/one')

    def request(self, path):
        handler = object.__new__(dashboard.H)
        handler.path = path
        handler._send = unittest.mock.Mock()
        handler.send_error = unittest.mock.Mock()
        handler.do_GET()
        return handler

    def test_history_filters_to_enabled_repositories(self):
        with patch.object(dashboard, 'repos', return_value=['o/one']), \
             patch.object(dashboard, 'read_overview', return_value={'status': 'ok'}) as read:
            handler = self.request('/api/history?days=7')
            read.assert_called_once_with(Path(dashboard.WD) / 'state', days=7, repos=['o/one'])
            self.assertEqual(json.loads(handler._send.call_args.args[0]), {'status': 'ok'})

    def test_history_rejects_unbounded_range(self):
        with patch.object(dashboard, 'read_overview') as read:
            handler = self.request('/api/history?days=999999')
            handler.send_error.assert_called_once_with(400)
            read.assert_not_called()

    def test_repository_details_reject_disabled_or_arbitrary_repo(self):
        with patch.object(dashboard, 'repos', return_value=['o/one']), \
             patch.object(dashboard, 'repository_detail') as read:
            handler = self.request('/api/repository?repo=o/private')
            handler.send_error.assert_called_once_with(400)
            read.assert_not_called()

    def test_repository_endpoint_reads_local_data(self):
        with patch.object(dashboard, 'repos', return_value=['o/one']), \
             patch.object(dashboard, 'repository_detail', return_value={'items': []}) as read:
            handler = self.request('/api/repository?repo=o%2Fone')
            read.assert_called_once_with('o/one')
            self.assertEqual(json.loads(handler._send.call_args.args[0]), {'items': []})
