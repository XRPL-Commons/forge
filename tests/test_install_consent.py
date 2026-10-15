"""Single App consent against the real selected setup and private identity store."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit, urlencode
from urllib.request import urlopen, Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

from forge.app_access import APP_PERMISSIONS
from forge.installation_apps import FreshAppConsent
from forge.installation_setup import InstallationSetup
from forge.onboarding import Onboarding
from forge.operator_auth import OperatorAuth
from forge_client.contracts import SetupError
from forge_client.install_consent import InstallConsent
from forge_client.workflow import Journal
from tests.test_sqlite_identity import selected_store


class Vault:
    def __init__(self): self.values = {}
    def get(self, key): return deepcopy(self.values.get(key))
    def set(self, key, value): self.values[key] = deepcopy(value)
    def delete(self, key): self.values.pop(key, None)


class FreshConsentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'host'; self.root.mkdir(mode=0o700)
        self.database = selected_store(self.root)
        self.api = Mock(); self.api.jwt.return_value = 'app-token'
        self.owner = {'id': 99, 'login': 'example', 'type': 'Organization'}
        self.app = {'id': 44, 'slug': 'forge-fixture', 'pem': 'fixture PRIVATE KEY-----',
                    'client_id': 'Iv1.fixture', 'client_secret': 'synthetic_app_secret_' + 'x' * 30}
        self.installed = False
        self.discovered = [{'id': 10, 'name': 'example/project'}]
        self.permissions = APP_PERMISSIONS
        def request(method, path, token, *args):
            if path == '/users/example': return self.owner.copy()
            if path == '/app': return {**self.app, 'owner': self.owner.copy(), 'permissions': self.permissions.copy()}
            if path.startswith('/app/installations?'):
                return [{'id': 55, 'app_id': 44, 'account': self.owner.copy()}] if self.installed else []
            raise AssertionError((method, path))
        self.api.request.side_effect = request
        self.api.discover.side_effect = lambda c: {'repositories': self.discovered.copy(), 'permissions': self.permissions.copy()}
        self.setup = Onboarding(self.root, client=self.api)
        self.server = InstallationSetup(self.root, setup=self.setup)
        self.local = Journal('test-host', 'anvil', root=Path(temp.name) / 'local')
        self.local.save({'version': 1, 'intent': {'id': 'a' * 32}, 'origin': 'https://anvil.example'})
        self.github = Mock(); self.github.identity.return_value = {'id': 7, 'login': 'operator'}
        self.host = Mock()
        self.host.rpc.side_effect = lambda program, request, **kw: {'ok': True, 'result': self.server.call(request['setup'])}
        self.installation = SimpleNamespace(journal=self.local, host=self.host, ssh='anvil')
        self.selected = {'owner': self.owner, 'operators': [{'id': 7, 'login': 'operator'}],
                         'codex_parallel_auth': True, 'repositories': [{'target': self.discovered[0], 'allow_workflow_edits': False}]}
        self.vault = Vault()
        self.events = []
        self.client = self.make_client()

    def make_client(self, **kwargs):
        return InstallConsent(self.installation, self.selected, github=self.github, vault=self.vault,
                              tell=self.events.append, **kwargs)

    def begin(self): return self.client.call('begin', callback='http://127.0.0.1:39123/callback')

    def test_bootstrap_without_identity_then_installed_repo_observation(self):
        self.assertIsNone(OperatorAuth(self.root).config())
        self.assertEqual(self.client.observe()['status'], 'consent_required')
        first = self.begin(); self.assertEqual(first, self.begin())
        manifest = first['registration']['manifest']
        self.assertEqual(manifest['callback_urls'], ['https://anvil.example/auth/github/callback'])
        self.assertEqual(manifest['redirect_url'], 'http://127.0.0.1:39123/callback')
        self.client.checkpoint(self.app)
        result = self.client.import_checkpoint()
        self.assertEqual(result['status'], 'app_installation_required')
        self.assertTrue(result['identity_ready'])
        self.assertIsNone(self.vault.get(self.client.secret_key))
        config = OperatorAuth(self.root).config()
        self.assertEqual(config['operators'], [7])
        self.assertEqual(config['client_id'], self.app['client_id'])
        self.assertTrue(self.setup.runtime()['codex_parallel_auth'])
        self.assertFalse(self.setup.runtime()['enabled'])
        self.installed = True
        result = self.client.apply()
        self.assertEqual(result['status'], 'consent_ready')
        connections = self.setup.settings().load()['connections']
        self.assertEqual({c['app_id'] for c in connections.values()}, {44})
        for file in self.local.root.glob('*.json'):
            self.assertNotIn(self.app['pem'], file.read_text())
            self.assertNotIn(self.app['client_secret'], file.read_text())
        self.assertFalse(any('/conversions' in call.args[1] for call in self.api.request.call_args_list))

    def test_unknown_conversion_cannot_register_duplicate(self):
        self.begin()
        self.client.state['phase'] = 'converting'; self.client.save()
        resumed = self.make_client()
        self.assertEqual(resumed.apply()['status'], 'write_unconfirmed')
        self.assertEqual(len(self.events), 0)
        self.assertEqual(self.setup.read()['apps'], {})

    def test_import_retry_preserves_operator_sessions_and_no_secret_in_receipt(self):
        self.begin(); self.client.checkpoint(self.app)
        result = self.client.call('import', app=self.app)
        with self.database.read_snapshot() as db:
            before = tuple(db.execute('SELECT source_epoch,authorization_epoch FROM operator_auth_configuration').fetchone())
        self.client.import_checkpoint()
        with self.database.read_snapshot() as db:
            after = tuple(db.execute('SELECT source_epoch,authorization_epoch FROM operator_auth_configuration').fetchone())
        self.assertEqual(before, after)
        self.assertNotIn(self.app['client_secret'], json.dumps(result))
        self.assertNotIn(self.app['pem'], json.dumps(result))

    def test_owner_and_generation_binding_cannot_be_replaced(self):
        self.begin()
        self.client.binding['operators'] = [8]
        with self.assertRaises(SetupError): self.client.observe()
        self.client.binding['operators'] = [7]
        with self.setup.store.transaction() as data: data['generation'] = 'reset'
        with self.assertRaises(SetupError): self.client.observe()

    def test_partial_repository_installation_is_not_ready(self):
        self.selected['repositories'].append({'target': {'id': 11, 'name': 'example/scratch'}, 'allow_workflow_edits': True})
        self.client = self.make_client()
        self.permissions = {**APP_PERMISSIONS, 'workflows': 'write'}
        manifest = self.begin()['registration']['manifest']
        self.assertEqual(manifest['default_permissions']['workflows'], 'write')
        self.client.checkpoint(self.app); self.client.import_checkpoint(); self.installed = True
        result = self.client.apply()
        self.assertEqual(result['status'], 'app_installation_required')
        self.assertEqual(result['missing_repositories'], [{'id': 11, 'name': 'example/scratch'}])
        self.assertIn('repository_ids%5B%5D=10', self.events[-1]['url'])
        self.assertIn('repository_ids%5B%5D=11', self.events[-1]['url'])
        self.assertEqual(self.events[-1]['repository_selection'], 'selected')
        self.assertEqual(self.events[-1]['repositories'], [
            {'id': 10, 'name': 'example/project'}, {'id': 11, 'name': 'example/scratch'}])
        self.assertIn('Only select repositories', self.events[-1]['instruction'])
        self.assertIn('example/project, example/scratch', self.events[-1]['instruction'])
        self.assertIn('may default to “All repositories”', self.events[-1]['instruction'])

    def test_repository_guidance_escapes_names_in_html(self):
        self.client.binding['repositories'] = [{'id': 10, 'name': 'example/<script>&repo'}]
        page = self.client.installation_guidance()
        self.assertIn('example/&lt;script&gt;&amp;repo', page)
        self.assertNotIn('<script>', page)

    def test_wrong_app_permissions_refused_before_configured_key_changes(self):
        self.begin(); self.client.checkpoint(self.app)
        self.permissions = {'contents': 'read'}
        with self.assertRaises(SetupError): self.client.import_checkpoint()
        self.assertEqual(self.setup.read()['apps'], {})
        self.assertIsNone(OperatorAuth(self.root).config())
        self.assertIsNotNone(self.vault.get(self.client.secret_key))

    def test_loopback_form_callback_converts_locally_and_rejects_cross_host(self):
        done = threading.Event()
        failures = []
        def browser(event):
            self.events.append(event)
            def visit():
                try:
                    with urlopen(event['url']) as response:
                        page = response.read().decode()
                        policy = response.headers['Referrer-Policy']
                    self.assertIn('Continue on GitHub', page)
                    self.assertIn('Only select repositories', page)
                    self.assertIn('example/project', page)
                    self.assertEqual(event['repositories'], [{'id': 10, 'name': 'example/project'}])
                    launch = event['url'].replace('/start/', '/launch/')
                    origin = 'http://' + urlsplit(event['url']).netloc
                    # Browsers serialize the Origin as null for a form POST
                    # whose page uses no-referrer. urllib does not do this.
                    browser_origin = 'null' if policy == 'no-referrer' else origin
                    for rejected_origin in ('null', 'https://evil.example'):
                        with self.assertRaises(HTTPError) as rejected:
                            urlopen(Request(launch, data=b'', headers={'Origin': rejected_origin}))
                        self.assertEqual(rejected.exception.code, 400)
                        rejected.exception.close()
                        self.assertEqual(self.client.state['phase'], 'waiting')
                        self.client.convert_app.assert_not_called()
                    with urlopen(Request(launch, data=b'', headers={'Origin': browser_origin})) as response:
                        self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')
                        first_form = response.read().decode()
                        self.assertIn('name="manifest"', first_form)
                    self.assertEqual(policy, 'same-origin')
                    with self.assertRaises(HTTPError) as replay: urlopen(Request(launch, data=b''))
                    self.assertEqual(replay.exception.code, 409)
                    replay.exception.close()
                    # Explicit reported refusal resends the exact saved manifest;
                    # merely reopening or replaying the first form cannot do so.
                    registration_before = deepcopy(self.client.state['registration'])
                    with urlopen(event['url']) as response:
                        self.assertEqual(response.headers['Referrer-Policy'], 'same-origin')
                        recovery_page = response.read().decode()
                        self.assertIn('no App was created', recovery_page)
                        self.assertIn('Only select repositories', recovery_page)
                        self.assertIn('example/project', recovery_page)
                    retry = event['url'].replace('/start/', '/retry/')
                    confirmation = urlencode({'nonce': self.client.state['refusal_nonce'],
                        'missing_manifest_no_app': 'confirmed'}).encode()
                    with urlopen(Request(retry, data=confirmation, headers={'Origin': origin})) as response:
                        self.assertIn('name="manifest"', response.read().decode())
                    self.assertEqual(self.client.state['registration'], registration_before)
                    with self.assertRaises(HTTPError) as repeated:
                        urlopen(Request(retry, data=confirmation))
                    self.assertEqual(repeated.exception.code, 409); repeated.exception.close()
                    self.client.state['phase'] = 'converting'
                    with self.assertRaises(HTTPError) as converted:
                        urlopen(Request(retry, data=confirmation))
                    self.assertEqual(converted.exception.code, 409); converted.exception.close()
                    self.client.state['phase'] = 'submitted'
                    request = Request(event['url'], headers={'Host': 'evil.example'})
                    with self.assertRaises(HTTPError) as error: urlopen(request)
                    self.assertEqual(error.exception.code, 400)
                    error.exception.close()
                    registration = self.client.state['registration']
                    state = parse_qs(urlsplit(registration['action']).query)['state'][0]
                    callback = registration['manifest']['redirect_url'] + '?state=' + state + '&code=local-code'
                    class NoRedirect(HTTPRedirectHandler):
                        def redirect_request(self, *args): return None
                    with self.assertRaises(HTTPError) as redirect:
                        build_opener(NoRedirect()).open(callback)
                    self.assertEqual(redirect.exception.code, 303)
                    self.assertIn('repository_ids', redirect.exception.headers['Location'])
                    redirect.exception.close()
                except BaseException as error: failures.append(error)
                finally: done.set()
            threading.Thread(target=visit, daemon=True).start()
        self.client = self.make_client(convert_app=Mock(return_value=self.app), wait=5)
        self.client.tell = browser
        result = self.client.apply()
        self.assertTrue(done.wait(5))
        if failures: raise failures[0]
        self.assertEqual(result['status'], 'app_installation_required')
        self.client.convert_app.assert_called_once_with('local-code')
        self.assertIsNone(self.vault.get(self.client.secret_key))


if __name__ == '__main__': unittest.main()
