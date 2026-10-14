from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from forge.client_connections import ClientConnections, CAPABILITIES, ConnectionError
from forge.client_repositories import ClientRepositories
from forge.operator_auth import OperatorAuth, AuthError, SESSION_COOKIE, FLOW_COOKIE, read_cookie, digest
from forge.sqlite_identity import (SqliteOperatorAuth, SqliteClientConnections, SqliteClientRepositories,
                                    identity_effect_lock)
from forge.store.db import Store, StoreError
from forge.store.files import StateError
from tests import test_client_connections as connection_tests
from tests import test_operator_auth as auth_tests


def selected_store(root):
    store = Store.create(root)
    with store.write() as db:
        db.executemany('INSERT INTO store_metadata VALUES(?,?)', [('runtime_root', str(root)), ('runtime_active', 'true')])
    marker = Path(root) / 'state/storage'
    marker.write_text('forge-sqlite-v1\n' + str(store.root) + '\n' + store.instance_id + '\n')
    marker.chmod(0o600)
    return store


class SqliteConnectionTests(connection_tests.ConnectionTests):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.database = selected_store(self.root)
        self.now, self.generation = 1000, 'generation-one'
        self.auth = OperatorAuth(self.root, clock=lambda: self.now, identity=lambda *args: {'id': 123, 'login': 'operator'})
        self.auth.configure('https://forge.example', 'client-id', 's' * 40, [123])
        url, flow_cookie = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.session = read_cookie(self.auth.callback(state, 'code', read_cookie(flow_cookie, FLOW_COOKIE)), SESSION_COOKIE)
        self.owner_check, self.identity = Mock(), Mock(return_value=88)
        self.service = ClientConnections(self.root, auth=self.auth, generation=lambda: self.generation,
                                          owner_check=self.owner_check, identity=self.identity)
        self.target = {'id': 321, 'name': 'example/project'}
        self.request = {'client_id': 'forge-cli', 'name': 'Local agent', 'capabilities': list(CAPABILITIES),
                        'scope': {'repositories': [self.target], 'owners': []}}

    def test_matching_code_approval_tokens_and_secret_free_listing(self):
        self.assertIsInstance(self.auth, SqliteOperatorAuth)
        self.assertIsInstance(self.service, SqliteClientConnections)
        device = self.begin()
        self.assertEqual(self.service.preview(self.session, device['user_code'])['scope'], self.request['scope'])
        self.approve(device)
        self.now += 5
        token = self.service.poll({'client_id': 'forge-cli', 'device_code': device['device_code']})
        self.service.authorize(token['access_token'], self.target, 'repositories:activate', device['instance'], device['generation'])
        listing = json.dumps(self.service.view(self.session))
        with self.database.read_snapshot() as db:
            saved = '\n'.join(db.iterdump())
        for value in (device['device_code'], token['access_token'], token['refresh_token'], 's' * 40):
            self.assertNotIn(value, listing)
            self.assertNotIn(value, saved)
        self.assertFalse((self.root / 'state/operator-auth').exists())
        self.now += 1
        self.assertEqual(self.service.status(token['access_token'])['last_used'], self.now)

    def test_refresh_race_revokes_the_rotated_family(self):
        token = self.token()
        def refresh():
            try:
                return self.service.refresh(token['refresh_token'])
            except ConnectionError as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: refresh(), range(2)))
        issued = [value for value in results if isinstance(value, dict)]
        self.assertEqual(len(issued), 1)
        self.assertIn('invalid_grant', results)
        self.assertError('invalid_token', lambda: self.service.status(issued[0]['access_token']))
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM consumed_refresh_tokens').fetchone()[0], 1)

    def test_revocation_serializes_with_effect_and_lock_is_reentrant(self):
        token = self.token()
        entered, released = threading.Event(), threading.Event()
        def revoke():
            entered.set()
            self.service.revoke(self.session, token['connection']['id'])
            released.set()
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.service.effect_guard(token['access_token'], token['connection']['instance'], self.generation):
                with identity_effect_lock(self.root):
                    pending = pool.submit(revoke)
                    self.assertTrue(entered.wait(2))
                    self.assertFalse(released.is_set())
                    with self.database.write() as db:
                        db.execute("INSERT INTO store_metadata VALUES('simulated_config_effect','committed')")
            pending.result(timeout=2)
        self.assertTrue(released.is_set())
        self.assertError('invalid_token', lambda: self.service.status(token['access_token']))

    def test_imported_opaque_epoch_fence_cannot_be_renumbered_into_validity(self):
        token = self.token()
        with self.database.write() as db:
            db.execute("UPDATE operator_auth_configuration SET source_epoch='different' WHERE singleton=1")
        self.assertError('invalid_token', lambda: self.service.status(token['access_token']))
        self.assertError('invalid_grant', lambda: self.service.refresh(token['refresh_token']))


