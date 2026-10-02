from copy import deepcopy
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from forge.client_connections import ClientConnections, CAPABILITIES, ConnectionError
from forge.client_repositories import ClientRepositories
from forge.operator_auth import OperatorAuth, read_cookie, FLOW_COOKIE, SESSION_COOKIE
from forge.repository_requirements import describe
from forge.settings import digest, write_json
from forge_client.cli import main, parser
from forge_client.connection import Client
from forge_client.contracts import SetupError
from forge_client.workflow import Workflow, Journal
from tests import test_agent_repository_setup as boundary_tests
from tests.test_agent_repository_setup import SECRET
from tests.test_client_connections import MemoryVault


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.boundary = boundary_tests.BoundaryTests()
        self.boundary.setUp()
        self.addCleanup(self.boundary.doCleanups)
        b = self.boundary
        self.now = 1000
        self.generation = 'generation-one'
        self.auth = OperatorAuth(b.root, clock=lambda: self.now, identity=lambda *args: {'id': 123, 'login': 'operator'})
        self.auth.configure('https://forge.example', 'client-id', 's' * 40, [123])
        url, cookie = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.session = read_cookie(self.auth.callback(state, 'code', read_cookie(cookie, FLOW_COOKIE)), SESSION_COOKIE)
        self.connections = ClientConnections(b.root, auth=self.auth, generation=lambda: self.generation)
        self.service = ClientRepositories(b.settings, self.connections, setup=b.service, launch=lambda fn: fn())
        self.requests = []
        def call(operation, value, token=None):
            self.requests.append((operation, deepcopy(value)))
            try:
                if operation == 'device': result = self.connections.begin(value)
                elif operation == 'token': result = self.connections.poll(value)
                elif operation == 'status': result = self.connections.status(token)
                elif operation == 'authorize':
                    self.connections.authorize(token, value['repository'], value['capability'], value['instance'], value['generation'])
                    result = {'authorized': True}
                elif operation == 'requirements':
                    self.connections.authorize(token, value['repository'], 'repositories:read', value['instance'], value['generation'])
                    result = describe(b.settings, value['repository'])
                elif operation == 'operation': result = self.service.operation(token, value)
                elif operation == 'read': result = self.service.read(token, value)
                else: raise AssertionError(operation)
                return {'ok': True, **result}
            except ConnectionError as exc:
                raise SetupError(exc.code, str(exc)) from None
        self.client = Client('https://forge.example', vault=MemoryVault(), transport=Mock(call=call),
                             clock=lambda: self.now, sleep=lambda delay: setattr(self, 'now', self.now + delay))
        scope = {'repositories': [b.target], 'owners': []}
        display = self.client.begin('Test agent', list(CAPABILITIES), scope)
        self.connections.decide(self.session, {'code': display['user_code'], 'decision': 'approve',
            'capabilities': list(CAPABILITIES), 'scope': scope, 'confirmed': True})
        self.client.resume(wait=5)
        self.journal = Journal(self.client.key, b.target, root=b.root / 'local')

    def workflow(self, **kwargs):
        return Workflow(self.client, self.boundary.broker, self.boundary.target, journal=self.journal, **kwargs)

    def test_full_setup_from_invisible_to_enabled_and_repeat(self):
        b = self.boundary
        result = self.workflow().execute('setup', apply=True, enable=True)
        self.assertEqual(result['status'], 'enabled')
        self.assertEqual(len(b.writes), 5)
        self.assertTrue(b.settings.view()['repositories'][0]['dispatch_allowed'])
        self.assertEqual(self.workflow().execute('setup', apply=True, enable=True)['status'], 'enabled')
        self.assertEqual(len(b.writes), 5)
        self.assertNotIn(SECRET, json.dumps(self.requests))
        for file in b.root.rglob('*.json'):
            self.assertNotIn(SECRET, file.read_text())
        for token in ('access_token', 'refresh_token'):
            self.assertNotIn(self.client._saved()[token], self.service.store.path.read_text())
            self.assertNotIn(self.client._saved()[token], self.journal.path.read_text())

    def test_plan_has_no_operations_or_writes_and_cli_flags(self):
        self.assertEqual(self.workflow().execute('setup', enable=True)['status'], 'planned')
        self.assertFalse(self.boundary.writes)
        self.assertFalse(any(op == 'operation' for op, _ in self.requests))
        args = parser().parse_args(['repo', 'setup', 'example/project', '--server', 'https://forge.example', '--apply', '--enable', '--json'])
        self.assertTrue(args.json and args.apply and args.enable)
        output = io.StringIO()
        with patch('sys.stdout', output): self.assertEqual(main([]), 0)
        self.assertIn('repo', output.getvalue())

    def test_cli_drives_actual_workflow_and_json_does_not_open_browser(self):
        with patch('forge_client.cli.Client', return_value=self.client), patch('forge_client.cli.GitHub', return_value=self.boundary.broker), \
             patch('forge_client.cli.connection_lock') as lock, patch('forge_client.workflow.Journal', return_value=self.journal), \
             patch('forge_client.cli.webbrowser.open') as browser, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['repo', 'setup', 'example/project', '--server', 'https://forge.example', '--apply', '--enable', '--json']), 0)
            self.assertEqual(json.loads(output.getvalue())['status'], 'enabled')
            browser.assert_not_called()
            lock.assert_called_once()

    def test_server_restart_pending_operation_resumes_without_duplicate(self):
        queued = []
        self.service.launch = queued.append
        result = self.workflow(wait=0).execute('setup', apply=True, enable=True)
        self.assertEqual(result['status'], 'operation_pending')
        identifier = result['operation_id']
        self.service = ClientRepositories(self.boundary.settings, self.connections, setup=self.boundary.service, launch=lambda fn: fn())
        result = self.workflow().execute('setup', apply=True, enable=True, resume=True)
        self.assertEqual(result['status'], 'enabled')
        self.assertEqual(self.service.store.read()[identifier]['status'], 'complete')
        self.assertEqual(len(self.boundary.writes), 5)

    def test_interrupted_create_does_not_duplicate_and_independent_verification_recovers(self):
        broker = self.boundary.broker
        original = broker.create_baseline
        def crash(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(broker, 'create_baseline', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt): self.workflow().execute('setup', apply=True, enable=True)
        self.assertIsNotNone(self.journal.read()['local_write'])
        result = self.workflow().execute('setup', apply=True, enable=True, resume=True)
        self.assertEqual(result['status'], 'enabled')
        self.assertEqual(len(self.boundary.writes), 5)

    def test_uncertain_absent_rule_is_not_blindly_recreated(self):
        with patch.object(self.boundary.broker, 'create_baseline', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.workflow().execute('setup', apply=True)
        result = self.workflow().execute('setup', apply=True, resume=True)
        self.assertEqual(result['status'], 'write_unconfirmed')
        self.assertEqual(len(self.boundary.writes), 4)

    def test_browser_grant_handoff_and_resume(self):
        with patch.object(self.boundary.broker, 'grant_repository', side_effect=SetupError('github_request_failed', SECRET)):
            result = self.workflow().execute('setup', apply=True, enable=True)
        self.assertEqual(result['status'], 'needs_user_action')
        self.assertNotIn(SECRET, json.dumps(result))
        self.boundary.granted = {1, 2, 3, 4}
        result = self.workflow().execute('setup', apply=True, enable=True, resume=True)
        self.assertEqual(result['status'], 'enabled')
        self.assertEqual(len(self.boundary.writes), 1)

    def test_changed_intent_and_generation_cannot_resume(self):
        self.service.launch = lambda fn: None
        self.workflow(wait=0).execute('setup', apply=True, enable=True)
        with self.assertRaises(SetupError): self.workflow().execute('setup', apply=True, resume=True)
        self.generation = 'reset-generation'
        with self.assertRaises(SetupError): self.workflow().execute('setup', apply=True, enable=True, resume=True)
        self.assertFalse(self.boundary.writes)

    def test_changed_apps_on_a_reused_connection_require_review_before_writes(self):
        self.workflow().execute('setup')
        config = self.boundary.settings.load()
        config['connections']['controller']['app_id'] = 99
        write_json(self.boundary.settings.path, config)
        with self.assertRaisesRegex(SetupError, 'App identities changed'):
            self.workflow().execute('setup', apply=True)
        self.assertFalse(self.boundary.writes)

    def test_invalid_operation_fields_never_dispatch(self):
        saved = self.client._saved()
        value = {'repository': self.boundary.target, 'instance': saved['instance'], 'generation': saved['generation'],
                 'id': 'a' * 32, 'action': 'verify', 'revision': digest(self.boundary.settings.load())}
        for extra in ({'command': 'shell'}, {'github_token': SECRET}, {'endpoint': 'https://other.example'}):
            with self.assertRaises(ConnectionError): self.service.operation(saved['access_token'], {**value, **extra})
        self.assertEqual(self.service.store.read(), {})

    def test_concurrent_duplicate_verification_has_one_effect(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Event
        queued = []
        self.service.launch = queued.append
        self.workflow(wait=0).execute('setup', apply=True)
        request = self.journal.read()['operation']
        saved = self.client._saved()
        value = {**request, 'instance': saved['instance'], 'generation': saved['generation']}
        entered, release = Event(), Event()
        original = self.boundary.settings.refresh
        calls = []
        def refresh():
            calls.append(True); entered.set()
            self.assertTrue(release.wait(5))
            return original()
        with patch.object(self.boundary.settings, 'refresh', side_effect=refresh), ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(self.service.run, saved['access_token'], value)
            self.assertTrue(entered.wait(5))
            second = pool.submit(self.service.run, saved['access_token'], value)
            second.result(timeout=5)
            release.set(); first.result(timeout=5)
        self.assertEqual(len(calls), 1)

    def test_revocation_between_dispatch_and_effect(self):
        queued = []
        self.service.launch = queued.append
        self.workflow(wait=0).execute('setup', apply=True, enable=True)
        self.connections.revoke(self.session, self.client.status()['id'])
        for fn in queued: fn()
        self.assertFalse(self.boundary.writes)
        self.assertEqual(next(iter(self.service.store.read().values()))['error'], 'invalid_token')

    def test_readonly_cannot_mutate_and_out_of_scope_cannot_read(self):
        with self.connections.store.transaction() as data:
            for grant in data['clients'].values(): grant['capabilities'] = ['repositories:read']
        with self.assertRaises(SetupError): self.workflow().execute('setup', apply=True)
        self.assertFalse(self.boundary.writes)
        with self.assertRaises(SetupError): self.client.read({'id': 999, 'name': 'other/private'})

    def test_duplicate_operation_binding_and_changed_revision(self):
        saved = self.client._saved()
        value = {'repository': self.boundary.target, 'instance': saved['instance'], 'generation': saved['generation'],
                 'id': 'a' * 32, 'action': 'verify', 'revision': digest(self.boundary.settings.load())}
        token = saved['access_token']
        self.service.operation(token, value)
        self.assertEqual(self.service.operation(token, value)['status'], 'complete')
        with self.assertRaises(ConnectionError): self.service.operation(token, {**value, 'action': 'enable'})
        result = self.service.operation(token, {**value, 'id': 'b' * 32, 'revision': '0' * 64})
        self.assertEqual(self.service.operation(token, {**value, 'id': 'b' * 32, 'revision': '0' * 64})['error'], 'revision_changed')

    def test_enable_checks_fresh_protection_even_after_verified(self):
        self.workflow().execute('setup', apply=True)
        self.boundary.github_rules.clear()
        with self.assertRaises(SetupError): self.workflow().execute('enable')
        self.assertFalse(self.boundary.settings.view()['repositories'][0]['enabled'])

    def test_concurrent_configuration_change_cannot_activate_old_revision(self):
        self.workflow().execute('setup', apply=True)
        original = self.boundary.service.check
        def concurrent(*args):
            result = original(*args)
            config = self.boundary.settings.load()
            config['access_max_age_seconds'] += 1
            write_json(self.boundary.settings.path, config)
            return result
        with patch.object(self.boundary.service, 'check', side_effect=concurrent):
            with self.assertRaises(SetupError): self.workflow().execute('enable')
        self.assertFalse(any(row['enabled'] for row in self.boundary.settings.load()['repositories']))

    def test_revocation_after_verification_prevents_activation(self):
        self.workflow().execute('setup', apply=True)
        original = self.boundary.service.check
        identifier = self.client.status()['id']
        def revoked(*args):
            result = original(*args)
            self.connections.revoke(self.session, identifier)
            return result
        with patch.object(self.boundary.service, 'check', side_effect=revoked):
            with self.assertRaises(SetupError): self.workflow().execute('enable')
        self.assertFalse(any(row['enabled'] for row in self.boundary.settings.load()['repositories']))

    def test_disable_uses_same_service_when_access_is_lost(self):
        self.workflow().execute('setup', apply=True, enable=True)
        self.boundary.granted.clear()
        self.boundary.settings.refresh()
        self.assertEqual(self.workflow().execute('disable')['status'], 'disabled')
        self.assertFalse(self.boundary.settings.load()['repositories'][0]['enabled'])

    def test_activation_receipt_loss_reconciles_exact_config(self):
        self.workflow().execute('setup', apply=True)
        original = self.service.finish
        def crash(identifier, result):
            if result['readiness']['enabled']: raise KeyboardInterrupt()
            original(identifier, result)
        with patch.object(self.service, 'finish', side_effect=crash):
            with self.assertRaises(KeyboardInterrupt): self.workflow().execute('enable')
        self.assertEqual(self.workflow().execute('enable', resume=True)['status'], 'enabled')

    def test_activity_and_issue_state_hide_other_repositories(self):
        target = self.boundary.target
        state = {'active': True, 'observation': {'at': 1000, 'errors': [{'reason': 'other/private'}]},
                 'issues': [{'repo_id': 10, 'key': '10:1'}, {'repo_id': 999, 'key': '999:1'}],
                 'jobs': [{'repo_id': 10, 'id': 'safe'}, {'repo_id': 999, 'id': 'private'}],
                 'actions': ['private'], 'slots': ['private'], 'runtime': {'private': True}}
        with patch('forge.factory.control.view', return_value=state):
            result = self.client.read(target, 'activity')
        self.assertEqual(result['jobs'], [{'repo_id': 10, 'id': 'safe'}])
        self.assertNotIn('private', json.dumps(result))
        state['observation'] = {}
        with patch('forge.factory.control.view', return_value=state):
            result = self.client.read(target, 'activity')
        self.assertFalse(result['fresh'])
        self.assertIsNone(result['healthy'])
        inventory = {'issues': [{'repo_id': 10}], 'repositories': [{'id': 10}, {'id': 999}],
                     'actions': [{'key': '10:1'}, {'key': '999:1'}]}
        with patch('forge.factory.issues.IssueInventory') as service:
            service.return_value.view.return_value = inventory
            result = self.client.read(target, 'issues')
        self.assertNotIn('999', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
