import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.credentials import Credentials
from forge.factory.config import validate
from forge.factory.ledger import Ledger
from forge.factory.providers import available
from forge.harnesses.registry import manifests, adapter
from forge.host_setup import login
from forge.onboarding import Onboarding
from forge.settings import write_json
from forge.state import StateError
from tests.test_factory_cycle import CycleTest
from tests.test_onboarding import SetupTests


def pool():
    return [{'id': 'codex', 'provider': 'codex', 'model': None, 'provider_environment': ['OPENAI_API_KEY'], 'provider_auth_file_env': None},
            {'id': 'claude', 'provider': 'claude', 'model': None, 'provider_environment': ['ANTHROPIC_API_KEY'], 'provider_auth_file_env': None}]


class HarnessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_installed_modules_implement_contract_and_cannot_load_repository_code(self):
        for name in manifests():
            self.assertTrue(callable(adapter(name)['run']))
            self.assertTrue(callable(adapter(name)['probe']))
        with self.assertRaises(ValueError): adapter('../../untrusted')

    def test_probe_requires_real_canary_tool_result_not_model_claim(self):
        completed = adapter('claude')['canary_completed']
        self.assertFalse(completed([{'type':'result','structured_output':{'ready':True}}], 'canary', 'nonce'))
        events = [{'type':'assistant', 'message':{'content':[{'type':'tool_use', 'id':'tool', 'name':'Bash', 'input':{'command':'canary'}}]}},
                  {'type':'user', 'message':{'content':[{'type':'tool_result','tool_use_id':'tool','content':'forge-canary:nonce'}]}}]
        self.assertTrue(completed(events, 'canary', 'nonce'))
        events[1]['message']['content'][0]['is_error'] = True
        self.assertFalse(completed(events, 'canary', 'nonce'))

    def test_credentials_refresh_cannot_overwrite_manual_reconnect(self):
        store = Credentials(self.root)
        ref = 'FORGE_SETUP_CODEX_AUTH'
        original = json.dumps({'tokens': {'synthetic': 'first'}})
        newer = json.dumps({'tokens': {'synthetic': 'new-login'}})
        store.put(ref, original, file=True)
        digest = hashlib.sha256(original.encode()).hexdigest()
        self.assertTrue(store.refresh(ref, digest, newer))
        self.assertFalse(store.refresh(ref, digest, original))
        self.assertEqual(Path(store.environment()[ref]).read_text(), newer)
        self.assertFalse(store.refresh('EXTERNAL_AUTH', digest, original))

    def test_terminal_import_preserves_source_and_connects_both_harnesses(self):
        source = self.root / 'source.json'; source.write_text('{"synthetic":"login"}')
        with patch('sys.stdout', new=io.StringIO()):
            for name in manifests(): login(self.root, name, import_login=source)
        setup = Onboarding(self.root)
        self.assertEqual({e['provider'] for e in setup.runtime()['provider_pool']}, {'codex', 'claude'})
        self.assertEqual(source.read_text(), '{"synthetic":"login"}')
        self.assertTrue(all(h['connected'] for h in setup.view()['harnesses']))
        self.assertNotIn('synthetic', json.dumps(setup.view()))

    def test_invalid_models_do_not_persist_credentials_or_partial_configuration(self):
        setup = Onboarding(self.root)
        with self.assertRaises(ValueError):
            setup.connect('claude', api_key='synthetic-secret-value', models=['--unsafe'])
        self.assertFalse((self.root / 'credentials').exists())
        self.assertFalse(setup.runtime()['provider_pool'])

    def test_refreshable_login_has_one_writer_until_cleanup(self):
        entry = {**pool()[0], 'provider_auth_file_env': 'FORGE_SETUP_CODEX_AUTH'}
        data = {'jobs': {'one': {'cleanup': False, 'runner_config': dict(entry)}}}
        self.assertFalse(available(entry, data, 1))
        self.assertTrue(available(pool()[1], data, 1))
        data['jobs']['one']['cleanup'] = True
        self.assertTrue(available(entry, data, 1))

    def test_model_rotation_is_durable_and_assignment_configuration_is_immutable(self):
        ledger = Ledger(self.root)
        entries = pool() + [{**pool()[0], 'id': 'codex-other', 'model': 'another-model'}]
        config = validate({'provider_pool': entries})
        assignments = []
        for i in range(7):
            ledger = Ledger(self.root)
            key = ledger.discover({'id': i + 1, 'name': 'example/project'+str(i)}, 1, 'req')
            job = ledger.reserve(key, 'build', 'build-'+str(i), publisher='builder', policy='p', timeout=30, runner_config=config)
            assignments.append(job['provider_id'])
            if i == 0: first = job['id']
        self.assertEqual(assignments, ['codex','claude','codex-other','codex','claude','codex-other','codex'])
        config['provider_pool'][0]['model'] = 'changed'
        self.assertIsNone(ledger.read()['jobs'][first]['model'])