class SqliteAuthTests(unittest.TestCase):
    configure = auth_tests.OperatorAuthTests.configure
    begin = auth_tests.OperatorAuthTests.begin
    login = auth_tests.OperatorAuthTests.login
    test_one_use_state_requires_matching_browser = auth_tests.OperatorAuthTests.test_one_use_state_requires_matching_browser
    test_expired_state_is_not_exchanged = auth_tests.OperatorAuthTests.test_expired_state_is_not_exchanged
    test_reconfiguration_revokes_sessions_and_pending_flows = auth_tests.OperatorAuthTests.test_reconfiguration_revokes_sessions_and_pending_flows
    test_consumed_state_stays_consumed_when_identity_lookup_fails = auth_tests.OperatorAuthTests.test_consumed_state_stays_consumed_when_identity_lookup_fails
    test_configuration_requires_https_and_explicit_ids = auth_tests.OperatorAuthTests.test_configuration_requires_https_and_explicit_ids

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = selected_store(Path(self.temp.name))
        self.now = 1000
        self.identity = Mock(return_value={'id': 123, 'login': 'operator'})
        self.auth = OperatorAuth(self.temp.name, clock=lambda: self.now, identity=self.identity)
        self.configure()

    def test_reconfiguration_during_exchange_fails_closed_without_network_transaction(self):
        def change(*args):
            with self.database.write() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM operator_flows WHERE consumed_at IS NOT NULL').fetchone()[0], 1)
            self.configure(operators=[456])
            return {'id': 123, 'login': 'operator'}
        self.identity.side_effect = change
        with self.assertRaises(AuthError):
            self.login()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operator_sessions').fetchone()[0], 0)

    def test_session_csrf_and_audit_survive_restart_without_raw_credentials_in_rows(self):
        token = self.login()
        session = self.auth.session(token)
        other = OperatorAuth(self.temp.name, clock=lambda: self.now)
        self.assertEqual(other.session(token), session)
        self.assertEqual(len(session['csrf']), 43)
        other.audit(session, '/api/repositories/enable')
        with self.database.read_snapshot() as db:
            row = db.execute('SELECT * FROM operator_sessions WHERE token_hash=?', (digest(token),)).fetchone()
            self.assertIsNotNone(row)
            audit = dict(db.execute("SELECT * FROM operator_audit WHERE action='/api/repositories/enable'").fetchone())
        self.assertEqual(audit['operator_id'], '123')
        self.assertNotIn(token, json.dumps(audit))
        self.assertNotIn(session['csrf'], json.dumps(audit))

    def test_missing_or_changed_credential_fails_closed(self):
        with self.database.read_snapshot() as db:
            reference = db.execute('SELECT credential_ref FROM operator_auth_configuration').fetchone()[0]
        path = self.database.root / reference
        path.write_text('t' * 40)
        with self.assertRaises(StateError):
            self.auth.config()
        path.unlink()
        with self.assertRaises(StoreError):
            self.auth.config()

    def test_flow_capacity_counts_only_live_unconsumed_rows(self):
        for _ in range(100):
            self.auth.begin()
        with self.assertRaises(AuthError):
            self.auth.begin()
        self.now += 601
        self.auth.begin()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM operator_flows WHERE expires_at>?', (self.now,)).fetchone()[0], 1)

    def test_import_staging_cannot_mutate_identity(self):
        with self.database.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        with self.assertRaises(StateError):
            self.auth.configure('https://forge.example', 'client-id', 'x' * 40, [123])
        with self.assertRaises(StateError):
            self.auth.begin()


