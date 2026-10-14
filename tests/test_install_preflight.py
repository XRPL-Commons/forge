"""Resource and policy preflight uses observations and performs no write operations."""
import unittest
from unittest.mock import Mock, patch

from forge_client.preflight import observe


class PreflightTests(unittest.TestCase):
    def test_resources_gate_and_each_probe_is_read_only(self):
        selected = {'name': 'Anvil', 'hostname': 'anvil', 'revision': 'a'*40, 'host_key': None,
                    'operators': [{'id': 1, 'login': 'operator'}], 'owner': {'id': 1, 'login': 'operator', 'type': 'User'},
                    'repositories': [], 'providers': [{'id': 'codex', 'kind': 'browser'}]}
        github = Mock(); github.identity.return_value = selected['operators'][0]
        host = Mock(); host.journal.read.return_value = {}
        host.execute.return_value = {'host': {'cpus': 1, 'memory_mib': 2048, 'free_disk_mib': 10000}}
        host.host.rpc.return_value = {'ok': True, 'userns_restricted': '1', 'egress': {'github.com': True},
                                     'tailnet': {'state': 'Running', 'cert_domains': []}}
        with patch('forge_client.preflight.shutil.which', return_value='/bin/tool'), patch('forge_client.preflight.client_revision', return_value='a'*40):
            result = observe(selected, host, github, run=lambda *a, **k: b'{"BackendState":"Running","Peer":{}}')
        needed = {row['id'] for row in result['items'] if row['status'] == 'needs_you'}
        self.assertEqual(needed, {'host.cpus', 'host.memory_mib', 'host.free_disk_mib', 'network.certificates'})
        self.assertEqual(host.execute.call_args.args, ('plan',))
        self.assertEqual(host.host.rpc.call_args.args[1], {'version': 1})
        github._request.assert_not_called()

    def test_full_database_tier_increases_resource_thresholds(self):
        selected = {'name': 'Anvil', 'hostname': 'anvil', 'revision': 'a'*40, 'host_key': None,
                    'operators': [{'id': 1, 'login': 'operator'}], 'owner': {'id': 1, 'login': 'operator', 'type': 'User'},
                    'repositories': [{'target': {'id': 10, 'name': 'owner/repo'}, 'allow_workflow_edits': False}], 'providers': []}
        row = {'env_tier': 'full', 'target': {'id': 10, 'admin': True}, 'signed_commits_required': True,
               'workflow_files': ['.github/workflows/ci.yml'], 'ready_issues': [10]}
        github = Mock(); github.identity.return_value = selected['operators'][0]
        host = Mock(); host.journal.read.return_value = {}
        host.execute.return_value = {'host': {'cpus': 2, 'memory_mib': 8000, 'free_disk_mib': 50000}}
        host.host.rpc.return_value = {'ok': True, 'userns_restricted': '0', 'egress': {}, 'tailnet': None}
        with patch('forge_client.preflight.repository', return_value=row), patch('forge_client.preflight.shutil.which', return_value='/bin/tool'), patch('forge_client.preflight.client_revision', return_value='a'*40):
            result = observe(selected, host, github, run=lambda *a, **k: b'{"BackendState":"Running"}')
        self.assertGreater(result['minimum_resources']['memory_mib'], 8000)
        signing = next(value for value in result['items'] if value['id'] == 'signing.10')
        self.assertIn('No signing bypass', signing['detail'])
