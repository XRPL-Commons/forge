"""Retired GitHub policy endpoints cannot change repository protections."""
import json
import unittest
from unittest.mock import patch
from tests import test_settings_http


class RetiredProtectionHTTPTests(unittest.TestCase):
    setUp = test_settings_http.SettingsHTTPTests.setUp
    handler = test_settings_http.SettingsHTTPTests.handler

    def test_old_protection_setting_cannot_mutate_repository(self):
        value = dict(repo_id=10, revision='current', mode='optional')
        with patch('forge.repository_setup.RepositorySetup') as service:
            handler = self.handler('/api/repositories/github-protection', value)
            handler.do_POST()
            service.assert_not_called()
            self.assertFalse(json.loads(handler._send.call_args.args[0])['ok'])

    def test_retired_endpoint_keeps_request_authentication(self):
        handler = self.handler('/api/repositories/github-protection', {}, Origin='http://attacker.example')
        handler.do_POST()
        handler.send_error.assert_called_once_with(403)


if __name__ == '__main__':
    unittest.main()