class SqliteRepositoryOperationTests(connection_tests.Fixture):
    # Only this class's operation tests run; the connection conformance is
    # already exercised above. Its fixture/approval helpers are shared below.
    def setUp(self):
        SqliteConnectionTests.setUp(self)
        self.credential = self.token()
        self.config = {'repositories': [{'id': 321, 'name': 'example/project', 'enabled': False}]}
        self.revision = 'a' * 64
        self.requirements = {'revision': self.revision, 'readiness': {'discovered': True, 'access_ready': True, 'protection_ready': True}}
        self.settings = Mock(root=self.root, clock=lambda: self.now)
        self.settings.load.side_effect = lambda: deepcopy(self.config)
        self.settings.refresh.return_value = True
        @contextmanager
        def locked(*args, **kwargs):
            yield
        self.settings.locked.side_effect = locked
        self.setup = Mock()
        self.setup.check.return_value = True
        self.queued = []
        self.operations = ClientRepositories(self.settings, self.service, setup=self.setup, launch=self.queued.append)
        self.assertIsInstance(self.operations, SqliteClientRepositories)
        self.value = dict(repository=self.target, instance=self.credential['connection']['instance'], generation=self.generation,
                          id='1' * 32, action='enable', revision=self.revision)
        self.description = patch('forge.repository_requirements.describe', side_effect=lambda *args: deepcopy(self.requirements))
        self.description.start()
        self.addCleanup(self.description.stop)

    def test_admitted_operation_replay_binds_request_and_contains_no_token(self):
        result = self.operations.operation(self.credential['access_token'], self.value)
        self.assertEqual(result['status'], 'pending')
        self.assertEqual(result, self.operations.operation(self.credential['access_token'], self.value))
        self.assertError('operation_conflict', lambda: self.operations.operation(self.credential['access_token'], {**self.value, 'action': 'disable'}))
        with self.database.read_snapshot() as db:
            rows = list(db.execute('SELECT * FROM client_operations'))
        self.assertEqual(len(rows), 1)
        self.assertNotIn(self.credential['access_token'], rows[0]['payload_json'])
        self.assertFalse((self.root / 'state/client-operations').exists())

    def test_request_receipt_is_separate_from_asynchronous_effect_and_redacted(self):
        self.operations.operation(self.credential['access_token'], self.value)
        self.operations.operation(self.credential['access_token'], self.value)
        self.queued[0]()
        with self.database.read_snapshot() as db:
            requests = [dict(row) for row in db.execute("SELECT * FROM operator_audit WHERE json_extract(details_json,'$.kind')='request_outcome'")]
            effects = [dict(row) for row in db.execute("SELECT * FROM operator_audit WHERE json_extract(details_json,'$.kind')='operation_effect'")]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['outcome'], 'accepted')
        self.assertEqual(requests[0]['connection_id'], self.credential['connection']['id'])
        self.assertEqual(json.loads(requests[0]['details_json'])['command_request_id'], self.value['id'])
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]['request_id'], self.value['id'])
        for secret in (self.credential['access_token'], self.credential['refresh_token'], 's' * 40):
            self.assertNotIn(secret, json.dumps(requests + effects))

    def test_revocation_before_effect_prevents_configuration_write(self):
        self.operations.operation(self.credential['access_token'], self.value)
        self.service.revoke(self.session, self.credential['connection']['id'])
        self.queued[0]()
        self.setup.enable.assert_not_called()
        self.assertEqual(self.operations._get(self.value['id'])['error'], 'invalid_token')

    def test_lost_configuration_receipt_resumes_without_duplicate_write(self):
        from forge.settings import digest as config_digest
        self.operations.operation(self.credential['access_token'], self.value)
        expected = deepcopy(self.config)
        expected['repositories'][0]['enabled'] = True
        def enable(*args):
            with self.database.write() as db:
                db.execute("INSERT INTO store_metadata VALUES('test_config_written','true')")
            self.requirements['revision'] = config_digest(expected)
            raise KeyboardInterrupt()
        self.setup.enable.side_effect = enable
        with self.assertRaises(KeyboardInterrupt):
            self.queued[0]()
        restarted = ClientRepositories(self.settings, self.service, setup=self.setup, launch=lambda fn: fn())
        restarted.operation(self.credential['access_token'], self.value)
        self.assertEqual(restarted._get(self.value['id'])['status'], 'complete')
        self.setup.enable.assert_called_once()


if __name__ == '__main__':
    unittest.main()
