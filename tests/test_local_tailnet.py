"""The app-bundle CLI serves both install preflight and verified operator lookup."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge_client import tailnet
from forge_client.contracts import SetupError
from forge_client.install_identity import operator
from forge_client.preflight import observe


class LocalTailnetTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.app = Path(directory.name) / 'Tailscale.app/Contents/MacOS/Tailscale'
        self.app.parent.mkdir(parents=True)
        self.app.write_text('#!/bin/sh\nexit 0\n'); self.app.chmod(0o755)
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(tailnet, 'MACOS_EXECUTABLE', self.app))
        self.stack.enter_context(patch.object(tailnet.sys, 'platform', 'darwin'))

    def test_path_cli_takes_precedence_over_installed_app(self):
        with patch.object(tailnet.shutil, 'which', return_value='/opt/homebrew/bin/tailscale'):
            self.assertEqual(tailnet.executable(), '/opt/homebrew/bin/tailscale')

    def test_mac_app_is_used_by_both_preflight_and_identity(self):
        data = {'BackendState': 'Running', 'Self': {'UserID': 7},
                'User': {'7': {'LoginName': 'operator@example.com'}}, 'Peer': {}}
        run = Mock(return_value=json.dumps(data).encode())
        github = Mock(); github.identity.return_value = {'id': 123, 'login': 'github-operator'}
        selected = {'name': 'Anvil', 'hostname': 'anvil', 'revision': 'a' * 40, 'host_key': None,
                    'operators': [github.identity.return_value],
                    'owner': {'id': 123, 'login': 'github-operator', 'type': 'User'}, 'repositories': [], 'providers': []}
        installation = Mock()
        installation.execute.return_value = {'host': {'cpus': 4, 'memory_mib': 16000, 'free_disk_mib': 50000}}
        installation.host.rpc.return_value = {'ok': True, 'userns_restricted': '0', 'egress': {}, 'tailnet': None}
        with patch.object(tailnet.shutil, 'which', side_effect=lambda name: None if name == 'tailscale' else '/usr/bin/' + name), \
             patch('forge_client.preflight.client_revision', return_value='a' * 40):
            report = observe(selected, installation, github, run=run)
            row = next(item for item in report['items'] if item['id'] == 'local.tailnet')
            self.assertEqual(row['status'], 'pass')
            self.assertEqual(row['command'], [str(self.app), 'up'])
            self.assertEqual(operator(github, run=run), {'operator_id': 123, 'login': 'operator@example.com'})
        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertEqual(call.args[0], [str(self.app), 'status', '--json'])
            self.assertEqual(call.kwargs['timeout'], 15)
            self.assertEqual(call.kwargs['environ']['TERM'], 'dumb')
            self.assertNotIn('GH_TOKEN', call.kwargs['environ'])
        # Resolution requires no symlink or modification to the installation.
        self.assertFalse(self.app.is_symlink())

    def test_missing_or_nonexecutable_app_does_not_run_a_command(self):
        run = Mock()
        with patch.object(tailnet.shutil, 'which', return_value=None):
            for exists in (True, False):
                if exists: self.app.chmod(0o644)
                else: self.app.unlink()
                with self.subTest(exists=exists), self.assertRaises(SetupError) as error:
                    operator(Mock(), run=run)
                self.assertEqual(error.exception.code, 'tailnet_cli_missing')
        run.assert_not_called()

    def test_linux_does_not_select_a_mac_app_path(self):
        with patch.object(tailnet.shutil, 'which', return_value=None), patch.object(tailnet.sys, 'platform', 'linux'):
            with self.assertRaises(SetupError) as error: tailnet.executable()
            self.assertEqual(error.exception.code, 'tailnet_cli_missing')

    def test_real_bounded_subprocess_gets_cli_marker_without_shell_or_github_secrets(self):
        from forge_client.github import run_bounded
        self.app.write_text('''#!/bin/sh
if [ "${TERM-}" != dumb ]; then echo 'GUI mode selected'; exit 1; fi
if [ -n "${GH_TOKEN-}${GITHUB_TOKEN-}${FORGE_SENTINEL_SECRET-}${TERM_PROGRAM-}${SHLVL_PARENT-}" ]; then exit 2; fi
printf '%s' '{"BackendState":"Running","Self":{"UserID":7},"User":{"7":{"LoginName":"operator@example.com"}}}'
''')
        github = Mock(); github.identity.return_value = {'id': 123, 'login': 'github-operator'}
        with patch.object(tailnet.shutil, 'which', return_value=None), \
             patch.dict(tailnet.os.environ, {'TERM': 'operator-terminal', 'TERM_PROGRAM': 'private-terminal',
                 'GH_TOKEN': 'synthetic-private-github-token', 'GITHUB_TOKEN': 'another-private-token',
                 'FORGE_SENTINEL_SECRET': 'synthetic-private-value', 'SHLVL_PARENT': 'private-shell-state'}):
            self.assertEqual(operator(github, run=run_bounded),
                             {'operator_id': 123, 'login': 'operator@example.com'})


if __name__ == '__main__': unittest.main()
