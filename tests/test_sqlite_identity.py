from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import Mock, patch

from forge.client_connections import ClientConnections, ConnectionError
from forge.client_repositories import ClientRepositories
from forge.operator_auth import OperatorAuth
from forge.sqlite_identity import identity_effect_lock
from tests import test_client_connections as connection_tests
from tests.sqlite_fixture import selected_store  # noqa: F401 -- re-exported for other test modules


class SqliteConnectionTests(connection_tests.Fixture):
    """SQLite-specific connection behaviour; the shared conformance suite is
    test_client_connections.ConnectionTests, which already runs on a selected store."""
    def test_base_names_resolve_to_selected_store(self):
        self.assertIsInstance(self.auth, OperatorAuth)
        self.assertIsInstance(self.service, ClientConnections)

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


class SqliteRepositoryOperationTests(connection_tests.Fixture):
    # Connection conformance runs in test_client_connections.ConnectionTests;
    # this class reuses only its selected-store fixture and approval helpers.
    def setUp(self):
        super().setUp()
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
        self.assertIsInstance(self.operations, ClientRepositories)
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
