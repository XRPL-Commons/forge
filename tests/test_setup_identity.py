import io
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge_client import _identity_host as remote, bootstrap, identity
from forge_client.cli import parser, main
from forge_client.contracts import SetupError
from forge_client.install import Installation
from forge_client.network import payload
from forge_client.workflow import Journal

INTENT = {'id': '1' * 32, 'name': 'Anvil', 'revision': 'a' * 40}
ORIGIN = 'https://anvil.example.ts.net'
SECRET = 'b' * 40
TARGET = {'origin': ORIGIN, 'client_id': 'identity-client', 'operators': [123]}
PLAN = {'ok': True, 'status': 'planned', **TARGET, 'generation': 'generation-one',
        'configured': False, 'revision': 'a' * 64}


class IdentityHostTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name).resolve()
        (home / 'source/forge').mkdir(parents=True)
        for name in ('launch.py', 'operator_bootstrap.py'):
            (home / 'source/forge' / name).touch()
        for name, value in (('facts', Mock()), ('HOME', home), ('SOURCE', home / 'source'),
                            ('read', Mock(return_value={'intent': INTENT, 'step': 'complete'}))):
            context = patch.object(remote, name, value)
            context.start(); self.addCleanup(context.stop)
        context = patch.object(remote.pwd, 'getpwnam', return_value=SimpleNamespace(pw_uid=os.getuid(), pw_dir=str(home)))
        context.start(); self.addCleanup(context.stop)
        self.request = {'version': 1, 'intent': INTENT, 'bootstrap': {
            'version': 1, 'action': 'apply', 'target': TARGET, 'revision': PLAN['revision'], 'secret': SECRET}}

    def run_reply(self, reply):
        with patch.object(remote.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(reply).encode())) as run:
            result = remote.identity_dispatch(self.request)
        return result, run

    def test_runs_shared_bootstrap_as_service_account_with_secret_only_on_stdin(self):
        if os.getuid() == 0:
            self.skipTest('fixture requires a non-root owned home')
        result, run = self.run_reply(PLAN)
        self.assertEqual(result, PLAN)
        argv = run.call_args.args[0]
        options = run.call_args.kwargs
        self.assertEqual(argv[:6], ['runuser', '-u', 'forge', '--', 'env', '-i'])
        self.assertIn('forge.operator_bootstrap', argv)
        self.assertEqual(json.loads(options['input'])['secret'], SECRET)
        self.assertNotIn(SECRET, str(argv) + str(options['env']))
        self.assertEqual(options['stderr'], remote.subprocess.DEVNULL)

    def test_old_server_reports_maintenance_before_bootstrap_or_secret_transfer(self):
        (remote.SOURCE / 'forge/launch.py').unlink()
        with patch.object(remote.subprocess, 'run') as run:
            with self.assertRaisesRegex(remote.InstallError, '^server_maintenance_required$'):
                remote.identity_dispatch(self.request)
        run.assert_not_called()

    def test_wrong_installation_or_incomplete_host_stops_before_process(self):
        for installation in ({'intent': {**INTENT, 'id': '2' * 32}, 'step': 'complete'},
                             {'intent': INTENT, 'step': 'host'}):
            remote.read.return_value = installation
            with patch.object(remote.subprocess, 'run') as run, self.assertRaises(remote.InstallError):
                remote.identity_dispatch(self.request)
            run.assert_not_called()

    def test_remote_diagnostics_and_secret_echo_are_removed(self):
        if os.getuid() == 0:
            self.skipTest('fixture requires a non-root owned home')
        for code in ('https_unavailable', {'raw': SECRET}, SECRET):
            result, _ = self.run_reply({'ok': False, 'error': code, 'reason': SECRET})
            self.assertNotIn(SECRET, json.dumps(result))
            self.assertIn(result['error'], ('https_unavailable', 'bootstrap_failed'))
        for reply in ({**PLAN, 'unexpected': SECRET}, {**PLAN, 'client_id': SECRET}):
            with self.assertRaises(remote.InstallError): self.run_reply(reply)

    def test_composed_payload_runs_identity_entrypoint(self):
        namespace = {'__name__': 'forge_rpc'}
        exec(compile(payload('_identity_host.py'), '<identity-payload>', 'exec'), namespace)
        self.assertEqual(namespace['main'].__name__, 'identity_main')


class IdentityClientTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Journal('test', 'identity', root=Path(temporary.name).resolve())
        self.store.save({'version': 1, 'ssh': 'anvil', 'intent': INTENT, 'milestones': {},
                         'host_key': 'pinned-key', 'origin': ORIGIN})
        self.host = Mock()
        self.host.rpc.side_effect = lambda program, request, **kw: dict(
            PLAN, client_id=request['bootstrap']['target']['client_id'],
            **({'status': 'ready', 'configured': True} if request['bootstrap']['action'] != 'plan' else {}))
        self.workflow = Installation('anvil', host=self.host, storage=self.store, clock=lambda: 100)
        self.args = parser().parse_args(['setup', 'identity', '--ssh', 'anvil', '--operator', 'operator', '--json'])
        self.github = Mock()
        self.github.owner.return_value = {'id': 123, 'login': 'operator'}
        self.verify = Mock()

    def execute(self):
        def setup(args, **kwargs):
            return bootstrap.setup(args, **kwargs, github_factory=lambda: self.github, verify=self.verify,
                                   stdin=io.StringIO(SECRET), prompt=Mock(side_effect=AssertionError('Unexpected prompt')))
        return identity.execute(self.args, workflow=self.workflow, setup=setup)

    def test_handoff_uses_saved_origin_and_factory_name_without_writes_to_host(self):
        result = self.execute()
        self.assertEqual(result['status'], 'identity_app_required')
        self.assertEqual(result['next_action']['application_name'], 'Anvil — Forge operator sign-in')
        self.assertEqual(result['next_action']['callback_url'], ORIGIN + '/auth/github/callback')
        self.assertEqual(self.host.rpc.call_args.args[1]['bootstrap']['action'], 'plan')
        self.assertEqual(self.host.fingerprint, 'pinned-key')
        self.assertNotIn('https_configured', self.store.read()['milestones'])

    def test_apply_and_resume_reuse_shared_bootstrap_and_never_store_secret(self):
        self.args.client_id = TARGET['client_id']
        self.args.apply = self.args.secret_stdin = True
        result = self.execute()
        self.assertEqual(result['status'], 'sign_in_required')
        request = self.host.rpc.call_args.args[1]
        self.assertEqual(request['intent'], INTENT)
        self.assertEqual(request['bootstrap']['secret'], SECRET)
        self.assertEqual(self.store.read()['milestones'], {'https_configured': 100})
        self.assertNotIn(SECRET, json.dumps(self.store.read()) + json.dumps(result))
        self.verify.assert_called_once_with(ORIGIN)
        self.args.apply = self.args.secret_stdin = False
        self.args.resume = True
        self.args.client_id = self.args.operator = None
        self.execute()
        request = self.host.rpc.call_args.args[1]['bootstrap']
        self.assertEqual(request['action'], 'resume')
        self.assertIsNone(request['secret'])
        self.assertEqual(request['target'], TARGET)

    def test_json_apply_retains_public_client_id_when_waiting_for_secret(self):
        self.args.apply = True
        self.args.client_id = TARGET['client_id']
        self.assertEqual(self.execute()['status'], 'identity_secret_required')
        self.assertEqual(self.store.read()['identity_client_id'], TARGET['client_id'])
        self.verify.assert_not_called()

    def test_no_network_origin_stops_before_github_or_host(self):
        state = self.store.read(); state.pop('origin'); self.store.save(state)
        with self.assertRaises(SetupError) as caught: self.execute()
        self.assertEqual(caught.exception.code, 'network_not_ready')
        self.host.rpc.assert_not_called()
        self.github.owner.assert_not_called()


class SignInGuideTests(unittest.TestCase):
    def test_friendly_command_runs_wizard_but_json_never_prompts(self):
        result = {'status': 'planned', 'server': ORIGIN, 'client_id': TARGET['client_id'], 'steps': ['Configure sign-in']}
        with patch('sys.stdin.isatty', return_value=True), patch.object(identity, 'wizard', return_value=result) as wizard, \
                patch.object(identity, 'execute', return_value=result) as execute, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['setup', 'sign-in', '--ssh', 'anvil']), 0)
            wizard.assert_called_once()
            self.assertIn('GitHub sign-in setup', output.getvalue())
            self.assertNotIn('"ok"', output.getvalue())
            wizard.reset_mock()
            output.seek(0); output.truncate()
            self.assertEqual(main(['setup', 'sign-in', '--ssh', 'anvil', '--json']), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'planned')
            wizard.assert_not_called()
            execute.assert_called_once()

    def test_error_explains_saved_secret_and_exact_resume_command(self):
        with patch.object(identity, 'execute', side_effect=SetupError('https_unavailable', 'HTTPS check failed.')), \
                redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['setup', 'sign-in', '--ssh', 'anvil', '--resume']), 1)
        self.assertIn('do not need to enter the secret again', output.getvalue())
        self.assertIn('forge setup sign-in --ssh anvil --resume', output.getvalue())

    def test_registration_explains_manual_github_actions_and_exact_fields(self):
        output = []
        identity.registration({'next_action': {'url': 'https://github.com/settings/applications/new',
            'application_name': 'Anvil — Forge operator sign-in', 'homepage_url': ORIGIN,
            'callback_url': ORIGIN + '/auth/github/callback'}}, tell=output.append)
        text = '\n'.join(output)
        for expected in ('you register it on GitHub', 'Register application', 'Client ID',
                         'Anvil — Forge operator sign-in', ORIGIN + '/auth/github/callback', 'Device Flow off'):
            self.assertIn(expected, text)

    def test_wizard_resumes_existing_app_without_asking_for_registration_again(self):
        workflow = Mock(ssh='anvil')
        workflow.journal.read.return_value = {'operators': ['operator']}
        planned = {'status': 'planned', 'server': ORIGIN, 'operators': [{'login': 'operator'}]}
        ready = {'status': 'sign_in_required', 'next_action': {'url': ORIGIN + '/login'}}
        calls = []
        def execute(args, **kwargs):
            calls.append((args.apply, args.client_id))
            return ready if args.apply else planned
        ask, browser = Mock(return_value='yes'), Mock()
        with patch.object(identity, 'execute', side_effect=execute):
            self.assertEqual(identity.wizard(workflow, ask=ask, tell=Mock(), open_browser=browser), ready)
        self.assertEqual(calls, [(False, None), (True, None)])
        ask.assert_called_once_with('Continue with these settings? [y/N] ')
        browser.assert_called_once_with(ORIGIN + '/login')


if __name__ == '__main__':
    unittest.main()
