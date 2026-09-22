import hashlib
import io
import json
import subprocess
import threading
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.credentials import Credentials
from forge.factory.config import validate
from forge.factory.ledger import Ledger
from forge.factory.providers import available
from forge.harnesses.registry import manifests, adapter
from forge.harnesses.claude.canary import check_repository_access
from forge.host_setup import login, pending_login, main as setup_main
from forge.factory.control import controller_lock, ControllerBusy
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

    def test_claude_probe_reports_fixed_failure_reasons(self):
        reason = adapter('claude')['probe_failure_reason']
        self.assertEqual(reason(None, 'Provider authentication_error; reconnect the account', None, 'n'), 'provider_login_expired')
        self.assertEqual(reason(None, 'Provider usage limit reached', None, 'n'), 'provider_usage_limit')
        self.assertEqual(reason(None, 'Claude Code did not return a usable result', None, 'n'), 'provider_connection_failed')
        self.assertEqual(reason(None, 'canary', None, 'n'), 'native_sandbox_failed')
        self.assertEqual(reason({'ready': True}, None, 'other', 'n'), 'native_sandbox_failed')
        self.assertEqual(reason({'ready': True}, None, None, 'n'), 'native_sandbox_failed')
        self.assertIsNone(reason({'ready': True}, None, 'n', 'n'))

    def test_claude_harness_environment_lets_oauth_refresh_reach_the_proxy(self):
        env = adapter('claude')['harness_environment']({'HTTPS_PROXY': 'http://forge-egress:3128', 'CLAUDE_CODE_PROXY_RESOLVES_HOSTS': '1', 'GH_TOKEN': 'job'})
        self.assertNotIn('CLAUDE_CODE_PROXY_RESOLVES_HOSTS', env)
        self.assertEqual(env['HTTPS_PROXY'], 'http://forge-egress:3128')
        self.assertEqual(env['CLAUDE_CODE_SUBPROCESS_ENV_SCRUB'], '1')
        self.assertEqual(env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'], '1')
        self.assertTrue(env['PATH'].startswith('/opt/forge/claude-tools:'))

    def test_grok_probe_reports_fixed_failure_reasons(self):
        module = adapter('grok')
        self.assertEqual(module['error_message']('Error: Not signed in. To authenticate without a browser, run: grok login --device-auth'),
                         'Provider authentication_error; reconnect the account')
        self.assertEqual(module['error_message']('{"type":"error","message":"insufficient_quota"}'), 'Provider usage limit reached')
        self.assertEqual(module['error_message']('bwrap: setting up uid map: Permission denied'), 'Grok Build did not return a usable result')
        reason = module['probe_failure_reason']
        self.assertEqual(reason(None, 'Provider authentication_error; reconnect the account', None, 'n'), 'provider_login_expired')
        self.assertEqual(reason(None, 'Grok Build did not return a usable result', None, 'n'), 'provider_connection_failed')
        self.assertEqual(reason(None, 'canary', None, 'n'), 'native_sandbox_failed')
        self.assertEqual(reason({'ready': True}, None, 'other', 'n'), 'native_sandbox_failed')
        self.assertIsNone(reason({'ready': True}, None, 'n', 'n'))
        env = module['harness_environment']({'GH_TOKEN': 'job', 'XAI_API_KEY': 'synthetic'})
        self.assertEqual(env['GROK_HOME'], '/work/home/.grok')
        self.assertEqual(env['GROK_DISABLE_AUTOUPDATER'], '1')
        self.assertNotIn('/work/repository', env['PATH'])

    def test_binary_harness_manifest_is_pinned_and_verifiable(self):
        item = manifests()['grok']
        self.assertEqual(item['distribution'], 'binary')
        self.assertRegex(item['url'], r'^https://x\.ai/cli/grok-' + item['version'].replace('.', r'\.') + r'-linux-x86_64$')
        self.assertRegex(item['sha256'], r'^[a-f0-9]{64}$')
        self.assertEqual(item['auth_file'], '.grok/auth.json')
        self.assertEqual(item['login'], ['login', '--device-auth'])
        self.assertEqual(set(item['managed_files'].values()), {'/etc/grok/requirements.toml', '/etc/grok/managed_config.toml'})
        for name in item['managed_files']:
            self.assertTrue((Path('forge/harnesses/grok') / name).is_file(), name)

    def test_setup_probe_failure_messages_name_the_cause(self):
        from forge.setup_service import probe_failure_message
        self.assertIn('setup login claude', probe_failure_message({'result': {'reason': 'provider_login_expired'}}, 'claude'))
        self.assertIn('usage limit', probe_failure_message({'result': {'reason': 'provider_usage_limit'}}, 'claude'))
        self.assertIn('egress proxy', probe_failure_message({'result': {'reason': 'provider_connection_failed'}}, 'codex'))
        self.assertIn('native tool sandbox', probe_failure_message({'result': {'reason': 'native_sandbox_failed'}}, 'claude'))
        self.assertIn('deadline', probe_failure_message({'result': None, 'logs': ''}, 'claude', timed_out=True))
        logs = '{"runner": "alive"}\n' + json.dumps({'type': 'error', 'message': 'Provider authentication_error; reconnect the account'}) + '\nTraceback\n'
        self.assertIn('the harness reported: Provider authentication_error', probe_failure_message({'result': None, 'logs': logs}, 'claude'))
        self.assertIn('(reason: odd_label)', probe_failure_message({'result': {'reason': 'odd_label'}}, 'claude'))
        self.assertNotIn('\x1b', probe_failure_message({'result': None, 'logs': json.dumps({'type': 'error', 'message': 'x\x1b[31m y'})}, 'claude'))
        self.assertTrue(probe_failure_message({'result': None, 'logs': 'garbage'}, 'claude').startswith('Provider or native sandbox check failed'))

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

    def test_repository_access_probe_rejects_missing_token_and_network_failure(self):
        with patch.dict('os.environ', {}, clear=True), patch('urllib.request.urlopen') as request:
            with self.assertRaisesRegex(SystemExit, 'token was removed'):
                check_repository_access()
            request.assert_not_called()
        with patch.dict('os.environ', {'GH_TOKEN': 'forge-synthetic-repository-token'}):
            with patch('urllib.request.urlopen', side_effect=OSError('unreachable')):
                with self.assertRaisesRegex(SystemExit, 'connectivity check failed'):
                    check_repository_access()
            with patch('urllib.request.urlopen') as request:
                check_repository_access()
                request.assert_called_once_with('https://api.github.com/meta', timeout=15)

    def test_terminal_import_preserves_source_and_connects_both_harnesses(self):
        source = self.root / 'source.json'; source.write_text('{"synthetic":"login"}')
        with patch('sys.stdout', new=io.StringIO()):
            for name in manifests(): login(self.root, name, import_login=source)
        setup = Onboarding(self.root)
        self.assertEqual({e['provider'] for e in setup.runtime()['provider_pool']}, set(manifests()))
        self.assertEqual(source.read_text(), '{"synthetic":"login"}')
        self.assertTrue(all(h['connected'] for h in setup.view()['harnesses']))
        self.assertNotIn('synthetic', json.dumps(setup.view()))

    def test_login_waits_for_supervisor_and_resumes_saved_sign_in_after_timeout(self):
        source = self.root / 'source.json'; source.write_text('{"synthetic":"login"}')
        with patch('forge.host_setup.controller_lock', side_effect=ControllerBusy('busy')) as lock:
            with patch('sys.stdout', new=io.StringIO()), self.assertRaisesRegex(ValueError, 'Sign-in is saved'):
                login(self.root, 'claude', import_login=source, models=['sonnet'])
            lock.assert_called_once_with(self.root, timeout=120)
        setup = Onboarding(self.root)
        pending = self.root / 'credentials/pending-login-claude.json'
        self.assertEqual(pending.stat().st_mode & 0o777, 0o600)
        self.assertFalse(setup.runtime()['provider_pool'])
        self.assertNotIn('synthetic', json.dumps(setup.view()))
        # A second invocation uses the private saved login without Docker/OAuth.
        with patch('forge.host_setup.command') as command, patch('sys.stdout', new=io.StringIO()):
            login(self.root, 'claude')
            command.assert_not_called()
        self.assertFalse(pending.exists())
        self.assertEqual(setup.runtime()['provider_pool'][0]['model'], 'sonnet')
        self.assertEqual(Path(setup.credentials.environment()['FORGE_SETUP_CLAUDE_AUTH']).read_text(), source.read_text())

    def test_interactive_login_is_saved_before_container_cleanup_failure(self):
        setup = Onboarding(self.root)
        config = setup.runtime(); config['image'] = 'sha256:' + 'a' * 64
        write_json(self.root / 'setup/runtime.json', config)
        def command(args, **kwargs):
            if args[1] == 'create':
                mount = args[args.index('--mount') + 1]
                home = Path(mount.split('source=', 1)[1].split(',', 1)[0])
                (home / '.claude').mkdir()
                (home / '.claude/.credentials.json').write_text('{"synthetic":"interactive"}')
                return Mock(stdout='container-id')
            if args[1] == 'rm':
                raise subprocess.CalledProcessError(1, args)
            return Mock(returncode=0)
        from forge.harnesses.accounts import owner
        name = 'forge-login-' + owner(self.root) + '-claude'
        with patch('forge.host_setup.subprocess.run', side_effect=[Mock(returncode=1), Mock(returncode=0, stdout=name)]), patch('forge.host_setup.command', side_effect=command):
            with self.assertRaises(subprocess.CalledProcessError): login(self.root, 'claude')
        record = pending_login(setup, 'claude', setup.read()['generation'])
        self.assertEqual(record['value'], '{"synthetic":"interactive"}')
        with patch('forge.host_setup.subprocess.run', return_value=Mock(returncode=0, stdout=name)), patch('forge.host_setup.command') as command, patch('sys.stdout', new=io.StringIO()):
            login(self.root, 'claude')
            self.assertEqual(command.call_args.args[0], ['docker', 'rm', '--force', name])
        self.assertFalse((self.root / 'credentials/pending-login-claude.json').exists())

    def test_saved_login_from_before_reset_is_never_imported(self):
        setup = Onboarding(self.root); generation = setup.read()['generation']
        pending_login(setup, 'claude', generation, {'secret': None, 'value': '{"old":"login"}', 'models': None})
        with setup.store.transaction() as data: data['generation'] = 'new-generation'
        self.assertIsNone(pending_login(setup, 'claude', 'new-generation'))
        self.assertFalse((self.root / 'credentials/pending-login-claude.json').exists())
        with self.assertRaisesRegex(ValueError, 'reset during login'):
            pending_login(setup, 'claude', generation, {'secret': None, 'value': '{}', 'models': None})

    def test_controller_lock_remains_exclusive_and_can_wait_for_release(self):
        with controller_lock(self.root):
            with self.assertRaises(ControllerBusy):
                with controller_lock(self.root): pass
            with self.assertRaises(ControllerBusy):
                with controller_lock(self.root, timeout=0.01): pass
            entered = threading.Event()
            def contender():
                with controller_lock(self.root, timeout=2): entered.set()
            thread = threading.Thread(target=contender); thread.start()
            self.assertFalse(entered.wait(0.05))
        thread.join(timeout=3)
        self.assertTrue(entered.is_set())

    def test_terminal_explains_state_errors_without_generic_host_failure(self):
        stderr = io.StringIO()
        with patch('sys.argv', ['forge setup', 'login', 'claude', '--root', str(self.root)]), patch('forge.host_setup.login', side_effect=StateError('Supervisor is busy')), patch('sys.stderr', new=stderr):
            self.assertEqual(setup_main(), 1)
        self.assertIn('Supervisor is busy', stderr.getvalue())
        self.assertNotIn('Host command failed', stderr.getvalue())

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