class PoolCycleTests(CycleTest):
    def setUp(self):
        super().setUp()
        self.config['provider_pool'] = pool()

    def test_sequential_roles_rotate_across_restarts(self):
        self.cycle(); build = self.active(); self.assertEqual(build['provider'], 'codex')
        self.github.publish(build); self.engine.finish(build)
        self.cycle(); self.assertEqual(self.active()['provider'], 'claude')
        self.finish('review'); self.cycle()
        self.assertEqual(self.active()['role'], 'audit'); self.assertEqual(self.active()['provider'], 'codex')

    def test_provider_unavailable_falls_back_without_issue_failure(self):
        self.cycle(); job = self.active()
        self.engine.finish(job)
        self.engine.capture = lambda _: {'result': None, 'logs': json.dumps({'type':'error','message':'authentication_error'}),
                                        'execution': {'exit_code':1}, 'missing':[]}
        self.cycle()
        self.assertEqual(self.active()['provider'], 'claude')
        self.assertEqual(self.ledger.read()['issues'][self.key]['failures'], [])
        self.assertGreater(self.ledger.read()['provider_cooldowns']['codex']['until'], 0)


class PoolSetupTests(SetupTests):
    def test_unavailable_harness_does_not_prevent_verifying_other_account(self):
        self.setup.connect('codex', api_key='synthetic-codex-secret')
        self.setup.connect('claude', api_key='synthetic-claude-secret')
        service, engine, resets = self.service()
        original = service.runner_class.capture
        def capture(runner, job):
            if runner.config['provider'] == 'codex':
                return {'result': {'ready':False, 'reason':'provider_login_expired'}}
            return original(runner, job)
        service.runner_class.capture = capture
        self.setup.queue('harness_check', self.setup.view()['revision'])
        for _ in range(30): service.tick()
        self.assertIsNone(self.setup.read()['error'])
        self.assertEqual(self.setup.runtime()['provider_ready'], ['claude'])
        self.assertEqual(self.setup.read()['provider_checks'], {'codex':'failed','claude':'passed'})
        self.assertFalse(engine)

    def test_terminal_checks_all_harnesses_before_github_setup_and_cleans_every_probe(self):
        self.setup.connect('codex', api_key='synthetic-codex-secret')
        self.setup.connect('claude', api_key='synthetic-claude-secret', models=['sonnet', 'opus'])
        service, engine, resets = self.service()
        self.setup.queue('harness_check', self.setup.view()['revision'])
        for _ in range(30): service.tick()
        self.assertEqual(self.setup.read()['provider_checks'], {'codex':'passed','claude-1':'passed','claude-2':'passed'})
        self.assertFalse(self.setup.view()['can_start'])
        self.assertFalse(engine)
        self.assertEqual(len(set(resets)), 5)
        service.github_class.assert_not_called()


if __name__ == '__main__': unittest.main()
