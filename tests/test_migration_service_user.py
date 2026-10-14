"""Explicit maintenance account carries the upgrade RPC and its resume commands."""
import json
from pathlib import Path
import shlex
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge_client import _service_host, migration
from forge_client.cli import execute, migration_commands, parser
from forge_client.contracts import SetupError


class MigrationAccountTransportTests(unittest.TestCase):
    def args(self, action='upgrade', user='forge'):
        values = ['migrate', action, '--ssh', 'anvil', '--source', '/home/forge/forge',
                  '--root', '/home/forge/.local/share/forge']
        if user is not None:
            values.append('--service-user=' + user)
        return parser().parse_args(values)

    def test_default_transport_keeps_the_ssh_account(self):
        self.assertEqual(migration.transport(self.args(user=None), ['python3', '-c', 'print(1)']),
                         migration.ssh('anvil', "python3 -c 'print(1)'"))

    def test_rpc_switches_account_before_the_private_bootstrap(self):
        reply = dict(ok=True, status='draining', next_command='forge migrate upgrade')
        with patch.object(migration, 'bundle', return_value=b'code'), \
                patch.object(migration.subprocess, 'run', return_value=SimpleNamespace(
                    returncode=0, stdout=json.dumps(reply).encode())) as run:
            result = migration.call(self.args(), 'upgrade', apply=False)
        command = shlex.split(run.call_args.args[0][-1])
        self.assertEqual(command[:3], ['python3', '-I', '-c'])
        self.assertEqual(command[4:], ['forge', 'python3', '-c', migration.BOOTSTRAP])
        self.assertIn('os.execvpe', command[3])
        resumed = parser().parse_args(shlex.split(result['next_command'])[1:])
        self.assertEqual((resumed.ssh, resumed.service_user, resumed.migration_command),
                         ('anvil', 'forge', 'upgrade'))
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

    def test_invalid_account_rejected_before_remote_effects(self):
        with patch.object(migration.subprocess, 'run') as run:
            for user in ('-root', 'forge;id', '../forge', 'forge name', 'x' * 33):
                args = self.args(user=user)
                with self.assertRaises(SetupError): migration.call(args, 'upgrade')
                with self.assertRaises(SetupError) as caught: execute(args)
                self.assertEqual(caught.exception.code, 'invalid_service_user')
            run.assert_not_called()

    def test_contract_offers_the_account_for_upgrade(self):
        self.assertIn('service_user', migration_commands()['upgrade']['input_schema']['properties'])


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
