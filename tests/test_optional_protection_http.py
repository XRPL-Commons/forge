"""The operator protection choice uses the normal authenticated mutation surface."""
import json
import unittest
from unittest.mock import patch
from tests import test_settings_http


class ProtectionHTTPTests(unittest.TestCase):
    setUp = test_settings_http.SettingsHTTPTests.setUp
    handler = test_settings_http.SettingsHTTPTests.handler

    def test_requires_csrf_and_exact_payload(self):
        value = dict(repo_id=10, revision='current', mode='optional')
        handler = self.handler('/api/repositories/github-protection', value, Origin='http://attacker.example')
        handler.do_POST()
        handler.send_error.assert_called_once_with(403)
        with patch('forge.repository_setup.RepositorySetup') as service:
            service.return_value.github_protection.return_value = {'revision': 'new', 'repositories': []}
            handler = self.handler('/api/repositories/github-protection', value)
            handler.do_POST()
            service.return_value.github_protection.assert_called_once_with(**value)
            self.assertEqual(json.loads(handler._send.call_args.args[0])['revision'], 'new')
            service.return_value.github_protection.reset_mock()
            handler = self.handler('/api/repositories/github-protection', {**value, 'skip_audit': True})
            handler.do_POST()
            service.return_value.github_protection.assert_not_called()
            self.assertFalse(json.loads(handler._send.call_args.args[0])['ok'])

    def test_active_merge_reports_retryable_busy(self):
        with patch('forge.repository_setup.RepositorySetup') as service:
            service.return_value.github_protection.side_effect = BlockingIOError()
            handler = self.handler('/api/repositories/github-protection',
                                   dict(repo_id=10, revision='current', mode='optional'))
            handler.do_POST()
            self.assertEqual(handler._send.call_args.kwargs['status'], 409)
            self.assertIn('Try saving again', json.loads(handler._send.call_args.args[0])['reason'])


if __name__ == '__main__':
    unittest.main()
