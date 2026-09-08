import os, tempfile, unittest, stat
from types import SimpleNamespace
from unittest.mock import patch, Mock
from observer.config import load_repos, Repo, resolve_claude
from observer.isolation import IsolationError

class TestLoadRepos(unittest.TestCase):
    def _write(self, d, name, body):
        with open(os.path.join(d, name), "w") as f:
            f.write(body)

    def test_parses_enabled_repos_sorted(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "zeta.repo",
                'REPO="example-org/zeta"\nCLONE_BUILD="/home/forge-operator/zeta"\nENABLED=true\n')
            self._write(d, "alpha.repo",
                'REPO="forge-bot/alpha"\nCLONE_BUILD="/home/forge-operator/alpha"\nENABLED=true\n')
            repos = load_repos(d)
            self.assertEqual([r.name for r in repos], ["alpha", "zeta"])
            self.assertEqual(repos[0].repo, "forge-bot/alpha")
            self.assertEqual(repos[0].clone_build, "/home/forge-operator/alpha")

    def test_skips_disabled(self):
        with tempfile.TemporaryDirectory() as d:
            self._write(d, "off.repo",
                'REPO="X/off"\nCLONE_BUILD="/home/forge-operator/off"\nENABLED=false\n')
            self.assertEqual(load_repos(d), [])


class TestResolveClaude(unittest.TestCase):
    def secure_path(self, owner=0, ancestor_owner=0):
        executable = Mock()
        executable.stat.return_value = SimpleNamespace(st_uid=owner, st_mode=stat.S_IFREG | 0o755)
        ancestor = Mock()
        ancestor.stat.return_value = SimpleNamespace(st_uid=ancestor_owner)
        executable.parents = [ancestor]
        return executable

    def test_missing_or_relative_pin_never_discovers_from_path_or_home(self):
        for configured in ('', 'claude', '~/bin/claude'):
            with self.subTest(configured=configured), patch.dict(os.environ, {'FORGE_OBSERVER_CLAUDE_BIN':configured, 'PATH':'/untrusted/bin', 'HOME':'/untrusted/home'}):
                with self.assertRaises(IsolationError): resolve_claude()

    def test_explicit_protected_root_executable_is_returned(self):
        configured = '/opt/forge-cli/claude/2.1.261/claude'
        with patch.dict(os.environ, {'FORGE_OBSERVER_CLAUDE_BIN':configured}), patch('observer.config.protected_path', return_value=self.secure_path()) as verify, patch('observer.config.os.access', return_value=True):
            self.assertEqual(resolve_claude(), configured)
            verify.assert_called_once_with(configured, directory=False)

    def test_nonroot_executable_or_replaceable_parent_is_rejected(self):
        for owner, ancestor in ((1000,0),(0,1000)):
            with self.subTest(owner=owner,ancestor=ancestor), patch.dict(os.environ, {'FORGE_OBSERVER_CLAUDE_BIN':'/opt/pinned/claude'}), patch('observer.config.protected_path', return_value=self.secure_path(owner,ancestor)), patch('observer.config.os.access', return_value=True):
                with self.assertRaises(IsolationError): resolve_claude()

    def test_nonexecutable_file_or_unprotected_path_is_rejected(self):
        with patch.dict(os.environ, {'FORGE_OBSERVER_CLAUDE_BIN':'/opt/pinned/claude'}), patch('observer.config.protected_path', return_value=self.secure_path()), patch('observer.config.os.access', return_value=False):
            with self.assertRaises(IsolationError): resolve_claude()
        with patch.dict(os.environ, {'FORGE_OBSERVER_CLAUDE_BIN':'/opt/pinned/claude'}), patch('observer.config.protected_path', side_effect=IsolationError('symlink or writable ancestor')):
            with self.assertRaises(IsolationError): resolve_claude()

if __name__ == "__main__":
    unittest.main()
