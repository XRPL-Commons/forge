import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.installation_identity import configure
from forge.operator_auth import OperatorAuth
from forge_client.contracts import SetupError
from forge_client.install_identity import operator
from tests.sqlite_fixture import selected_store


class InstallationIdentityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        selected_store(self.root)
        self.auth = OperatorAuth(self.root)
        self.auth.configure('https://anvil.example.ts.net', 'Iv1client', 'a' * 40, [123])
        self.bootstrap = Mock()
        self.proxy = False
        self.bootstrap.snapshot.return_value = {'revision': 'r'}
        self.bootstrap.command.side_effect = lambda args: (
            json.dumps({'Web': {}} if self.proxy else {}).encode() if args[0] == 'tailscale'
            else b'NITRO_FORGE_TAILSCALE_IDENTITY=serve' if self.proxy else b'old unit')
        self.bootstrap.execute.side_effect = lambda request: setattr(self, 'proxy', True)
        self.patcher = patch('forge.installation_identity.Bootstrap', return_value=self.bootstrap)
        self.patcher.start(); self.addCleanup(self.patcher.stop)
        self.kw = {'setup': Mock(), 'actor_id': 123, 'tailnet_operator': {'operator_id': 123, 'login': 'alice@example.com'}}

    def test_plan_does_not_link_identity_or_reconcile_services(self):
        self.assertEqual(configure(self.root, **self.kw)['status'], 'identity_required')
        self.assertEqual(self.auth.identity_settings()['tailnet_operators'], [])
        self.bootstrap.execute.assert_not_called()

    def test_apply_then_resume_links_only_installing_operator_without_repeat_update(self):
        self.assertEqual(configure(self.root, **self.kw, apply=True)['status'], 'identity_ready')
        self.assertEqual(self.auth.tailnet_session('alice@example.com')[1]['operator']['id'], 123)
        configure(self.root, **self.kw, apply=True)
        self.bootstrap.execute.assert_called_once()

    def test_cannot_link_another_actor_or_tagged_device(self):
        for operator_id, name in ((456, 'alice@example.com'), (123, 'tagged-devices@example.com')):
            with self.assertRaises(SetupError):
                configure(self.root, **{**self.kw, 'tailnet_operator': {'operator_id': operator_id, 'login': name}}, apply=True)
        self.bootstrap.execute.assert_not_called()

    def test_local_identity_comes_from_self_user_not_github_login(self):
        github = Mock(); github.identity.return_value = {'id': 123, 'login': 'github-name'}
        data = {'BackendState': 'Running', 'Self': {'UserID': 7},
                'User': {'7': {'LoginName': 'Alice@example.com'}, '8': {'LoginName': 'other@example.com'}}}
        self.assertEqual(operator(github, run=lambda *a, **k: json.dumps(data).encode()),
                         {'operator_id': 123, 'login': 'alice@example.com'})
        data['Self']['Tags'] = ['tag:server']
        with self.assertRaises(SetupError):
            operator(github, run=lambda *a, **k: json.dumps(data).encode())
