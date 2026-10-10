"""GET authorization has one snapshot, released before cache reads and waits."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from forge.operator_auth import OperatorAuth, FLOW_COOKIE, SESSION_COOKIE, read_cookie
from forge.sqlite_identity import SqliteOperatorAuth
from forge.store.db import Store
from forge.store.runtime import selected
from forge.view_cache import ViewCache
from tests.test_dashboard import dashboard
from tests.test_sqlite_identity import selected_store


class OperatorReadSnapshotTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        selected_store(self.root)
        self.store = selected(self.root)
        self.auth = OperatorAuth(self.root, clock=lambda: 1000,
                                 identity=lambda *args: {'id': 123, 'login': 'operator'})
        self.auth.configure('https://forge.example', 'client-id', 's' * 40, [123])
        url, flow = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.token = read_cookie(self.auth.callback(state, 'code', read_cookie(flow, FLOW_COOKIE)), SESSION_COOKIE)
        self.writer = Store.open(self.store.root)
        self.writer_auth = SqliteOperatorAuth(self.root, store=self.writer, clock=lambda: 1000)
        for patcher in (patch.object(dashboard, 'WD', self.root),
                        patch('forge.operator_auth.OperatorAuth', return_value=self.auth),
                        patch.object(dashboard, 'VIEWS', ViewCache(clock=lambda: 1))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def handler(self, path='/api/factory?section=board', tag=None):
        handler = object.__new__(dashboard.H)
        handler.path, handler.command = path, 'GET'
        handler.headers = {'Cookie': f'{SESSION_COOKIE}={self.token}'}
        if tag:
            handler.headers['If-None-Match'] = tag
        handler._trusted_host = Mock(return_value=True)
        handler._upstream = Mock(return_value=True)
        handler._public_host = Mock(return_value='forge.example')
        handler._auth_get = Mock(return_value=False)
        handler.send_error = Mock()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        return handler

    def test_configuration_session_and_pause_share_one_snapshot_then_refresh(self):
        config = self.auth.config
        def revoke_after_config():
            value = config()
            self.writer_auth.logout(self.token)
            with self.writer.write() as db:
                db.execute("INSERT INTO runtime_controls(name,enabled,updated_at) VALUES('factory_paused',1,1000)")
            return value
        first = self.handler()
        with patch.object(self.auth, 'config', side_effect=revoke_after_config), \
                patch.object(self.store, '_connect_path', wraps=self.store._connect_path) as connect:
            self.assertTrue(first._operator_guard(read_snapshot=True))
            self.assertTrue(first._signed_in())
            self.assertFalse(first._factory_paused)
            self.assertEqual(connect.call_count, 1)
        self.assertIsNone(self.store._reader.get())
        fresh = self.handler()
        self.assertTrue(fresh._operator_guard(read_snapshot=True))
        self.assertFalse(fresh._signed_in())
        self.assertTrue(fresh._factory_paused)
        fresh.send_error.assert_called_once_with(401)

    def test_cache_fingerprint_and_projection_follow_the_released_auth_snapshot(self):
        with self.writer.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('projection_test','before')")
        def commit_after_guard(_url):
            self.assertIsNone(self.store._reader.get())
            with self.writer.write() as db:
                db.execute("UPDATE store_metadata SET value='after' WHERE key='projection_test'")
            return False
        def build(*args, **kwargs):
            self.assertIsNone(self.store._reader.get())
            with self.store.read_session() as db:
                return {'value': db.execute("SELECT value FROM store_metadata WHERE key='projection_test'").fetchone()[0]}
        # Keep the WAL file identity stable across the requests under test.
        with self.writer._connection(), \
                patch('forge.factory.control.factory_view', side_effect=build) as projection, \
                patch.object(self.store, '_connect_path', wraps=self.store._connect_path) as connect:
            first = self.handler()
            first._auth_get.side_effect = commit_after_guard
            first.do_GET()
            first.send_error.assert_not_called()
            self.assertEqual(json.loads(first.wfile.getvalue())['value'], 'after')
            self.assertEqual(connect.call_count, 1)  # Separate snapshots, one physical connection.
            tag = next(c.args[1] for c in first.send_header.call_args_list if c.args[0] == 'ETag')
            connect.reset_mock()
            warm = self.handler(); warm.do_GET()
            warm.send_response.assert_called_once_with(200)
            self.assertEqual(connect.call_count, 1)
            connect.reset_mock()
            unchanged = self.handler(tag=tag); unchanged.do_GET()
            unchanged.send_response.assert_called_once_with(304)
            self.assertEqual(connect.call_count, 1)
            projection.assert_called_once()

    def test_wait_holds_no_auth_snapshot_and_rechecks_revocation(self):
        def wait(**kwargs):
            self.assertIsNone(self.store._reader.get())
            self.assertIsNone(self.store._read_lease.get())
            self.writer_auth.logout(self.token)
            return {'board': {}, 'events': []}
        with patch('forge.factory.control.public_views', return_value=Mock()), \
                patch('forge.factory.changes.Changes.read', side_effect=wait), \
                patch.object(self.store, '_connect_path', wraps=self.store._connect_path) as connect:
            handler = self.handler('/api/factory/changes?wait=1')
            handler.do_GET()
            handler.send_error.assert_called_once_with(401)
            handler.send_response.assert_not_called()
            self.assertEqual(connect.call_count, 2)
        self.assertIsNone(self.store._reader.get())

    def test_cached_response_reauthenticates_after_revocation(self):
        with patch('forge.factory.control.factory_view', return_value={'issues': []}) as projection:
            first = self.handler(); first.do_GET()
            tag = next(c.args[1] for c in first.send_header.call_args_list if c.args[0] == 'ETag')
            self.writer_auth.logout(self.token)
            denied = self.handler(tag=tag); denied.do_GET()
            denied.send_error.assert_called_once_with(401)
            denied.send_response.assert_not_called()
            projection.assert_called_once()
        self.assertIsNone(self.store._read_lease.get())

    def test_projection_failure_and_disconnected_response_release_lease(self):
        import sqlite3
        opened=[]
        original=self.store._connect_path
        def connect(*args, **kwargs):
            result=original(*args, **kwargs); opened.append(result); return result
        for failure in ('projection', 'response'):
            with self.subTest(failure=failure), patch.object(self.store, '_connect_path', side_effect=connect), \
                    patch('forge.factory.control.factory_view', side_effect=ValueError('invalid projection') if failure=='projection' else None, return_value={'issues': []}):
                handler=self.handler()
                if failure=='response':
                    handler.wfile=Mock();handler.wfile.write.side_effect=BrokenPipeError('disconnected')
                    with self.assertRaises(BrokenPipeError):handler.do_GET()
                else:handler.do_GET()
                self.assertIsNone(self.store._read_lease.get())
                with self.assertRaises(sqlite3.ProgrammingError):opened[-1].execute('SELECT 1')
