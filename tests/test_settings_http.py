import io
import json
import unittest
from unittest.mock import Mock, patch
from tests.test_dashboard import dashboard


class SettingsHTTPTests(unittest.TestCase):
    def handler(self, path, body=None, **headers):
        handler = object.__new__(dashboard.H)
        handler.server = Mock(server_address=('127.0.0.1',8787))
        handler.headers = {'Host':'127.0.0.1:8787', 'Origin':'http://127.0.0.1:8787',
                           'X-Forge-Action':dashboard.CONTROLS_TOKEN, 'Content-Type':'application/json', **headers}
        handler.path = path
        handler._send = Mock(); handler.send_error = Mock()
        payload = json.dumps(body).encode()
        handler.rfile = io.BytesIO(payload); handler.headers['Content-Length'] = str(len(payload))
        return handler

    def test_page_and_api_require_trusted_host(self):
        for path in ('/settings','/api/settings','/factory','/api/factory','/api/factory/output?id=invalid'):
            handler = self.handler(path, Host='attacker.example:8787')
            handler.do_GET(); handler.send_error.assert_called_once_with(403)

    def test_settings_get_is_local_only(self):
        service = Mock(); service.view.return_value = {'config':{},'revision':'abc'}
        with patch.object(dashboard,'settings_service',return_value=service):
            handler = self.handler('/api/settings'); handler.do_GET()
        result = json.loads(handler._send.call_args.args[0])
        self.assertEqual(result['controls_token'],dashboard.CONTROLS_TOKEN)
        service.start_refresh.assert_not_called(); service.refresh.assert_not_called()

    def test_settings_mutations_reject_cross_origin_and_bad_token(self):
        for path in ('/api/settings','/api/settings/sync','/api/factory/action','/api/runtime'):
            for headers in ({'Origin':'http://attacker.example'}, {'X-Forge-Action':'bad'}, {'Sec-Fetch-Site':'cross-site'}):
                with patch.object(dashboard,'settings_service') as service:
                    handler = self.handler(path,{},**headers); handler.do_POST()
                    handler.send_error.assert_called_once_with(403); service.assert_not_called()

    def test_sync_is_requested_asynchronously(self):
        service = Mock()
        with patch.object(dashboard,'settings_service',return_value=service):
            handler = self.handler('/api/settings/sync',{}); handler.do_POST()
        service.start_refresh.assert_called_once_with(); service.refresh.assert_not_called()
        self.assertTrue(json.loads(handler._send.call_args.args[0])['ok'])

    def test_save_passes_revision_and_rejects_extra_fields(self):
        service = Mock(); service.save.return_value = {'config':{},'revision':'new'}
        with patch.object(dashboard,'settings_service',return_value=service):
            handler = self.handler('/api/settings',{'config':{},'revision':'old'}); handler.do_POST()
            service.save.assert_called_once_with({},'old')
            service.reset_mock()
            handler = self.handler('/api/settings',{'config':{},'revision':'old','private_key':'not-accepted'}); handler.do_POST()
            service.save.assert_not_called()
            self.assertFalse(json.loads(handler._send.call_args.args[0])['ok'])


if __name__ == '__main__': unittest.main()
