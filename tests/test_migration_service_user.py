"""Explicit maintenance account is shared by RPCs and private snapshot reads."""
import io
import json
from pathlib import Path
import shlex
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge_client import _service_host, migration
from forge_client.cli import execute, migration_commands, parser
from forge_client.contracts import SetupError


class MigrationAccountTransportTests(unittest.TestCase):
    def args(self, action='plan', user='forge'):
        values = ['migrate', action, '--ssh', 'anvil', '--source', '/home/forge/forge',
                  '--root', '/home/forge/.local/share/forge']
        if user is not None:
            values.append('--service-user=' + user)
        return parser().parse_args(values)

    def test_default_transport_keeps_the_ssh_account(self):
        self.assertEqual(migration.transport(self.args(user=None), ['python3', '-c', 'print(1)']),
                         migration.ssh('anvil', "python3 -c 'print(1)'"))

    def test_rpc_switches_account_before_the_private_bootstrap(self):
        reply = dict(ok=True, status='draining', next_command='forge migrate resume')
        with patch.object(migration, 'bundle', return_value=b'code'), \
                patch.object(migration.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(reply).encode())) as run:
            result = migration.call(self.args('activate'), 'activate', apply=True)
        command = shlex.split(run.call_args.args[0][-1])
        self.assertEqual(command[:3], ['python3', '-I', '-c'])
        self.assertEqual(command[4:], ['forge', 'python3', '-c', migration.BOOTSTRAP])
        self.assertIn('os.execvpe', command[3])
        resumed = parser().parse_args(shlex.split(result['next_command'])[1:])
        self.assertEqual((resumed.ssh, resumed.service_user, resumed.migration_command),
                         ('anvil', 'forge', 'resume'))
        self.assertNotIn('service_user', json.loads(run.call_args.kwargs['input']))

    def test_upgrade_and_failure_commands_retain_explicit_account(self):
        args = self.args('upgrade'); args.source_revision = 'a' * 40
        for returncode, reply in [(0, dict(ok=True, next_command='forge migrate upgrade')),
                                  (0, dict(ok=False, error='test_failure', stage='draining')),
                                  (1, {})]:
            with self.subTest(returncode=returncode, reply=reply), \
                    patch.object(migration, 'bundle', return_value=b'code'), \
                    patch.object(migration.subprocess, 'run', return_value=SimpleNamespace(
                        returncode=returncode, stdout=json.dumps(reply).encode())):
                if reply.get('ok'):
                    command = migration.call(args, 'upgrade')['next_command']
                else:
                    with self.assertRaises(SetupError) as caught:
                        migration.call(args, 'upgrade')
                    command = str(caught.exception)
                self.assertIn('--service-user forge', command)
                self.assertIn('--apply --source-revision ' + 'a' * 40, command)

    def test_only_explicit_prepare_installs_dependencies_and_its_error_retries_prepare(self):
        reply = dict(ok=False, error='sqlite_build_dependencies_unavailable',
                     exception_type='MigrationError', stage='before_journal')
        for action in ('plan', 'prepare', 'resume', 'activate', 'upgrade'):
            with self.subTest(action=action), patch.object(migration, 'bundle', return_value=b'code'), \
                    patch.object(migration.subprocess, 'run', return_value=SimpleNamespace(
                        returncode=0, stdout=json.dumps(reply).encode())) as run:
                with self.assertRaises(SetupError) as caught:
                    migration.call(self.args(action), action)
                argv = shlex.split(run.call_args.args[0][-1])
                self.assertEqual('--prepare-sqlite' in argv, action == 'prepare')
                if action == 'prepare':
                    command = str(caught.exception).split('Retry preparation with: ', 1)[1]
                    retry = parser().parse_args(shlex.split(command)[1:])
                    self.assertEqual((retry.migration_command, retry.service_user), ('prepare', 'forge'))
                    self.assertTrue(retry.apply)
                    self.assertNotIn('migrate resume', str(caught.exception))
        with patch.object(migration.subprocess, 'run') as run:
            with self.assertRaises(SetupError): execute(self.args('prepare'))
            run.assert_not_called()

    def test_snapshot_download_uses_same_account_and_checks_identity(self):
        def transfer(command, *, stdout, **kwargs):
            argv = shlex.split(command[-1])
            self.assertEqual(argv[:3], ['python3', '-I', '-c'])
            self.assertEqual(argv[4:7], ['forge', 'python3', '-c'])
            self.assertEqual(argv[-1], '/private/capture')
            with tarfile.open(fileobj=stdout, mode='w') as archive:
                value = b'{}'; info = tarfile.TarInfo('manifest.json'); info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
            return SimpleNamespace(returncode=0)
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(migration.subprocess, 'run', side_effect=transfer), \
                patch('forge.store.migration.snapshot.Snapshot', return_value=SimpleNamespace(id='receipt')):
            destination = Path(directory) / 'snapshot'
            migration.download(self.args(), '/private/capture', destination, 'receipt')
            self.assertEqual(destination.stat().st_mode & 0o777, 0o700)
            self.assertEqual((destination / 'manifest.json').stat().st_mode & 0o777, 0o600)

    def test_invalid_account_rejected_before_remote_or_local_effects(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(migration.subprocess, 'run') as run:
            destination = Path(directory) / 'snapshot'
            for user in ('-root', 'forge;id', '../forge', 'forge name', 'x' * 33):
                args = self.args(user=user)
                with self.assertRaises(SetupError): migration.call(args, 'plan')
                with self.assertRaises(SetupError): migration.download(args, '/private/capture', destination, 'receipt')
            run.assert_not_called()
            self.assertFalse(destination.exists())

    def test_contract_limits_account_to_remote_operations(self):
        commands = migration_commands()
        for action in ('plan', 'prepare', 'capture', 'activate', 'resume', 'upgrade'):
            self.assertIn('service_user', commands[action]['input_schema']['properties'])
        for action in ('import', 'verify'):
            self.assertNotIn('service_user', commands[action]['input_schema']['properties'])
            with self.assertRaises(SetupError) as caught:
                execute(self.args(action))
            self.assertEqual(caught.exception.code, 'invalid_service_user')


class ServiceAccountBridgeTests(unittest.TestCase):
    def test_account_environment_and_privilege_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            uid = home.stat().st_uid
            account = SimpleNamespace(pw_dir=str(home), pw_uid=uid)
            for effective_uid in (uid, 0, uid + 12345):
                with self.subTest(effective_uid=effective_uid), \
                        patch.object(_service_host.pwd, 'getpwnam', return_value=account), \
                        patch.object(_service_host.os, 'geteuid', return_value=effective_uid), \
                        patch.object(_service_host.os, 'chdir') as chdir, \
                        patch.object(_service_host.os, 'execvpe') as execute:
                    _service_host.launch('forge', ['python3', '-c', 'PRIVATE_TOOL'])
                chdir.assert_called_once_with(home if effective_uid == uid else '/')
                executable, argv, environment = execute.call_args.args
                if effective_uid == uid:
                    self.assertEqual(argv, ['python3', '-c', 'PRIVATE_TOOL'])
                    self.assertEqual(environment['HOME'], str(home))
                    self.assertEqual(environment['XDG_RUNTIME_DIR'], '/run/user/' + str(uid))
                else:
                    offset = 0 if effective_uid == 0 else 3
                    self.assertEqual(argv[:offset], [] if effective_uid == 0 else ['sudo', '-n', '--'])
                    self.assertEqual(argv[offset:offset + 6], ['runuser', '-u', 'forge', '--', 'env', '-i'])
                    self.assertEqual(argv[offset + 6], '--chdir=' + str(home))
                    self.assertIn('HOME=' + str(home), argv)
                    self.assertIn('XDG_RUNTIME_DIR=/run/user/' + str(uid), argv)
                    self.assertEqual(argv[-3:], ['python3', '-c', 'PRIVATE_TOOL'])
                self.assertEqual(executable, argv[0])
                self.assertEqual(set(environment) - {'HOME', 'XDG_RUNTIME_DIR'}, {'PATH', 'LANG'})

    def test_prepare_installs_exact_missing_dependencies_as_ssh_administrator_before_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve(); uid = home.stat().st_uid
            for administrator in (0, uid + 12345):
                events = []
                def installed(argv, **kwargs):
                    events.append(('install', argv, kwargs))
                    return SimpleNamespace(returncode=0)
                with self.subTest(administrator=administrator), \
                        patch.object(_service_host.pwd, 'getpwnam', return_value=SimpleNamespace(pw_dir=str(home), pw_uid=uid)), \
                        patch.object(_service_host.os, 'geteuid', return_value=administrator), \
                        patch.object(_service_host.shutil, 'which', return_value=None), \
                        patch.object(_service_host.subprocess, 'run', side_effect=installed), \
                        patch.object(_service_host.os, 'chdir'), \
                        patch.object(_service_host.os, 'execvpe', side_effect=lambda *args: events.append(('switch', args))):
                    _service_host.launch('forge', ['python3', '-c', 'PRIVATE_BUILD'], prepare_sqlite=True)
                self.assertEqual([event[0] for event in events], ['install', 'install', 'switch'])
                prefix = [] if administrator == 0 else ['sudo', '-n', '--']
                self.assertEqual(events[0][1], [*prefix, 'apt-get', 'update'])
                self.assertEqual(events[1][1], [*prefix, 'apt-get', 'install', '-y', '--no-install-recommends', 'build-essential'])
                self.assertEqual(events[0][2]['env']['DEBIAN_FRONTEND'], 'noninteractive')
                self.assertNotIn('HOME', events[0][2]['env'])
                self.assertEqual(events[-1][1][1][-3:], ['python3', '-c', 'PRIVATE_BUILD'])

    def test_present_dependencies_and_read_only_launch_do_not_install(self):
        with patch.object(_service_host.shutil, 'which', return_value='/usr/bin/tool'), \
                patch.object(_service_host.subprocess, 'run') as run:
            _service_host.prepare_dependencies()
            run.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            with patch.object(_service_host.pwd, 'getpwnam', return_value=SimpleNamespace(pw_dir=str(home), pw_uid=home.stat().st_uid)), \
                    patch.object(_service_host.os, 'chdir'), patch.object(_service_host.os, 'execvpe'), \
                    patch.object(_service_host, 'prepare_dependencies') as install:
                _service_host.launch('forge', ['python3', '-c', 'READ_ONLY_PLAN'])
                install.assert_not_called()

    def test_failed_administrator_install_reports_typed_error_without_building(self):
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            with patch.object(_service_host.pwd, 'getpwnam', return_value=SimpleNamespace(pw_dir=str(home), pw_uid=home.stat().st_uid)), \
                    patch.object(_service_host.shutil, 'which', return_value=None), \
                    patch.object(_service_host.subprocess, 'run', return_value=SimpleNamespace(returncode=1)) as install, \
                    patch.object(_service_host.os, 'execvpe') as execute, \
                    patch.object(_service_host.sys, 'argv', ['bridge', 'forge', '--prepare-sqlite', 'python3']), \
                    redirect_stdout(io.StringIO()) as output:
                _service_host.main()
                execute.assert_not_called()
                self.assertEqual(install.call_count, 1)
                reply = json.loads(output.getvalue())
                self.assertEqual(reply['error'], 'sqlite_build_dependencies_unavailable')
                self.assertEqual(reply['stage'], 'before_journal')

    def test_sudo_caller_enters_private_home_only_after_switching_user(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve()
            home.chmod(0o700)
            uid = home.stat().st_uid
            def enter(path):
                if Path(path) == home:
                    raise PermissionError('SSH administrator cannot enter private service home')
            with patch.object(_service_host.pwd, 'getpwnam', return_value=SimpleNamespace(pw_dir=str(home), pw_uid=uid)), \
                    patch.object(_service_host.os, 'geteuid', return_value=uid + 12345), \
                    patch.object(_service_host.os, 'chdir', side_effect=enter), \
                    patch.object(_service_host.os, 'execvpe') as execute:
                _service_host.launch('forge', ['python3', '-c', 'PRIVATE_TOOL'])
            argv = execute.call_args.args[1]
            self.assertEqual(argv[:9], ['sudo', '-n', '--', 'runuser', '-u', 'forge', '--', 'env', '-i'])
            self.assertEqual(argv[9], '--chdir=' + str(home))

    def test_account_home_must_be_owned_and_not_a_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory).resolve(); link = home / 'link'; link.symlink_to(home)
            for path, uid in [(home, home.stat().st_uid + 1), (link, home.stat().st_uid)]:
                with self.subTest(path=path, uid=uid), \
                        patch.object(_service_host.pwd, 'getpwnam', return_value=SimpleNamespace(pw_dir=str(path), pw_uid=uid)), \
                        patch.object(_service_host.os, 'execvpe') as execute:
                    with self.assertRaises(ValueError): _service_host.launch('forge', ['python3'])
                    execute.assert_not_called()

    def test_missing_account_and_invalid_names_never_execute(self):
        with patch.object(_service_host.pwd, 'getpwnam', side_effect=KeyError), \
                patch.object(_service_host.os, 'execvpe') as execute:
            with self.assertRaises(KeyError): _service_host.launch('forge', ['python3'])
            with self.assertRaises(ValueError): _service_host.launch('forge;id', ['python3'])
            execute.assert_not_called()
