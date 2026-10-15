from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge import host_update
from forge.install_environment import Environment, KEY
from forge.onboarding import Onboarding
from forge_client.contracts import SetupError
from forge_client.install_environment import Environment as Client
from forge_client import _environment_host
from tests.sqlite_fixture import selected_store

BASE = 'sha256:' + 'a' * 64
FULL = 'sha256:' + 'b' * 64
ID = 'c' * 32


class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        selected_store(self.root)
        self.setup = Onboarding(self.root)
        self.setup.read()
        (self.root / 'runner-seccomp.json').write_text('{}')
        host_update.Record(self.root).save(image=BASE)
        for domain in ('host-runtime.json', 'setup/runtime.json'):
            host_update.save_runtime_document(self.root, domain, {'image': BASE}, expected_revision=0)
        self.environment = Environment(self.root, ID)
        self.present = {BASE, FULL}
        self.active = False
        self.calls = []
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(host_update, 'APPARMOR_TARGET', self.root / 'runner-seccomp.json'))
        self.stack.enter_context(patch.object(host_update, 'source_revision', return_value='d' * 40))
        self.stack.enter_context(patch('forge.install_environment.subprocess.run', side_effect=self.command))
        self.build = self.stack.enter_context(patch.object(host_update, 'full_runner_image', return_value=FULL))
        self.qualify = self.stack.enter_context(patch.object(host_update, 'qualify_runner'))

    def command(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[:2] == ['docker', 'version']:
            return SimpleNamespace(returncode=0, stdout='29.5.3\n')
        if argv[:3] == ['docker', 'image', 'inspect']:
            return SimpleNamespace(returncode=0 if argv[3] in self.present else 1, stdout=argv[3] + '\n')
        if argv[:3] == ['systemctl', '--user', 'is-active']:
            return SimpleNamespace(returncode=0, stdout='active' if self.active else 'inactive')
        if argv[0] == 'systemd-run':
            self.active = True
            return SimpleNamespace(returncode=0, stdout='')
        self.fail('Unexpected command: ' + repr(argv))

    def complete(self):
        self.environment.apply()
        receipt = host_update.Record(self.root).data[KEY]
        self.environment.worker(receipt['binding'])
        self.active = False

    def test_observation_does_not_build_switch_or_start(self):
        before = host_update.Record(self.root).data
        self.assertEqual(self.environment.view()['status'], 'environment_required')
        self.build.assert_not_called()
        self.qualify.assert_not_called()
        self.assertEqual(host_update.Record(self.root).data, before)
        self.assertFalse(any(row[0] == 'systemd-run' for row in self.calls))

    def test_complete_requires_real_canary_and_records_exact_evidence(self):
        self.complete()
        observed = self.environment.view()
        self.assertEqual(observed['status'], 'environment_ready')
        self.assertEqual(observed['image'], FULL)
        self.assertEqual(observed['services'], ['mongodb', 'postgres', 'redis'])
        self.assertEqual(observed['identity']['base_image'], BASE)
        self.assertEqual(observed['identity']['engine_version'], '29.5.3')
        self.qualify.assert_called_once_with(FULL, self.root / 'runner-seccomp.json', host_update.APPARMOR_PROFILE, full=True)
        for domain in ('host-runtime.json', 'setup/runtime.json'):
            self.assertEqual(host_update.runtime_document(self.root, domain)['environment_tiers']['full']['image'], FULL)

    def test_resume_while_running_does_not_start_another_service(self):
        self.environment.apply()
        self.assertEqual(self.environment.apply()['status'], 'environment_installing')
        self.assertEqual(sum(row[0] == 'systemd-run' for row in self.calls), 1)

    def test_failed_canary_does_not_advertise_environment(self):
        self.environment.apply()
        self.qualify.side_effect = RuntimeError('database unavailable')
        with self.assertRaisesRegex(RuntimeError, 'database unavailable'):
            self.environment.worker(host_update.Record(self.root).data[KEY]['binding'])
        self.active = False
        self.assertEqual(self.environment.view()['status'], 'environment_interrupted')
        self.assertNotIn('environment_tiers', host_update.runtime_document(self.root, 'host-runtime.json'))

    def test_resume_after_qualified_switch_interruption_reuses_proof(self):
        self.environment.apply()
        binding = host_update.Record(self.root).data[KEY]['binding']
        with patch.object(host_update, 'switch_full_environment', side_effect=RuntimeError('interrupted')):
            with self.assertRaises(RuntimeError):
                self.environment.worker(binding)
        self.active = False
        self.environment.apply()
        self.environment.worker(binding)
        self.active = False
        self.assertEqual(self.environment.view()['status'], 'environment_ready')
        self.assertEqual(self.build.call_count, 1)
        self.assertEqual(self.qualify.call_count, 1)

    def test_image_or_policy_change_invalidates_ready_receipt(self):
        self.complete()
        self.present.remove(FULL)
        self.assertEqual(self.environment.view()['status'], 'environment_interrupted')
        self.present.add(FULL)
        (self.root / 'runner-seccomp.json').write_text('{"changed": true}')
        self.assertEqual(self.environment.view()['status'], 'environment_interrupted')
        self.environment.apply()
        self.environment.worker(host_update.Record(self.root).data[KEY]['binding'])
        self.assertEqual(self.qualify.call_count, 2)

    def test_failed_requalification_does_not_reuse_an_old_success(self):
        self.complete()
        self.present.remove(FULL)
        self.environment.apply()
        binding = host_update.Record(self.root).data[KEY]['binding']
        def rebuild(base):
            self.present.add(FULL)
            return FULL
        self.build.side_effect = rebuild
        self.qualify.side_effect = RuntimeError('failed current canary')
        with self.assertRaises(RuntimeError):
            self.environment.worker(binding)
        self.assertIsNone(host_update.Record(self.root).data[KEY]['qualified_at'])
        self.qualify.side_effect = None
        self.environment.worker(binding)
        self.assertEqual(self.qualify.call_count, 3)

    def test_reset_during_canary_cannot_publish_environment(self):
        self.environment.apply()
        binding = host_update.Record(self.root).data[KEY]['binding']
        def reset(*args, **kwargs):
            with self.setup.store.transaction() as data:
                data['generation'] = 'new-generation'
        self.qualify.side_effect = reset
        with self.assertRaises(SetupError):
            self.environment.worker(binding)
        self.assertNotIn('environment_tiers', host_update.runtime_document(self.root, 'host-runtime.json'))

    def test_active_factory_cannot_start_environment_install(self):
        with self.setup.store.transaction() as data:
            data['phase'] = 'active'
        with self.assertRaises(SetupError) as raised:
            self.environment.apply()
        self.assertEqual(raised.exception.code, 'setup_complete')

    def test_ready_survives_activation_without_requalifying(self):
        self.complete()
        with self.setup.store.transaction() as data:
            data['phase'] = 'active'
        self.assertEqual(self.environment.apply()['status'], 'environment_ready')
        self.assertEqual(self.qualify.call_count, 1)

    def test_client_pins_host_and_installation_and_rejects_fake_ready(self):
        self.complete()
        report = self.environment.view()
        installation = Mock(ssh='anvil')
        installation.journal.read.return_value = {'ssh': 'anvil', 'host_key': 'verified-key', 'intent': {'id': ID}}
        installation.host.rpc.return_value = {'ok': True, 'result': report}
        self.assertEqual(Client(installation).execute()['status'], 'environment_ready')
        self.assertEqual(installation.host.fingerprint, 'verified-key')
        self.assertEqual(installation.host.rpc.call_args.args[1]['intent'], {'id': ID})
        report['receipt']['qualified_at'] = None
        with self.assertRaises(SetupError):
            Client(installation).execute()


class HostTests(unittest.TestCase):
    def test_foreign_installation_is_rejected_before_service_dispatch(self):
        intent = {'id': ID, 'name': 'Anvil', 'revision': 'd' * 40}
        with patch.object(_environment_host, 'read', return_value={'intent': {**intent, 'id': 'e' * 32}}), \
                patch.object(_environment_host, 'service') as service:
            with self.assertRaises(_environment_host.InstallError):
                _environment_host.dispatch({'version': 1, 'intent': intent, 'apply': True})
            service.assert_not_called()

    def test_environment_dispatch_runs_fixed_service_account_entrypoint(self):
        intent = {'id': ID, 'name': 'Anvil', 'revision': 'd' * 40}
        with patch.object(_environment_host, 'read', return_value={'intent': intent, 'step': 'complete'}), \
                patch.object(_environment_host, 'require_setup_entrypoint'), \
                patch.object(_environment_host, 'service', return_value='{"status":"environment_installing"}') as service:
            result = _environment_host.dispatch({'version': 1, 'intent': intent, 'apply': True})
            self.assertTrue(result['ok'])
            self.assertEqual(service.call_args.args[0], ['/usr/bin/python3', '-m', 'forge.launch',
                'forge.install_environment', '--root', '/home/forge/.local/share/forge', '--setup-id', ID, '--apply'])


if __name__ == '__main__':
    unittest.main()
