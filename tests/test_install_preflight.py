"""Resource and policy preflight uses observations and performs no write operations."""
import io
import json
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

    def hostname_finding(self, peers, remote, *, origin=None):
        selected = {'name': 'Anvil', 'hostname': 'anvil', 'revision': 'a'*40, 'host_key': 'pinned-key',
                    'operators': [{'id': 1, 'login': 'operator'}],
                    'owner': {'id': 1, 'login': 'operator', 'type': 'User'},
                    'repositories': [], 'providers': []}
        github = Mock(); github.identity.return_value = selected['operators'][0]
        host = Mock(); host.journal.read.return_value = {'origin': origin} if origin else {}
        verified = []
        def plan(action, **kwargs):
            self.assertEqual(action, 'plan')
            self.assertEqual(kwargs['fingerprint'], 'pinned-key')
            verified.append(True)
            return {'host': {'cpus': 4, 'memory_mib': 16000, 'free_disk_mib': 50000}}
        def observation(*args, **kwargs):
            self.assertTrue(verified, 'Host evidence must follow SSH identity verification')
            return {'ok': True, 'userns_restricted': '0', 'egress': {}, 'tailnet': remote}
        host.execute.side_effect = plan
        host.host.rpc.side_effect = observation
        with patch('forge_client.preflight.shutil.which', return_value='/bin/tool'), patch('forge_client.preflight.client_revision', return_value='a'*40):
            result = observe(selected, host, github, run=lambda *a, **k: json.dumps(
                {'BackendState': 'Running', 'Peer': {str(i): row for i, row in enumerate(peers)}}))
        return next(row for row in result['items'] if row['id'] == 'network.hostname')

    def test_reset_without_journal_recognizes_same_verified_tailnet_node(self):
        peer = {'ID': 'node-anvil', 'HostName': 'anvil', 'DNSName': 'anvil.example.ts.net.'}
        remote = {'state': 'Running', 'node_id': 'node-anvil', 'dns_name': 'anvil.example.ts.net',
                  'cert_domains': ['anvil.example.ts.net']}
        self.assertEqual(self.hostname_finding([peer], remote)['status'], 'pass')
        self.assertEqual(self.hostname_finding([], None)['status'], 'pass')

    def test_hostname_collision_requires_exact_live_host_evidence(self):
        peer = {'ID': 'node-anvil', 'HostName': 'anvil', 'DNSName': 'anvil.example.ts.net.'}
        remote = {'state': 'Running', 'node_id': 'node-anvil', 'dns_name': 'anvil.example.ts.net.',
                  'cert_domains': ['anvil.example.ts.net']}
        cases = [None, {**remote, 'state': 'Stopped'}, {**remote, 'node_id': 'other-node'},
                 {**remote, 'dns_name': 'other.example.ts.net.'}, {**remote, 'node_id': None},
                 {**remote, 'dns_name': None}]
        for evidence in cases:
            with self.subTest(remote=evidence):
                self.assertEqual(self.hostname_finding([peer], evidence,
                    origin='https://anvil.example.ts.net')['status'], 'needs_you')
        # One matching node cannot hide another node with the requested hostname.
        other = {**peer, 'ID': 'other-node', 'DNSName': 'anvil-1.example.ts.net.'}
        self.assertEqual(self.hostname_finding([peer, other], remote)['status'], 'needs_you')

    def test_host_probe_reports_public_node_identity_only(self):
        from forge_client import _preflight_host
        output = io.StringIO()
        status = {'BackendState': 'Running', 'CertDomains': ['anvil.example.ts.net'],
                  'Self': {'ID': 'node-anvil', 'DNSName': 'anvil.example.ts.net.',
                           'PublicKey': 'unneeded-key'}, 'User': {'private': 'unneeded'}}
        with patch.object(_preflight_host.sys, 'stdin', io.StringIO('{"version":1}')), \
             patch.object(_preflight_host.sys, 'stdout', output), \
             patch.object(_preflight_host.Path, 'exists', return_value=False), \
             patch.object(_preflight_host.Path, 'is_file', return_value=True), \
             patch.object(_preflight_host, 'reachable', return_value=True), \
             patch.object(_preflight_host.subprocess, 'run', return_value=Mock(returncode=0, stdout=json.dumps(status).encode())):
            self.assertEqual(_preflight_host.main(None), 0)
        self.assertEqual(json.loads(output.getvalue())['tailnet'],
                         {'state': 'Running', 'cert_domains': ['anvil.example.ts.net'],
                          'node_id': 'node-anvil', 'dns_name': 'anvil.example.ts.net.'})
