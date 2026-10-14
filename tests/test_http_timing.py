"""Authenticated GET timing describes work done, independently of HTTP status."""
import io
import re
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.view_cache import ViewCache
from pathlib import Path
from tests.test_sqlite_identity import selected_store
from forge.factory.config import load
from tests.test_dashboard import dashboard


class HTTPServerTimingTests(unittest.TestCase):
    def handler(self, path, tag=None):
        handler = object.__new__(dashboard.H)
        handler.path, handler.command = path, 'GET'
        handler.headers = {'If-None-Match': tag} if tag else {}
        handler._action_token = 'private-session-token'
        handler._factory_paused = False
        for method in ('_trusted_host', '_upstream', '_operator_guard', '_signed_in'):
            setattr(handler, method, Mock(return_value=True))
        handler._auth_get = Mock(return_value=False)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        return handler

    def header(self, handler, name):
        return next((call.args[1] for call in handler.send_header.call_args_list if call.args[0] == name), None)

    def timing(self, handler):
        header = self.header(handler, 'Server-Timing')
        self.assertIsNotNone(header)
        values = {}
        for metric in header.split(', '):
            match = re.fullmatch(r'([a-z_]+);dur=([0-9]+\.[0-9]{3})', metric)
            self.assertIsNotNone(match, header)
            values[match[1]] = float(match[2])
        self.assertIn('request', values)
        self.assertNotIn('private-session-token', header)
        self.assertNotIn('1:12', header)
        return values

    def test_actual_board_and_history_handlers_distinguish_build_cache_hit_and_validator(self):
        for path in ('/api/factory?section=board', '/api/factory?section=completed&limit=20',
                     '/api/delivery?issue=1%3A12'):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as root, \
                    patch.object(dashboard, 'WD', root), \
                    patch.object(dashboard, 'VIEWS', ViewCache(clock=lambda: 1)), \
                    patch('forge.factory.control.factory_view', return_value={'issues': []}) as factory, \
                    patch('forge.factory.control.public_views') as views:
                selected_store(Path(root))
                views.return_value.delivery.return_value = {'jobs': []}
                build = views.return_value.delivery if path.startswith('/api/delivery') else factory
                first = self.handler(path); first.do_GET()
                first.send_response.assert_called_once_with(200)
                measured = self.timing(first)
                self.assertEqual(set(measured), {'cache_miss', 'projection', 'request'})
                self.assertGreaterEqual(measured['cache_miss'], measured['projection'])
                self.assertGreaterEqual(measured['request'], measured['cache_miss'])
                tag = self.header(first, 'ETag')
                warm = self.handler(path); warm.do_GET()
                warm.send_response.assert_called_once_with(200)
                self.assertEqual(set(self.timing(warm)), {'cache_hit', 'request'})
                self.assertEqual(warm.wfile.getvalue(), first.wfile.getvalue())
                # A validator can succeed without retaining any serialized body.
                dashboard.VIEWS.entries.clear()
                unchanged = self.handler(path, tag); unchanged.do_GET()
                unchanged.send_response.assert_called_once_with(304)
                self.assertEqual(set(self.timing(unchanged)), {'cache_validated', 'conditional', 'request'})
                self.assertEqual(unchanged.wfile.getvalue(), b'')
                build.assert_called_once()
                # An ordinary cache miss after eviction does not imply process start.
                rebuilt = self.handler(path); rebuilt.do_GET()
                self.assertEqual(set(self.timing(rebuilt)), {'cache_miss', 'projection', 'request'})
                self.assertEqual(build.call_count, 2)
                # Even a matching validator still goes through authorization.
                denied = self.handler(path, tag); denied._signed_in.return_value = False; denied.do_GET()
                denied.send_response.assert_not_called()
                self.assertIsNone(self.header(denied, 'Server-Timing'))
                self.assertEqual(build.call_count, 2)

    def test_conditional_response_keeps_projection_evidence_when_body_was_built(self):
        handler = self.handler('/api/factory?section=board', 'same')
        handler._request_started = dashboard.perf_counter()
        handler._send(b'{}', etag='same', timings={'cache_miss': 0.0, 'projection': 0.0})
        handler.send_response.assert_called_once_with(304)
        self.assertEqual(set(self.timing(handler)), {'cache_miss', 'projection', 'conditional', 'request'})
        self.assertEqual(handler.wfile.getvalue(), b'')

    def test_sqlite_issue_cache_hits_do_not_read_projection_settings_or_create_github(self):
        with tempfile.TemporaryDirectory() as root:
            store = selected_store(Path(root))
            with store.read_snapshot() as db:
                db.execute('SELECT 1').fetchone()
            with patch.object(dashboard, 'WD', root), \
                    patch.object(dashboard, 'VIEWS', ViewCache(clock=lambda: 1)), \
                    patch('forge.factory.issues.IssueInventory.start_refresh'), \
                    patch('forge.factory.issues.GitHub') as github, \
                    patch('forge.factory.issues.load', wraps=load) as runtime:
                first = self.handler('/api/issues'); first.do_GET()
                first.send_response.assert_called_once_with(200)
                runtime.assert_called_once()
                tag = self.header(first, 'ETag')
                warm = self.handler('/api/issues'); warm.do_GET()
                self.assertEqual(warm.wfile.getvalue(), first.wfile.getvalue())
                self.assertIn('cache_hit', self.timing(warm))
                unchanged = self.handler('/api/issues', tag); unchanged.do_GET()
                unchanged.send_response.assert_called_once_with(304)
                runtime.assert_called_once()
                github.assert_not_called()

    def test_uncached_auth_and_error_responses_do_not_inherit_timing(self):
        handler = self.handler('/api/factory?section=board')
        handler._send('{"ok":false,"reason":"Unavailable"}', status=503)
        self.assertIsNone(self.header(handler, 'Server-Timing'))
