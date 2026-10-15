import io
import json
from pathlib import Path
import tarfile
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from forge.credentials import Credentials
from forge.factory.probe_runner import ProbeRunner
from forge.factory.runner import ContainerRunner, RunnerUnavailable
from forge.provider_proxy import ProxyError
from forge.provider_runtime import Service, directory, enabled, prepare, read_record, record_path, revoke
from forge.setup_service import Service as Setup
from forge.store.auxiliary import save_in
from forge.store.db import Store
from tests.test_worker_boundary import Engine


class ProviderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir='/tmp'); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store.create(self.root / 'database')
        self.credentials = Credentials(self.root)
        self.credentials.put('ANTHROPIC_API_KEY', 'host-only-fixture-secret')
        self.config = dict(provider='claude', model='claude-sonnet-4-6',
            provider_environment=['ANTHROPIC_API_KEY'], image='sha256:'+'a'*64,
            network='forge-workers', grace_seconds=2)
        self.job = Setup.new_probe(None, self.config, 'provider')
        self.config = self.job['runner_config']
        self.save()
        self.service = Service(self.root, self.root, self.store)
        self.addCleanup(self.service.close)

    def save(self):
        with self.store.write() as db:
            save_in(db, 'setup', 'operation', self.job, time.time())

    def grant(self):
        return prepare(self.root, self.root, self.store, self.job['id'], self.config)

    def runner(self):
        runner = ProbeRunner(self.config, environ={'FORGE_CREDENTIAL_ROOT':str(self.root)})
        runner.provider_context = lambda: (self.root, self.root, self.store)
        return runner

    def test_restart_reuses_capability_and_exact_account(self):
        record = self.grant()
        self.assertEqual(record, self.grant())
        self.assertEqual(self.service.step(), 1)
        server = self.service.servers[self.job['id']][0]
        self.assertEqual(server.broker.api_key(), 'host-only-fixture-secret')
        self.service.close()
        self.assertFalse(server.broker.alive())
        self.service.step()
        self.assertEqual(self.service.servers[self.job['id']][0].broker.assignment.capability, record['capability'])
        self.assertNotIn('host-only-fixture-secret', record_path(self.root, self.job['id']).read_text())

    def test_fencing_and_reconnection_withdraw_active_stream_authority(self):
        record = self.grant(); self.service.step()
        server = self.service.servers[self.job['id']][0]
        self.assertTrue(server.broker.alive())
        self.credentials.put('ANTHROPIC_API_KEY', 'replacement-fixture-secret')
        self.assertFalse(server.broker.alive())
        with self.assertRaises(ProxyError): server.broker.api_key()
        with self.assertRaisesRegex(ProxyError, 'assignment_changed'): self.grant()
        self.assertEqual(self.service.step(), 0)
        self.assertTrue(record_path(self.root, self.job['id']).exists())
        with self.assertRaisesRegex(ProxyError, 'assignment_changed'): self.grant()

    def test_fenced_and_captured_jobs_cannot_reacquire(self):
        self.grant(); self.service.step()
        server = self.service.servers[self.job['id']][0]
        self.job['fenced'] = True; self.save()
        self.assertFalse(server.broker.alive())
        self.assertEqual(self.service.step(), 0)
        with self.assertRaises(Exception): self.grant()

    def test_concurrent_cleanup_during_service_scan_is_normal_withdrawal(self):
        self.grant(); self.service.step()
        with patch('forge.provider_runtime.read_record', side_effect=FileNotFoundError):
            self.assertEqual(self.service.step(), 0)
        self.assertFalse(self.service.servers)

    def test_readiness_requires_a_live_authorized_server(self):
        from forge.provider_runtime import ready
        self.grant()
        with self.assertRaisesRegex(ProxyError, 'provider_service_unavailable'):
            ready(self.root, self.job['id'], timeout=0)
        self.service.step()
        self.assertTrue(ready(self.root, self.job['id'], timeout=0))
        self.job['fenced'] = True; self.save()
        with self.assertRaisesRegex(ProxyError, 'provider_service_unavailable'):
            ready(self.root, self.job['id'], timeout=0)

    def test_private_grant_removal_withdraws_live_authority(self):
        self.grant(); self.service.step()
        server = self.service.servers[self.job['id']][0]
        revoke(self.root, self.job['id'])
        self.assertFalse(server.broker.alive())

    def test_browser_modes_do_not_select_proxy(self):
        for config in ({**self.config, 'provider_auth_file_env':'FORGE_SETUP_CLAUDE_AUTH'},
                       {**self.config, 'provider_environment':['CLAUDE_CODE_OAUTH_TOKEN']},
                       {**self.config, 'provider':'grok'}):
            self.assertFalse(enabled(config))

    def test_account_must_be_stored_and_model_must_be_concrete(self):
        for model in ('unqualified-alias',):
            with self.assertRaisesRegex(ProxyError, 'model_unqualified'):
                prepare(self.root, self.root, self.store, self.job['id'], {**self.config,'model':model})
        with tempfile.TemporaryDirectory() as empty:
            with self.assertRaisesRegex(ProxyError, 'installation_account_required'):
                prepare(self.root, empty, self.store, self.job['id'], self.config)

    def test_pinned_aliases_preserve_observed_cli_selection(self):
        from forge.provider_runtime import model
        for selected, expected in ((None,'claude-opus-5'),('sonnet','claude-sonnet-5'),
                                   ('opus','claude-opus-5'),('haiku','claude-haiku-4-5-20251001')):
            self.assertEqual(model({**self.config,'model':selected}), expected)

    def test_runner_handoff_has_only_capability_and_retains_scrub_value(self):
        record = self.grant(); self.service.step()
        runner = self.runner(); runner.call = Mock()
        with patch.object(runner, 'inspect', return_value={'id':'owned','status':'created'}):
            runner._prepare_credentials(self.job)
        envelope = runner.call.call_args.kwargs['input']
        with tarfile.open(fileobj=io.BytesIO(envelope)) as archive:
            values = json.loads(archive.extractfile('credentials.json').read())
        self.assertEqual(values, {'ANTHROPIC_BASE_URL':'http://forge-provider:8082',
                                 'ANTHROPIC_AUTH_TOKEN':record['capability']})
        self.assertIn(record['capability'], runner._scrub_values(self.job))
        self.assertNotIn('host-only-fixture-secret', envelope.decode(errors='replace'))

    def test_unavailable_service_fails_closed_before_credential_handoff(self):
        runner = self.runner(); runner.call = Mock()
        with patch.object(runner, 'inspect', return_value={'id':'owned','status':'created'}), \
                patch('forge.provider_runtime.ready', side_effect=ProxyError('provider_service_unavailable')):
            with self.assertRaises(RunnerUnavailable): runner._prepare_credentials(self.job)
        runner.call.assert_not_called()

    def test_setup_probe_uses_same_isolated_gateway_without_git_authority(self):
        runner = self.runner(); runner.call = Engine()
        # Registry daemon is separately installed; no registry fixture is needed here.
        (self.root / 'registry/socket').mkdir(parents=True, mode=0o700)
        with patch('forge.registry_cache.socket_directory', return_value=self.root / 'registry/socket'):
            spec = runner.prepare_source(self.job)
        self.assertNotEqual(spec.network, 'forge-workers')
        gateway = runner.call.resources['container'][spec.gateway]
        self.assertIn('--provider-socket', gateway['Config']['Cmd'])
        self.assertIn('forge-provider', spec.environment['NO_PROXY'])
        keeper = runner.call.resources['container'][spec.frontend]
        self.assertNotIn('/opt/forge/mirror_frontend.py', keeper['Config']['Cmd'])
        self.assertFalse(any('/provider' in arg for arg in spec.worker_args))


if __name__ == '__main__': unittest.main()
