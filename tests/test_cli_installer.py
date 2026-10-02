"""Exercise the install script with isolated paths and fake package commands."""
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

SCRIPT = (Path(__file__).resolve().parents[1] / 'install-cli.sh').read_text().split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
COMMIT = 'a' * 40


class CLIInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.home = Path(self.temporary.name).resolve()
        self.source = self.home / 'source'
        (self.source / 'forge_client').mkdir(parents=True)
        (self.source / 'forge_client/cli.py').write_text('# fixture')
        self.commands = []
        self.fail = None
        self.release = None

    def command(self, argv, **kwargs):
        self.commands.append(argv)
        self.assertNotIn('synthetic-github-admin-token', json.dumps([argv, kwargs.get('env')]))
        if self.fail and self.fail in argv:
            raise RuntimeError('synthetic-github-admin-token in diagnostics')
        output = ''
        if argv[:3] == ['git', 'rev-parse', 'HEAD']:
            output = COMMIT
        elif argv[:2] == ['git', 'ls-remote']:
            output = COMMIT + '\trefs/heads/main'
        elif '-m' in argv and 'venv' in argv:
            self.release = Path(argv[-1]); (self.release / 'bin').mkdir(parents=True)
            (self.release / 'bin/forge').write_text('# installed executable')
        elif argv[-2:] == ['--json', 'commands']:
            output = json.dumps({'ok': True, 'factory_setup_commands': {}})
        return SimpleNamespace(stdout=output)

    def install(self, arguments=None):
        namespace = {'__name__': '__main__'}
        with patch('pathlib.Path.home', return_value=self.home), patch('pathlib.Path.cwd', return_value=self.source), \
                patch('subprocess.run', side_effect=self.command), patch('sys.argv', ['-', *(arguments or [])]), \
                patch.dict(os.environ, GH_TOKEN='synthetic-github-admin-token'), \
                patch('sys.stdout', new_callable=io.StringIO) as output, patch('sys.stderr', new_callable=io.StringIO) as errors:
            try:
                exec(compile(SCRIPT, 'install-cli.sh', 'exec'), namespace)
                status = 0
            except SystemExit as error:
                status = error.code
            finally:
                if 'lock' in namespace: os.close(namespace['lock'])
        return status, output.getvalue(), errors.getvalue()

    def test_public_install_pins_revision_checks_command_and_records_timing(self):
        status, output, errors = self.install()
        self.assertEqual(status, 0, errors)
        executable = self.home / '.local/bin/forge'
        self.assertEqual(executable.resolve(), self.release / 'bin/forge')
        pip = next(command for command in self.commands if 'pip' in command)
        self.assertEqual(pip[-1], 'git+https://github.com/XRPL-Commons/forge.git@' + COMMIT)
        record = next((self.home / '.local/state/forge-client').glob('cli-install-*.json'))
        value = json.loads(record.read_text())
        self.assertEqual(value['status'], 'installed')
        self.assertGreaterEqual(value['installed_at'], value['started_at'])
        self.assertEqual(record.stat().st_mode & 0o777, 0o600)
        self.assertIn('setup new', output)
        self.assertNotIn('synthetic-github-admin-token', record.read_text() + output + errors)

    def test_local_clone_installs_only_explicit_source(self):
        status, _, errors = self.install(['--local'])
        self.assertEqual(status, 0, errors)
        self.assertFalse(any('ls-remote' in command for command in self.commands))
        pip = next(command for command in self.commands if 'pip' in command)
        self.assertEqual(pip[-1], str(self.source))

    def test_foreign_command_is_never_overwritten(self):
        executable = self.home / '.local/bin/forge'
        executable.parent.mkdir(parents=True)
        executable.write_text('foreign command')
        status, _, errors = self.install()
        self.assertEqual(status, 1)
        self.assertEqual(executable.read_text(), 'foreign command')
        self.assertIn('Another forge executable', errors)
        self.assertFalse(self.commands)

    def test_failed_replacement_keeps_existing_command_and_records_failure(self):
        self.assertEqual(self.install()[0], 0)
        executable = self.home / '.local/bin/forge'
        original = executable.resolve()
        self.fail = 'pip'
        status, output, errors = self.install()
        self.assertEqual(status, 1)
        self.assertEqual(executable.resolve(), original)
        self.assertNotIn('synthetic-github-admin-token', output + errors)
        records = [json.loads(path.read_text()) for path in (self.home / '.local/state/forge-client').glob('cli-install-*.json')]
        self.assertEqual(sorted(r['status'] for r in records), ['failed', 'installed'])


if __name__ == '__main__':
    unittest.main()
