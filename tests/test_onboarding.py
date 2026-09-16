"""Setup state and failure boundaries, with explicit external-system fixtures."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from forge.app_access import ROLE_PERMISSIONS
from forge.credentials import Credentials
from forge.onboarding import Onboarding, ROLES
from forge.settings import Settings, write_json, digest
from forge.setup_service import Service
from forge.factory.runner import RunnerError
from forge.state import StateError


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.api = Mock()
        self.api.jwt.return_value = 'synthetic-app-jwt'
        self.setup = Onboarding(self.root, client=self.api)
        self.setup.read()
        self.browser = 'browser-session-for-tests'
        self.installations = [{'id': 101, 'app_id': 1, 'account': {'login': 'example'}}]
        def request(method, path, token, payload=None):
            if path.startswith('/app-manifests/'):
                self.assertIsNone(token)
                return {'id': 1, 'slug': 'forge-build-test', 'pem': '-----BEGIN PRIVATE KEY-----\nsynthetic test data\n-----END PRIVATE KEY-----'}
            if path == '/app':
                return {'id': 1, 'slug': 'forge-build-test', 'owner': {'login': 'example'}, 'permissions': ROLE_PERMISSIONS['build']}
            if path.startswith('/app/installations?'):
                return self.installations
            raise AssertionError(path)
        self.api.request.side_effect = request
        def discover(connection):
            role = ROLES[connection['app_id'] - 1]
            return {'repositories': [{'id': 10, 'name': 'example/project'}], 'permissions': ROLE_PERMISSIONS[role]}
        self.api.discover.side_effect = discover

    def begin(self):
        value = self.setup.begin('build', 'example', 'organization', 'http://127.0.0.1:8787', self.browser)
        return parse_qs(urlparse(value['action']).query)['state'][0]

    def connected(self):
        config = self.setup.settings().load()
        config['connections'] = {r: {'app_id': i, 'installation_id': 100+i, 'private_key_env': 'FORGE_SETUP_'+r.upper()+'_KEY'} for i,r in enumerate(ROLES, 1)}
        write_json(self.setup.settings().path, config)
        self.setup.settings().refresh()
        with self.setup.store.transaction() as data:
            data['apps'] = {r: {'connection': config['connections'][r], 'install_url': 'https://github.com/apps/fixture/installations/new'} for r in ROLES}
            data['owner'] = 'example'
            data['account_type'] = 'organization'
        self.setup.select(10)
        self.setup.provider('test-provider-value-no-real-credential')

    def service(self):
        engine = {}
        resets = []
        class Runner:
            def __init__(self, config, **kw): self.config = config
            def ready(self): return True
            def inspect(self, job): return engine.get(job['id'])
            def create(self, job):
                engine[job['id']] = {'id':job['id'], 'status':'created', 'running':False, 'exit_code':0}
                return job['id']
            def prepare(self, job, token): pass
            def start(self, job): engine[job['id']].update(status='exited' if job['kind']=='provider' else 'running', running=job['kind']!='provider')
            def capture(self, job): return {'result':{'ready':True,'attempt':job['id']}}
            def reset(self, job):
                assert job['fenced']
                resets.append(job['id']); engine.pop(job['id'], None)
        github = Mock()
        github.return_value.call.return_value = {'id':10,'default_branch':'main'}
        github.return_value.protections.return_value = True
        service = Service(self.root, runner=Runner, github=github)
        service.setup = self.setup
        return service, engine, resets

    def test_draft_does_not_change_legacy_admission(self):
        self.connected()
        self.assertFalse((self.root/'factory.json').exists())
        self.assertTrue(Settings(self.root).admission('example/project')['allowed'])
        self.assertTrue(self.setup.settings().load()['repositories'][0]['enabled'])
        self.assertFalse(self.setup.view()['can_start'])

    def test_failed_repository_verification_stops_checking_without_starting_workers(self):
        self.connected()
        service, engine, resets = self.service()
        service.github_class.return_value.protections.return_value = False
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(4): service.tick()
        view = self.setup.view()
        self.assertIsNone(view['operation'])
        self.assertEqual(view['checks']['github'], 'failed')
        self.assertIn('Protect the default branch', view['error'])
        self.assertFalse(view['can_start'])
        self.assertFalse(engine)
        self.assertFalse(resets)

    def test_manifest_role_permissions_and_no_admin_token(self):
        origin = 'http://127.0.0.1:8787'
        for role in ROLES:
            with self.subTest(role=role):
                value = self.setup.begin(role,'example','organization',origin,self.browser)
                manifest = value['manifest']
                self.assertEqual(manifest['default_permissions'],ROLE_PERMISSIONS[role])
                self.assertEqual(manifest['url'], origin)
                # Both URL fields are required by GitHub's manifest schema, even
                # though this polling integration never enables webhook delivery.
                self.assertEqual(manifest['hook_attributes']['url'], origin + '/github/events')
                self.assertFalse(manifest['hook_attributes']['active'])
                self.assertEqual(manifest['default_events'], [])
                self.assertNotIn('pem', json.dumps(value))
        self.api.request.assert_not_called()

    def test_callback_requires_cookie_nonce_and_is_single_use(self):
        nonce = self.begin()
        for state,browser in [('wrong',self.browser),(nonce,'another-browser')]:
            with self.assertRaises(ValueError): self.setup.callback(state,'code',browser)
        self.api.request.assert_not_called()
        self.setup.callback(nonce,'code',self.browser)
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        self.assertEqual(sum(c.args[0]=='POST' for c in self.api.request.call_args_list),1)
        view = json.dumps(self.setup.view())
        self.assertNotIn('PRIVATE KEY', view)
        self.assertNotIn('private_key_env', view)

    def test_expired_or_reset_callback_is_rejected(self):
        nonce = self.begin()
        with self.setup.store.transaction() as data: data['challenge']['expires'] = 0
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        nonce = self.begin()
        self.setup.queue('reset',generation=self.setup.read()['generation'])
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        self.api.request.assert_not_called()

    def test_lost_manifest_exchange_is_not_replayed(self):
        nonce = self.begin()
        self.api.request.side_effect = ValueError('Synthetic transport failure')
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        with self.assertRaises(ValueError): self.setup.callback(nonce,'code',self.browser)
        self.assertEqual(self.api.request.call_count,1)

    def test_installation_is_verified_independently(self):
        self.setup.callback(self.begin(),'code',self.browser)
        self.installations = [{'id':999,'app_id':1,'account':{'login':'other'}}]
        self.setup.refresh()
        self.assertFalse(self.setup.settings().load()['connections'])
        self.installations = [{'id':101,'app_id':1,'account':{'login':'example'}}]
        self.setup.refresh()
        self.assertEqual(self.setup.settings().load()['connections']['build']['installation_id'],101)
        self.assertEqual(self.setup.view()['roles']['build']['status'],'ok')

    def test_readiness_runs_cleanup_reuse_and_start_rechecks_github(self):
        self.connected()
        # Enabling uses a real digest in production; fixture supplies an image identity.
        config = self.setup.runtime(); config['image'] = 'sha256:'+'a'*64
        write_json(self.root/'setup/runtime.json',config)
        service,engine,resets = self.service()
        revision = self.setup.view()['revision']
        identifier = self.setup.queue('check',revision)
        self.assertEqual(identifier,self.setup.queue('check',revision))
        for _ in range(16): service.tick()
        self.assertTrue(self.setup.view()['can_start'])
        self.assertEqual(len(set(resets)),3)
        self.assertFalse(engine)
        revision = self.setup.view()['revision']
        self.setup.queue('start',revision)
        service.tick()
        self.assertEqual(self.setup.read()['phase'],'active')
        self.assertTrue((self.root/'factory.json').exists())
        self.assertTrue((self.root/'state/factory-v2.enabled').exists())
        self.assertTrue(json.loads((self.root/'runtime.json').read_text())['merge_enabled'])
        self.assertEqual(service.github_class.return_value.protections.call_count,2)
        self.assertEqual(self.setup.queue('start',revision),self.setup.read()['activation_request'])

    def test_start_rejects_stale_or_unverified_configuration(self):
        self.connected()
        revision = self.setup.view()['revision']
        with self.assertRaises(ValueError): self.setup.queue('start',revision)
        with self.setup.store.transaction() as data: data['readiness']={'revision':revision,'at':0}
        with self.assertRaises(ValueError): self.setup.queue('start',revision)
        with self.assertRaises(ValueError): self.setup.queue('check','old-revision')

    def test_reset_fences_before_cleanup_and_archives_without_credentials(self):
        self.connected()
        service,engine,resets = self.service()
        config = self.setup.runtime()
        job = service.new_probe(config,'termination')
        service.runner(config).create(job)
        with self.setup.store.transaction() as data: data['probe_jobs']=[job]
        outside = self.root/'external-login'; outside.write_text('shared')
        write_json(self.root/'factory.json',self.setup.settings().load())
        generation = self.setup.read()['generation']
        request = self.setup.queue('reset',generation=self.setup.read()['generation'])
        self.assertFalse(Settings(self.root).admission('example/project')['allowed'])
        self.assertTrue((self.root/'factory.json').exists())
        self.assertTrue(engine)
        for _ in range(3): service.tick()
        data = self.setup.read()
        self.assertEqual(data['phase'],'setup')
        self.assertNotEqual(data['generation'],generation)
        self.assertFalse(engine)
        self.assertFalse((self.root/'factory.json').exists())
        self.assertFalse((self.root/'credentials').exists())
        self.assertTrue(outside.exists())
        archive=self.root/'archives'/('reset-'+request)
        self.assertTrue((archive/'receipt.json').exists())
        self.assertNotIn('test-provider-value', ''.join(p.read_text() for p in archive.rglob('*') if p.is_file()))
        self.assertFalse(Settings(self.root).admission('example/project')['allowed'])

    def test_unverified_teardown_keeps_reset_pending_and_config_intact(self):
        self.connected()
        service,engine,_ = self.service()
        job = service.new_probe(self.setup.runtime(),'termination')
        with self.setup.store.transaction() as data: data['probe_jobs']=[job]
        self.setup.queue('reset',generation=self.setup.read()['generation']); service.tick()
        with patch.object(service,'runner') as runner:
            runner.return_value.reset.side_effect=RunnerError('Engine unavailable')
            service.tick()
        self.assertEqual(self.setup.read()['phase'],'resetting')
        self.assertTrue(self.setup.settings().path.exists())
        self.assertTrue((self.root/'credentials').exists())
        with self.assertRaises(ValueError): self.setup.provider('another-value-no-real-secret')
        service.tick(); service.tick()
        self.assertEqual(self.setup.read()['phase'],'setup')

    def test_old_reset_cannot_change_new_generation(self):
        service,_,_ = self.service()
        self.setup.queue('reset',generation=self.setup.read()['generation']); old_state=self.setup.read(); old=old_state['operation']
        for _ in range(3): service.tick()
        with self.assertRaises(StateError): service.update(old,phase='resetting')
        with self.assertRaises(StateError): service.reset(old_state)
        self.assertEqual(self.setup.queue('reset',generation=old['generation']),old['id'])
        self.assertEqual(self.setup.read()['phase'],'setup')
        with self.assertRaises(ValueError): self.setup.queue('reset',generation='unknown-old-generation')

    def test_restart_after_reset_receipt_clears_only_old_fence(self):
        service,_,_=self.service()
        old=self.setup.read()['generation']
        self.setup.queue('reset',generation=self.setup.read()['generation'])
        for _ in range(3): service.tick()
        write_json(self.root/'state/factory-paused.json',{'generation':old})
        service.tick()
        self.assertFalse((self.root/'state/factory-paused.json').exists())
        write_json(self.root/'state/factory-paused.json',{'generation':self.setup.read()['generation']})
        service.tick()
        self.assertTrue((self.root/'state/factory-paused.json').exists())

    def test_reset_read_does_not_recreate_archived_setup(self):
        import shutil
        self.connected(); self.setup.queue('reset',generation=self.setup.read()['generation'])
        shutil.rmtree(self.root/'setup')
        self.assertEqual(self.setup.view()['phase'],'resetting')
        self.assertFalse((self.root/'setup').exists())

    def test_secret_files_private_and_external_login_preserved(self):
        store=Credentials(self.root)
        store.put('OPENAI_API_KEY','synthetic-provider-credential')
        self.assertEqual(store.environment()['OPENAI_API_KEY'],'synthetic-provider-credential')
        files=list(store.root.iterdir())
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in files))
        index=json.loads((store.root/'index.json').read_text())
        path=store.root/index['OPENAI_API_KEY']['name']
        path.chmod(0o644)
        with self.assertRaises(ValueError): store.environment()


if __name__=='__main__': unittest.main()
