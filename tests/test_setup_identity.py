import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge_client import _identity_host as remote, bootstrap, identity
from forge_client.cli import parser
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
        for name, value in (('facts', Mock()), ('HOME', home),
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


if __name__ == '__main__':
    unittest.main()
