"""Device sign-in must satisfy the harness sandbox before asking for approval."""
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from forge.host_setup import login, login_sandbox
from forge.harnesses.registry import manifest, adapter
from forge.onboarding import Onboarding
from tests.sqlite_fixture import selected_store


class LoginSandboxTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        selected_store(self.root)
        self.setup = Onboarding(self.root)
        self.config = {**self.setup.runtime(), 'image': 'sha256:' + 'a' * 64,
                       'apparmor_profile': 'forge-runner-private-proc'}
        self.setup.save_runtime(self.config, active=False)
        (self.root / 'runner-seccomp.json').write_text('{}')

    def test_grok_checks_the_image_then_uses_the_same_isolation_for_sign_in(self):
        calls = []
        def command(args, **kwargs):
            calls.append(args)
            if args[1] == 'create':
                mount = args[args.index('--mount') + 1]
                home = Path(mount.split('source=', 1)[1].split(',', 1)[0])
                (home / '.grok').mkdir()
                (home / '.grok/auth.json').write_text('{"synthetic":"device-login"}')
                return Mock(stdout='login-container')
            return Mock(returncode=0)
        with patch('forge.host_setup.command', side_effect=command), \
                patch('forge.host_setup.subprocess.run', return_value=Mock(returncode=1)), \
                patch('sys.stdout', new=io.StringIO()):
            login(self.root, 'grok')
        self.assertEqual([c[1] for c in calls], ['run', 'create', 'start'])
        probe, create, _ = calls
        self.assertEqual(probe[probe.index('--network') + 1], 'none')
        self.assertNotIn('--mount', probe)
        for args in (probe, create):
            self.assertIn('apparmor=forge-runner-private-proc', args)
            self.assertIn('seccomp=' + str(self.root / 'runner-seccomp.json'), args)
            self.assertIn('systempaths=unconfined', args)
            self.assertIn('no-new-privileges', args)
            self.assertEqual(args[args.index('--cap-drop') + 1], 'ALL')
            self.assertIn('--read-only', args)
            self.assertNotIn('--privileged', args)
            self.assertNotIn('seccomp=unconfined', args)
            self.assertIn(self.config['image'], args)
            path = next(value[5:] for value in args if value.startswith('PATH='))
            self.assertEqual(path, adapter('grok')['harness_environment']({})['PATH'])
        self.assertEqual(create[-3:], [self.config['image'], 'login', '--device-auth'])
        self.assertEqual(self.setup.runtime()['provider_pool'][0]['provider'], 'grok')

    def test_missing_policies_stop_before_any_container_or_sign_in(self):
        for missing in ('seccomp', 'apparmor'):
            with self.subTest(missing=missing):
                config = self.config.copy()
                if missing == 'seccomp':
                    (self.root / 'runner-seccomp.json').unlink()
                else:
                    (self.root / 'runner-seccomp.json').write_text('{}')
                    config['apparmor_profile'] = None
                with patch('forge.host_setup.command') as command:
                    with self.assertRaisesRegex(ValueError, 'update --root'):
                        login_sandbox(self.root, manifest('grok'), config)
                    command.assert_not_called()

    def test_missing_image_tool_or_denied_namespace_stops_before_authorization(self):
        for code, message in ((10, 'missing a required sign-in tool'), (11, 'could not start the sign-in sandbox')):
            with self.subTest(code=code), patch('forge.host_setup.command',
                    side_effect=subprocess.CalledProcessError(code, ['docker', 'run'])) as command, \
                    patch('sys.stdout', new=io.StringIO()):
                with self.assertRaisesRegex(ValueError, message) as error:
                    login(self.root, 'grok')
                self.assertIn('Browser sign-in has not started', str(error.exception))
                self.assertIn('update --root ' + str(self.root), str(error.exception))
                self.assertEqual(command.call_count, 1)
                self.assertFalse((self.root / 'state/harness-login-enabled.json').exists())
                self.assertFalse((self.root / 'credentials/pending-login-grok.json').exists())

    def test_docker_timeout_is_actionable_and_other_logins_keep_their_policy(self):
        with patch('forge.host_setup.command', side_effect=subprocess.TimeoutExpired(['docker'], 30)), \
                patch('sys.stdout', new=io.StringIO()):
            with self.assertRaisesRegex(ValueError, 'Browser sign-in has not started'):
                login_sandbox(self.root, manifest('grok'), self.config)
        with patch('forge.host_setup.command') as command:
            for provider in ('codex', 'claude'):
                self.assertEqual(login_sandbox(self.root, manifest(provider), {}), [])
            command.assert_not_called()
