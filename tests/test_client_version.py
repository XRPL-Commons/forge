import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge_client import version
from forge_client.cli import parser


class VersionTests(unittest.TestCase):
    def test_managed_revision_is_stamped_in_installed_prefix(self):
        with patch.object(version.sys, 'prefix', '/private/client/' + 'a'*40 + '-' + 'b'*32):
            self.assertEqual(version.client_revision(), 'a'*40)

    def test_pip_vcs_install_uses_recorded_commit(self):
        distribution = Mock(); distribution.read_text.return_value = json.dumps({'vcs_info': {'commit_id': 'a'*40}})
        with patch.object(version.sys, 'prefix', '/pip/venv'), patch.object(version.importlib.metadata, 'distribution', return_value=distribution):
            self.assertEqual(version.client_revision(), 'a'*40)

    def test_actual_server_revision_is_compared_instead_of_saved_install_intent(self):
        installation = Mock()
        installation.journal.read.return_value = {'host_key': 'pinned', 'intent': {'revision': 'a'*40}}
        installation.host.rpc.return_value = {'ok': True, 'revision': 'b'*40}
        with patch.object(version, 'client_revision', return_value='a'*40), patch('forge_client.install.Installation', return_value=installation):
            result = version.report('anvil')
        self.assertFalse(result['compatible'])
        self.assertIn('differ', result['warning'])
        self.assertEqual(installation.host.fingerprint, 'pinned')
        namespace = {}; exec(installation.host.rpc.call_args.args[0], namespace)
        with patch('subprocess.run', return_value=SimpleNamespace(stdout='b'*40, returncode=0)) as run, patch('builtins.print') as output:
            self.assertEqual(namespace['main']('unused'), 0)
            self.assertEqual(run.call_args.args[0][:4], ['runuser','-u','forge','--'])
            self.assertIn('HOME=/home/forge', run.call_args.args[0])
        self.assertEqual(json.loads(output.call_args.args[0])['revision'], 'b'*40)

    def test_login_and_help_surface(self):
        args = parser().parse_args(['account', 'login', 'codex', '--ssh', 'anvil', '--json'])
        self.assertEqual((args.provider, args.ssh, args.json), ('codex', 'anvil', True))
        self.assertEqual(parser().parse_args(['self-update', '--revision', 'a'*40]).revision, 'a'*40)
