import io
import json
import tempfile
import unittest
from unittest.mock import Mock, patch
from tests.test_dashboard import dashboard


class SettingsHTTPTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        root=patch.object(dashboard,'WD',temporary.name);root.start();self.addCleanup(root.stop)

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
        for path in ('/issues','/api/issues','/settings','/settings/github','/api/settings','/factory','/api/factory','/api/factory/output?id=invalid','/onboarding','/reset','/api/onboarding','/onboarding/github/callback'):
            handler = self.handler(path, Host='attacker.example:8787')
            handler.do_GET(); handler.send_error.assert_called_once_with(403)

    def test_active_home_opens_activity(self):
        from pathlib import Path
        marker=Path(dashboard.WD)/'state/factory-v2.enabled'
        marker.parent.mkdir(parents=True,exist_ok=True);marker.touch()
        handler=self.handler('/');handler.do_GET()
        handler._send.assert_called_once_with(dashboard.FACTORY_PAGE,'text/html; charset=utf-8')

    def test_github_access_page_is_available_during_setup(self):
        handler=self.handler('/settings/github');handler.do_GET()
        handler._send.assert_called_once_with(dashboard.SETTINGS_PAGE,'text/html; charset=utf-8')

    def test_settings_get_is_local_only(self):
        service = Mock(); service.view.return_value = {'config':{},'revision':'abc'}
        with patch.object(dashboard,'settings_service',return_value=service):
            handler = self.handler('/api/settings'); handler.do_GET()
        result = json.loads(handler._send.call_args.args[0])
        self.assertEqual(result['controls_token'],dashboard.CONTROLS_TOKEN)
        service.start_refresh.assert_not_called(); service.refresh.assert_not_called()

    def test_settings_mutations_reject_cross_origin_and_bad_token(self):
        for path in ('/api/issues/action','/api/issues/refresh','/api/settings','/api/settings/sync','/api/factory/action','/api/runtime','/api/onboarding'):
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

    def test_onboarding_start_and_reset_queue_supervisor_intent(self):
        for action,fields in [('start',{'revision':'verified'}),('reset',{'generation':'current'})]:
            with patch('forge.onboarding.Onboarding') as setup:
                setup.return_value.queue.return_value='intent'
                handler=self.handler('/api/onboarding',{'action':action,**fields})
                handler.do_POST()
                setup.return_value.queue.assert_called_once_with(action,**fields)
                self.assertTrue(json.loads(handler._send.call_args.args[0])['ok'])

    def test_manifest_callback_binds_browser_cookie_and_never_accepts_installation_id(self):
        with patch('forge.onboarding.Onboarding') as setup:
            setup.return_value.callback.return_value='https://github.com/apps/fixture/installations/new'
            handler=self.handler('/onboarding/github/callback?state=nonce&code=code',Cookie='forge_setup='+('a'*43))
            handler._redirect=Mock();handler.do_GET()
            setup.return_value.callback.assert_called_once_with('nonce','code','a'*43)
            handler._redirect.assert_called_once_with('https://github.com/apps/fixture/installations/new')
            setup.reset_mock()
            handler=self.handler('/onboarding/github/callback?installation_id=999')
            handler._redirect=Mock();handler.do_GET()
            setup.return_value.callback.assert_not_called()
            handler._redirect.assert_called_once_with('/onboarding?connection_error=1')

    def test_setup_page_issues_http_only_callback_cookie(self):
        handler=self.handler('/onboarding');handler.do_GET()
        headers=handler._send.call_args.args[2]
        self.assertIn('HttpOnly',headers['Set-Cookie'])
        self.assertIn('SameSite=Lax',headers['Set-Cookie'])
        self.assertNotIn('localStorage',handler._send.call_args.args[0])


    def test_issue_decision_validates_schema_before_queueing(self):
        with patch('forge.factory.issue_actions.request', return_value='intent') as request:
            value={'key':'1:1','action':'queue','revision':'current','reason':'Operator decision'}
            handler=self.handler('/api/issues/action', value); handler.do_POST()
            request.assert_called_once_with(dashboard.WD, **value)
            request.reset_mock()
            handler=self.handler('/api/issues/action', {**value,'force':True}); handler.do_POST()
            request.assert_not_called()
            self.assertFalse(json.loads(handler._send.call_args.args[0])['ok'])


if __name__ == '__main__': unittest.main()
