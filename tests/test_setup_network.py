from contextlib import nullcontext
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge_client import _network_host as remote
from forge_client.network import Network, payload
from forge_client.install import Installation
from forge_client.contracts import SetupError
from forge_client.workflow import Journal

INTENT = {'id': '1' * 32, 'name': 'Anvil', 'revision': 'a' * 40}
AUTH = 'https://login.tailscale.com/a/1234abcdef'


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        for name, value in (('STATE', self.root), ('NETWORK', self.root / 'network.json'),
                            ('facts', Mock()), ('private_directory', Mock()),
                            ('read', Mock(return_value={'intent': INTENT, 'step': 'complete'})),
                            ('locked', lambda name: nullcontext()), ('network_running', Mock(return_value=False)),
                            ('tailnet_status', Mock(return_value={'BackendState': 'NeedsLogin'})),
                            ('run', Mock(return_value='')), ('managed_network_file', Mock())):
            context = patch.object(remote, name, value)
            context.start(); self.addCleanup(context.stop)

    def request(self, action='plan', **values):
        return remote.network_dispatch({'version': 1, 'action': action, 'intent': INTENT,
                                        'hostname': 'anvil', 'upgrade_installer': False, **values}, 'payload')

    def test_plan_does_not_write_and_apply_uses_durable_job(self):
        self.assertEqual(self.request()['status'], 'network_required')
        self.assertFalse(remote.NETWORK.exists())
        self.request('apply')
        self.assertEqual(remote.network_read()['intent'], INTENT)
        self.assertIn('systemd-run', remote.run.call_args.args[0])
        self.assertEqual((self.root / 'network-worker.py').stat().st_mode & 0o777, 0o600)

    def test_running_job_and_connected_network_are_not_restarted(self):
        self.request('apply')
        remote.run.reset_mock()
        remote.network_running.return_value = True
        self.assertEqual(self.request('apply')['status'], 'network_installing')
        self.assertFalse(remote.run.called)
        remote.network_running.return_value = False
        data = remote.network_read(); data['policy_ready'] = True; remote.network_write(data)
        remote.tailnet_status.return_value = {'BackendState': 'Running', 'Self': {'DNSName': 'anvil.example.ts.net.'}}
        self.assertEqual(self.request('apply')['status'], 'network_ready')
        self.assertFalse(remote.run.called)

    def test_network_is_bound_to_installed_host_and_saved_hostname(self):
        self.request('apply')
        for value in ({'hostname': 'other'}, {'intent': {**INTENT, 'id': '2' * 32}}):
            with self.assertRaises(remote.InstallError): self.request('apply', **value)
        remote.read.return_value['step'] = 'host'
        with self.assertRaises(remote.InstallError): self.request('apply')

    def test_handoff_excludes_raw_output_qr_and_peer_data(self):
        self.request('apply')
        output = json.dumps({'AuthURL': AUTH, 'QR': 'synthetic-secret', 'BackendState': 'NeedsLogin'})
        with patch('shutil.which', return_value='/usr/bin/tailscale'), \
                patch.object(remote.subprocess, 'run', return_value=SimpleNamespace(stdout=output.encode(), returncode=1)):
            self.assertEqual(remote.network_work(), 0)
        result = self.request()
        self.assertEqual(result['status'], 'tailnet_authorization_required')
        self.assertEqual(result['url'], AUTH)
        self.assertNotIn('synthetic-secret', json.dumps(result) + remote.NETWORK.read_text())
        self.assertEqual(remote.managed_network_file.call_args.args[1], remote.SERVE_POLICY)

    def test_operator_policy_conflict_is_not_hidden_by_old_login_url(self):
        self.request('apply')
        data = remote.network_read(); data.update(url=AUTH, error='network_conflict'); remote.network_write(data)
        result = self.request()
        self.assertEqual(result['status'], 'network_interrupted')
        self.assertEqual(result['error'], 'network_conflict')
        self.assertIsNone(result['url'])

    def test_composed_payload_has_separate_safe_rpc_and_worker_entrypoints(self):
        program = payload()
        namespace = {'__name__': 'forge_rpc'}
        exec(compile(program, '<network-payload>', 'exec'), namespace)
        self.assertEqual(namespace['main'].__name__, 'network_main')
        with patch.dict(namespace, network_work=Mock(return_value=0)), patch('sys.argv', ['worker.py', '--network-worker']):
            self.assertEqual(namespace['main'](), 0)


class LocalNetworkTests(unittest.TestCase):
    def test_schema_validation_rejects_untrusted_links_and_data(self):
        valid = {'ok': True, 'status': 'tailnet_authorization_required', 'origin': None,
                 'url': AUTH, 'step': 'authorization', 'error': None}
        self.assertEqual(Network.validate(valid), valid)
        for changed in ({'url': 'https://evil.example/a/123'}, {'origin': 'http://anvil.example.ts.net'},
                        {'secret': 'should-not-appear'}, {'error': 'raw secret exception'}):
            with self.assertRaises(SetupError): Network.validate({**valid, **changed})

    def test_journal_retains_hostname_and_records_only_real_connectivity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Journal('test', 'network', root=Path(directory).resolve())
            store.save({'version': 1, 'ssh': 'anvil', 'intent': INTENT, 'milestones': {}})
            host = Mock()
            host.rpc.return_value = {'ok': True, 'status': 'network_ready', 'origin': 'https://anvil.example.ts.net',
                                     'url': None, 'step': 'complete', 'error': None}
            workflow = Installation('anvil', host=host, storage=store, clock=lambda: 100)
            result = Network(workflow).execute()
            self.assertEqual(result['hostname'], 'anvil')
            self.assertEqual(store.read()['milestones'], {'tailnet_connected': 100})
            self.assertEqual(host.rpc.call_args.args[1]['action'], 'plan')
            with self.assertRaises(SetupError): Network(workflow).execute(hostname='other')


if __name__ == '__main__':
    unittest.main()
