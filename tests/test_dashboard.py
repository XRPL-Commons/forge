import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location("forge_dashboard", Path(__file__).parents[1] / "legacy/forge-status.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class HostTests(unittest.TestCase):
    def test_rebound_hostname_cannot_read_token_or_post_even_with_token(self):
        import io
        handler = object.__new__(dashboard.H)
        handler.server = Mock(server_address=('127.0.0.1', 8787))
        handler.path = '/api/settings'
        handler.headers = {'Host': 'attacker.example:8787', 'Origin': 'http://attacker.example:8787',
                           'X-Forge-Action': dashboard.CONTROLS_TOKEN, 'Content-Type': 'application/json', 'Content-Length': '2'}
        handler.rfile = io.BytesIO(b'{}')
        handler._send = Mock(); handler.send_error = Mock()
        handler.do_POST()
        handler.send_error.assert_called_once_with(403)
        handler = object.__new__(dashboard.H)
        handler.server = Mock(server_address=('127.0.0.1', 8787))
        handler.headers = {'Host': 'attacker.example:8787'}; handler.path = '/api/factory'
        handler.send_error = Mock(); handler._send = Mock()
        handler.do_GET()
        handler.send_error.assert_called_once_with(403); handler._send.assert_not_called()

    def test_retired_legacy_endpoints_are_gone(self):
        for path in ('/api/status', '/api/workflow', '/api/history', '/api/repository', '/api/pane'):
            handler = object.__new__(dashboard.H)
            handler.server = Mock(server_address=('127.0.0.1', 8787))
            handler.headers = {'Host': '127.0.0.1:8787'}; handler.path = path
            handler.send_error = Mock(); handler._send = Mock()
            handler.do_GET()
            handler.send_error.assert_called_once_with(404)


if __name__ == '__main__':
    unittest.main()
