from contextlib import ExitStack
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge import host_update, host_setup


class FakeHost:
    """Docker, systemd and sudo stand-ins that record what an update did."""

    def __init__(self, directory):
        self.base = Path(directory)
        self.root = self.base / 'root'; self.root.mkdir(mode=0o700)
        self.home = self.base / 'home'; self.home.mkdir()
        self.apparmor = self.base / 'apparmor/forge-runner-private-proc'; self.apparmor.parent.mkdir()
        self.images, self.calls, self.revision, self.builds = {}, [], 'a' * 40, 0
        self.qualify = Mock()

    def run(self, argv, check=False, **kwargs):
        self.calls.append(argv)
        result = Mock(returncode=0, stdout='')
        if argv[:2] == ['git', '-C'] and argv[3:] == ['rev-parse', 'HEAD']:
            result.stdout = self.revision + '\n'
        elif argv[:3] == ['docker', 'image', 'inspect']:
            if argv[3] in self.images:
                result.stdout = self.images[argv[3]] + '\n'
            else:
                result.returncode = 1
        elif argv[:2] == ['docker', 'build']:
            tag = argv[argv.index('-t') + 1]
            self.builds += 1
            self.images[tag] = 'sha256:' + str(self.builds) * 64
        elif argv[:2] == ['docker', 'run'] and 'npm run build' in argv[-1]:
            build = Path(argv[argv.index('--mount') + 1].split('source=')[1].split(',')[0])
            (build / '.output/server').mkdir(parents=True)
            (build / '.output/server/index.mjs').write_text('ui')
        elif argv[:2] == ['sudo', 'install']:
            Path(argv[-1]).write_bytes(Path(argv[-2]).read_bytes())
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, argv)
        return result

    def patches(self):
        policy = {'defaultAction': 'SCMP_ACT_ERRNO'}
        return [patch.object(host_update, 'command', side_effect=lambda argv, **kw: self.run(argv, check=True, **kw)),
                patch.object(host_update.subprocess, 'run', side_effect=self.run),
                patch.object(host_update.Path, 'home', return_value=self.home),
                patch.object(host_update, 'APPARMOR_TARGET', self.apparmor),
                patch.object(host_update.shutil, 'which', return_value='/usr/bin/docker'),
                patch.object(host_update.urllib.request, 'urlopen', side_effect=lambda *a, **k: io.BytesIO(b'{}')),
                patch.object(host_update.runpy, 'run_path', return_value={'generate': lambda *a: policy}),
                patch.object(host_update, 'qualify_runner', self.qualify),
                patch.object(host_update, 'wait_healthy'),
                patch.object(host_update, 'Onboarding', return_value=Mock(runtime=Mock(return_value={'slots': {}})))]

    def reconcile(self, **kwargs):
        with ExitStack() as stack:
            for item in self.patches():
                stack.enter_context(item)
            self.calls.clear()
            with patch('sys.stdout', io.StringIO()) as out:
                changed = host_update.reconcile(self.root, **kwargs)
            return changed, out.getvalue()

    def restarted(self):
        return [argv[3:] for argv in self.calls if argv[:3] == ['systemctl', '--user', 'restart']]

    def unit(self, name):
        return (self.home / '.config/systemd/user' / (name + '.service')).read_text()


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.host = FakeHost(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_fresh_install_runs_the_operator_ui_in_front_of_a_loopback_api(self):
        self.host.reconcile()
        status, ui = self.host.unit('forge-status'), self.host.unit('forge-ui')
        self.assertIn('FORGE_BIND_ADDRESS=127.0.0.1', status)
        self.assertIn('FORGE_PORT=8788', status)
        self.assertIn('"HOST=127.0.0.1"', ui)
        self.assertIn('"NITRO_FORGE_API=http://127.0.0.1:8788"', ui)
        self.assertIn(host_update.NODE_IMAGE, ui)
        self.assertIn('service --root', self.host.unit('forge-supervisor'))
        secret = self.host.root / 'state/ui-upstream.secret'
        self.assertEqual(secret.stat().st_mode & 0o777, 0o600)
        self.assertIn('FORGE_UPSTREAM_SECRET_FILE=' + str(secret), status)
        self.assertIn('source=' + str(secret) + ',target=/run/forge/upstream.secret,readonly', ui)
        self.assertIn('NITRO_FORGE_UPSTREAM_SECRET_FILE=/run/forge/upstream.secret', ui)
        self.assertTrue((self.host.root / 'ui/current/server/index.mjs').exists())
        for guide in ('onboarding.md', 'issue-state-machine.md', 'factory-runtime.md'):
            self.assertTrue((self.host.root / 'ui/current/docs' / guide).exists())
        self.assertIn('"FORGE_ROOT=/app"', ui)
        self.assertEqual(self.host.restarted(), [['forge-status.service', 'forge-ui.service', 'forge-supervisor.service']])
        self.host.qualify.assert_called_once()
        runtime = json.loads((self.host.root / 'host-runtime.json').read_text())
        self.assertEqual(runtime['image'], 'sha256:' + '1' * 64)
        self.assertEqual(runtime['apparmor_profile'], 'forge-runner-private-proc')
        self.assertEqual(json.loads((self.host.root / 'host.json').read_text())['source'], 'a' * 40)

    def test_failed_fresh_canary_installs_no_services_or_success_marker(self):
        self.host.qualify.side_effect = RuntimeError('Browser unavailable')
        with self.assertRaisesRegex(RuntimeError, 'Browser unavailable'):
            self.host.reconcile()
        self.assertFalse((self.host.home / '.config/systemd/user/forge-ui.service').exists())
        self.assertFalse((self.host.root / 'host-runtime.json').exists())
        self.assertEqual(self.host.restarted(), [])

    def test_repeating_an_unchanged_update_touches_nothing(self):
        self.host.reconcile()
        changed, output = self.host.reconcile()
        self.assertFalse(changed)
        self.assertIn('Forge host is up to date at aaaaaaaaaaaa', output)
        self.assertEqual(self.host.restarted(), [])
        self.assertFalse(any(argv[:2] in (['docker', 'build'], ['docker', 'run'], ['docker', 'compose']) for argv in self.host.calls))
        self.host.qualify.assert_called_once()  # Only by the install.

    def test_a_source_change_restarts_the_python_services_only(self):
        self.host.reconcile()
        self.host.revision = 'b' * 40
        changed, _ = self.host.reconcile()
        self.assertFalse(changed)
        self.assertEqual(self.host.restarted(), [['forge-status.service', 'forge-supervisor.service']])
        self.assertEqual(self.host.builds, 1)

    def test_a_new_runner_is_qualified_before_every_runtime_switches_to_it(self):
        self.host.reconcile()
        (self.host.root / 'setup').mkdir()
        for name in ('setup/runtime.json', 'runtime.json'):
            (self.host.root / name).write_text(json.dumps({'image': 'sha256:' + '1' * 64, 'slots': {'build': 3}}))
        with patch.object(host_update, 'RUNNER_INPUTS', host_update.RUNNER_INPUTS + ('README.md',)):
            self.host.qualify.side_effect = RuntimeError('Browser unavailable')
            with self.assertRaisesRegex(RuntimeError, 'Browser unavailable'):
                self.host.reconcile()
            for name in ('host-runtime.json', 'setup/runtime.json', 'runtime.json'):
                self.assertEqual(json.loads((self.host.root / name).read_text())['image'], 'sha256:' + '1' * 64)
            self.host.qualify.side_effect = None
            changed, output = self.host.reconcile()
        self.assertTrue(changed)
        self.assertIn('runner image', output)
        self.assertEqual(self.host.builds, 2)  # The failed attempt's image is reused, not rebuilt.
        for name in ('host-runtime.json', 'setup/runtime.json', 'runtime.json'):
            self.assertEqual(json.loads((self.host.root / name).read_text())['image'], 'sha256:' + '2' * 64)
        self.assertEqual(json.loads((self.host.root / 'runtime.json').read_text())['slots'], {'build': 3})

    def test_an_update_interrupted_after_switching_still_requalifies_when_rerun(self):
        self.host.reconcile()
        with patch.object(host_update, 'RUNNER_INPUTS', host_update.RUNNER_INPUTS + ('README.md',)):
            with patch.object(host_update, 'switch_ui', side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):
                    self.host.reconcile()
            self.assertEqual(json.loads((self.host.root / 'host-runtime.json').read_text())['image'], 'sha256:' + '2' * 64)
            changed, _ = self.host.reconcile()
        self.assertTrue(changed)

    def test_a_source_update_prints_how_to_roll_back(self):
        self.host.reconcile()
        self.host.revision = 'b' * 40
        _, output = self.host.reconcile()
        self.assertIn('git checkout aaaaaaaaaaaa && bin/forge update --no-pull', output)

    def test_rolling_back_the_source_reuses_the_earlier_image_and_ui(self):
        self.host.reconcile()
        first = json.loads((self.host.root / 'host.json').read_text())
        with patch.object(host_update, 'RUNNER_INPUTS', host_update.RUNNER_INPUTS + ('README.md',)):
            self.host.reconcile()
        self.assertTrue(self.host.reconcile()[0])
        record = json.loads((self.host.root / 'host.json').read_text())
        self.assertEqual(record['image'], first['image'])
        self.assertEqual(self.host.builds, 2)

    def test_files_edited_outside_forge_are_only_replaced_after_confirmation(self):
        self.host.reconcile()
        path = self.host.home / '.config/systemd/user/forge-ui.service'
        path.write_text(path.read_text().replace('512m', '1g'))
        self.host.revision = 'b' * 40
        with patch.object(host_update.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'rerun with --yes'):
                self.host.reconcile()
        self.assertIn('1g', path.read_text())
        self.assertEqual(self.host.restarted(), [])
        self.host.reconcile(assume_yes=True)
        self.assertIn('512m', path.read_text())

    def test_drop_ins_that_override_the_service_are_moved_aside_after_confirmation(self):
        self.host.reconcile()
        units = self.host.home / '.config/systemd/user'
        override = units / 'forge-supervisor.service.d/99-onboarding-service.conf'
        override.parent.mkdir()
        override.write_text('[Service]\nExecStart=\nExecStart=/old/copy/bin/forge service --root /r\n')
        extra = units / 'forge-supervisor.service.d/capacity.conf'
        extra.write_text('[Service]\nEnvironment=MAX_SESSIONS=6\n')
        allowed = units / 'forge-ui.service.d/override.conf'
        allowed.parent.mkdir()
        allowed.write_text('[Service]\nEnvironment=NITRO_ALLOWED_HOSTS=forge.tail.ts.net:8787\n')
        with patch.object(host_update.sys.stdin, 'isatty', return_value=False):
            with self.assertRaisesRegex(ValueError, 'rerun with --yes'):
                self.host.reconcile()
        self.assertTrue(override.exists())
        _, output = self.host.reconcile(assume_yes=True)
        self.assertFalse(override.exists())
        self.assertTrue(extra.exists())
        self.assertTrue(allowed.exists())
        self.assertIn('remove overriding drop-in 99-onboarding-service.conf', output)
        moved = list((self.host.root / 'backups').glob('unit-dropins-*/forge-supervisor.service.d/99-onboarding-service.conf'))
        self.assertEqual(len(moved), 1)
        self.assertEqual(self.host.restarted(), [['forge-supervisor.service']])

    def test_environment_overrides_count_as_overriding(self):
        for line, overriding in [('Environment=PYTHONPATH=/old', True), ('Environment="FORGE_ROOT=/x" "A=b"', True),
                                 ('Environment=HOST=10.0.0.1', True), ('Environment=NITRO_ALLOWED_HOSTS=a:1', False),
                                 ('Environment=MAX_SESSIONS=6', False), ('MemoryMax=2G', False)]:
            self.assertEqual(bool(host_update.OVERRIDING.search('[Service]\n' + line + '\n')), overriding, line)

    def test_units_from_the_previous_installer_upgrade_without_a_prompt_and_keep_their_bind(self):
        units = self.host.home / '.config/systemd/user'; units.mkdir(parents=True)
        for name in ('forge-status', 'forge-supervisor'):
            (units / (name + '.service')).write_text(host_update.legacy_unit(name, self.host.root, '100.64.0.7'))
        (self.host.root / 'host-runtime.json').write_text(json.dumps({'image': 'sha256:' + '9' * 64}))
        with patch.object(host_update.sys.stdin, 'isatty', return_value=False):
            self.host.reconcile()
        self.assertIn('"HOST=100.64.0.7"', self.host.unit('forge-ui'))
        self.assertIn('FORGE_BIND_ADDRESS=127.0.0.1', self.host.unit('forge-status'))

    def test_the_operator_ui_only_binds_loopback_or_tailnet(self):
        with self.assertRaisesRegex(ValueError, 'loopback or a private tailnet'):
            self.host.reconcile(bind='0.0.0.0')


class PullTests(unittest.TestCase):
    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        base = Path(self.directory.name)
        self.upstream, self.clone = base / 'upstream', base / 'clone'
        self.upstream.mkdir()
        self.git(self.upstream, 'init', '-q', '-b', 'main')
        self.git(self.upstream, 'config', 'user.email', 'test@example.com')
        self.git(self.upstream, 'config', 'user.name', 'Test')
        (self.upstream / 'file').write_text('one')
        self.git(self.upstream, 'add', 'file'); self.git(self.upstream, 'commit', '-qm', 'one')
        subprocess.run(['git', 'clone', '-q', str(self.upstream), str(self.clone)], check=True)
        self.git(self.clone, 'config', 'user.email', 'test@example.com')
        self.git(self.clone, 'config', 'user.name', 'Test')
        self.source = patch.object(host_update, 'SOURCE', self.clone); self.source.start()

    def tearDown(self):
        self.source.stop()
        self.directory.cleanup()

    def advance(self, where, content):
        (where / 'file').write_text(content)
        self.git(where, 'commit', '-qam', content)

    def test_fast_forwards_to_upstream(self):
        self.assertFalse(host_update.pull())
        self.advance(self.upstream, 'two')
        self.assertTrue(host_update.pull())
        self.assertEqual((self.clone / 'file').read_text(), 'two')

    def test_refuses_local_changes_and_diverged_history(self):
        (self.clone / 'file').write_text('edited')
        with self.assertRaisesRegex(ValueError, 'local changes'):
            host_update.pull()
        self.git(self.clone, 'checkout', '-q', 'file')
        self.advance(self.clone, 'local')
        self.advance(self.upstream, 'remote')
        with self.assertRaisesRegex(ValueError, 'diverged'):
            host_update.pull()
        self.assertEqual((self.clone / 'file').read_text(), 'local')


class SetupCommandTests(unittest.TestCase):
    def test_update_reruns_itself_from_the_pulled_source(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(host_setup, 'private_root', return_value=Path(directory)), \
                patch.object(host_update, 'pull', return_value=True), \
                patch.object(host_setup.os, 'execv', side_effect=SystemExit(0)) as execv, \
                patch('sys.argv', ['forge', 'update', '--bind', '100.64.0.7', '--yes']):
            with self.assertRaises(SystemExit):
                host_setup.main()
        argv = execv.call_args.args[1]
        self.assertEqual(argv[1:4], ['-m', 'forge.host_setup', 'update'])
        self.assertIn('--no-pull', argv)
        self.assertEqual(argv[argv.index('--bind') + 1], '100.64.0.7')
        self.assertIn('--yes', argv)

    def test_update_requires_an_installed_host(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'run bin/forge setup first'):
                host_setup.update(Path(directory))

    def test_update_verifies_connected_accounts_when_the_runner_changed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); (root / 'host-runtime.json').write_text('{}')
            with patch.object(host_setup, 'preflight'), \
                    patch.object(host_update, 'reconcile', return_value=True), \
                    patch.object(host_setup, 'Onboarding') as onboarding, \
                    patch.object(host_setup, 'check') as check, patch('sys.stdout', io.StringIO()):
                onboarding.return_value.view.return_value = {'provider_connected': True}
                host_setup.update(root)
                check.assert_called_once_with(root)
                check.reset_mock()
                host_update.reconcile.return_value = False
                host_setup.update(root)
                check.assert_not_called()


if __name__ == '__main__':
    unittest.main()
