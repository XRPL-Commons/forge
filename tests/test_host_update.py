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
        if argv[:2] == ['docker', 'version']:
            result.stdout = '29.5.3\n'
        elif argv[:2] == ['git', '-C'] and argv[3:] == ['rev-parse', 'HEAD']:
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

    def test_import_maintenance_blocks_reconcile_before_any_host_action(self):
        self.host.root = self.host.root.resolve()
        (self.host.root / '.migration-cutover.json').write_text(json.dumps({'stage':'importing'}))
        with self.assertRaisesRegex(ValueError, 'cutover_maintenance_in_progress'):
            self.host.reconcile()
        self.assertEqual(self.host.calls, [])

    def test_update_maintenance_blocks_source_pull_before_reconciliation(self):
        root = self.host.root.resolve()
        (root / '.migration-cutover.json').write_text(json.dumps({'stage':'importing'}))
        with patch('sys.argv', ['forge','update','--root',str(root)]), \
                patch.object(host_update, 'pull') as pull, \
                patch.object(host_setup, 'update') as update, \
                patch('sys.stderr', io.StringIO()):
            self.assertEqual(host_setup.main(), 1)
        pull.assert_not_called()
        update.assert_not_called()

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
        self.assertTrue((self.host.root / 'ui/current/docs/assets/setup-hero.svg').exists())
        self.assertIn('"FORGE_ROOT=/app"', ui)
        self.assertEqual(self.host.restarted(), [['forge-mirror.service'], ['forge-status.service', 'forge-ui.service', 'forge-supervisor.service']])
        self.host.qualify.assert_called_once()
        runtime = json.loads((self.host.root / 'host-runtime.json').read_text())
        self.assertEqual(runtime['image'], 'sha256:' + '1' * 64)
        self.assertEqual(runtime['apparmor_profile'], 'forge-runner-private-proc')
        self.assertEqual(json.loads((self.host.root / 'host.json').read_text())['source'], 'a' * 40)

    def test_mirror_unit_runs_the_qualified_launcher_before_supervisor(self):
        self.host.reconcile()
        mirror = self.host.unit('forge-mirror')
        self.assertIn('forge.launch forge.mirror_daemon --root', mirror)
        supervisor = self.host.unit('forge-supervisor')
        self.assertIn('Requires=forge-mirror.service', supervisor)
        self.assertIn('After=network-online.target forge-mirror.service', supervisor)
        health = next(i for i, call in enumerate(self.host.calls) if 'forge.mirror_daemon' in call and '--check' in call)
        start = next(i for i, call in enumerate(self.host.calls) if call[:3] == ['systemctl', '--user', 'restart'] and 'forge-supervisor.service' in call)
        self.assertLess(health, start)

    def test_unsupported_engine_refuses_before_build_or_host_changes(self):
        original = self.host.run
        def old_engine(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ['docker', 'version']: result.stdout = '23.0.5'
            return result
        self.host.run = old_engine
        with self.assertRaisesRegex(RuntimeError, 'Docker Engine 28'):
            self.host.reconcile()
        self.assertFalse(self.host.builds)
        self.assertFalse((self.host.home / '.config/systemd/user').exists())

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

    def failing(self, prefix):
        run = self.host.run
        def fail(argv, check=False, **kwargs):
            if argv[:len(prefix)] == prefix:
                self.host.calls.append(argv)
                raise subprocess.CalledProcessError(1, argv)
            return run(argv, check=check, **kwargs)
        self.host.run = fail

    def test_without_sudo_setup_prints_the_administrator_apparmor_commands(self):
        self.failing(['sudo'])
        with self.assertRaises(ValueError) as caught:
            self.host.reconcile()
        message = str(caught.exception)
        self.assertIn('sudo install -m 0644 ' + str(host_update.SOURCE / 'deploy/runner/forge-runner-private-proc.apparmor')
                      + ' ' + str(self.host.apparmor), message)
        self.assertIn('sudo apparmor_parser -r ' + str(self.host.apparmor), message)
        self.assertFalse((self.host.home / '.config/systemd/user/forge-ui.service').exists())
        self.host.qualify.assert_not_called()

    def test_a_profile_an_administrator_installed_needs_no_sudo_and_later_updates_normally(self):
        wanted = (host_update.SOURCE / 'deploy/runner/forge-runner-private-proc.apparmor').read_bytes()
        self.host.apparmor.write_bytes(wanted)
        self.host.reconcile()
        self.assertFalse(any(argv[0] == 'sudo' for argv in self.host.calls))
        self.assertTrue(host_update.Record(self.host.root).owns(self.host.apparmor, wanted))

    def test_the_egress_proxy_runs_as_the_service_account_and_must_become_healthy(self):
        seen = []
        run = self.host.run
        def record(argv, check=False, **kwargs):
            if argv[:2] == ['docker', 'compose']:
                seen.append((argv, kwargs.get('env', {})))
            return run(argv, check=check, **kwargs)
        self.host.run = record
        self.host.reconcile()
        argv, env = seen[0]
        self.assertEqual(argv[argv.index('up'):], ['up', '-d', '--wait', '--wait-timeout', '60', 'egress'])
        self.assertEqual(env['FORGE_EGRESS_USER'], f'{host_update.os.getuid()}:{host_update.os.getgid()}')
        compose = (host_update.SOURCE / 'deploy/runner/compose.yaml').read_text()
        self.assertIn('user: "${FORGE_EGRESS_USER:-1000:1000}"', compose)
        self.assertIn('healthcheck:', compose)
        self.assertIn('name: forge-uplink', compose)

    def test_an_unhealthy_egress_proxy_fails_setup_before_services_are_installed(self):
        self.failing(['docker', 'compose'])
        with self.assertRaisesRegex(ValueError, 'egress proxy did not become healthy'):
            self.host.reconcile()
        self.assertFalse((self.host.home / '.config/systemd/user/forge-ui.service').exists())
        self.assertFalse((self.host.root / 'host-runtime.json').exists())

    def test_a_source_change_restarts_the_python_services_only(self):
        self.host.reconcile()
        self.host.revision = 'b' * 40
        changed, _ = self.host.reconcile()
        self.assertFalse(changed)
        self.assertEqual(self.host.restarted(), [['forge-mirror.service'], ['forge-status.service', 'forge-supervisor.service']])
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

    def test_full_environment_is_opt_in_and_only_switches_after_service_qualification(self):
        self.host.reconcile()
        with ExitStack() as stack:
            for patcher in self.host.patches():
                stack.enter_context(patcher)
            self.host.qualify.side_effect = RuntimeError('MongoDB round trip failed')
            with self.assertRaisesRegex(RuntimeError, 'MongoDB'):
                host_update.install_full_environment(self.host.root)
            runtime = json.loads((self.host.root / 'host-runtime.json').read_text())
            self.assertEqual(runtime.get('environment_tiers', {}), {})
            self.host.qualify.side_effect = None
            host_update.install_full_environment(self.host.root)
        full = json.loads((self.host.root / 'host-runtime.json').read_text())['environment_tiers']['full']
        self.assertEqual(full['image'], 'sha256:' + '2' * 64)
        self.assertEqual(self.host.qualify.call_args.kwargs, {'full': True})
        self.assertEqual(json.loads((self.host.root / 'host.json').read_text())['full_image'], full['image'])

    def test_update_rebuilds_and_qualifies_the_opted_in_full_tier(self):
        self.host.reconcile()
        with ExitStack() as stack:
            for patcher in self.host.patches():
                stack.enter_context(patcher)
            host_update.install_full_environment(self.host.root)
        self.host.qualify.reset_mock()
        with patch.object(host_update, 'RUNNER_INPUTS', host_update.RUNNER_INPUTS + ('README.md',)):
            changed, _ = self.host.reconcile()
        self.assertTrue(changed)
        self.assertEqual(self.host.qualify.call_count, 2)
        self.assertTrue(self.host.qualify.call_args.kwargs['full'])
        runtime = json.loads((self.host.root / 'host-runtime.json').read_text())
        self.assertEqual(runtime['environment_tiers']['full']['image'], 'sha256:' + '4' * 64)

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
        self.assertEqual(argv[1:5], ['-m', 'forge.launch', 'forge.host_setup', 'update'])
        self.assertIn('--no-pull', argv)
        self.assertEqual(argv[argv.index('--bind') + 1], '100.64.0.7')
        self.assertIn('--yes', argv)

    def test_setup_finds_the_lingering_user_manager_after_sudo_i(self):
        seen = []
        with tempfile.TemporaryDirectory() as directory, patch.dict(host_setup.os.environ, {}, clear=True), \
                patch.object(host_setup, 'Path', side_effect=lambda *parts: Path(directory) if parts == ('/run/user',) else Path(*parts)), \
                patch.object(host_setup, 'command', side_effect=lambda argv, **kw: seen.append(host_setup.os.environ.get('XDG_RUNTIME_DIR'))):
            (Path(directory) / str(host_setup.os.getuid())).mkdir()
            host_setup.user_manager()
            self.assertEqual(seen, [str(Path(directory) / str(host_setup.os.getuid()))])

    def test_setup_explains_a_missing_user_manager(self):
        failure = subprocess.CalledProcessError(1, ['systemctl'])
        with patch.dict(host_setup.os.environ, {'XDG_RUNTIME_DIR': '/nonexistent'}), \
                patch.object(host_setup, 'command', side_effect=failure), \
                patch.object(host_setup.getpass, 'getuser', return_value='forge'):
            with self.assertRaisesRegex(ValueError, r'sudo loginctl enable-linger forge'):
                host_setup.user_manager()

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
