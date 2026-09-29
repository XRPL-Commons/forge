import io
import json
import unittest
from unittest.mock import Mock
from tests.test_dashboard import dashboard


class ConditionalHTTPTests(unittest.TestCase):
    def handler(self, etag=None, command='GET'):
        handler = object.__new__(dashboard.H)
        handler.command = command
        handler.headers = {'If-None-Match': etag} if etag else {}
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        return handler

    def test_unchanged_get_has_no_body_but_changes_and_restarts_get_fresh_data(self):
        body = json.dumps({'ok': True, 'controls_token': 'first', 'issues': []})
        first = self.handler(); first._send(body)
        etag = next(call.args[1] for call in first.send_header.call_args_list if call.args[0] == 'ETag')
        same = self.handler(etag); same._send(body)
        same.send_response.assert_called_once_with(304)
        self.assertEqual(same.wfile.getvalue(), b'')
        for new in ({'ok': True, 'controls_token': 'second', 'issues': []}, {'ok': False, 'reason': 'Factory reset is in progress'}):
            changed = self.handler(etag); changed._send(json.dumps(new))
            changed.send_response.assert_called_once_with(200)
            self.assertEqual(json.loads(changed.wfile.getvalue()), new)

    def test_actions_never_use_read_validators(self):
        handler = self.handler('anything', command='POST'); handler._send('{"ok":true}')
        handler.send_response.assert_called_once_with(200)
        self.assertFalse(any(call.args[0] == 'ETag' for call in handler.send_header.call_args_list))
