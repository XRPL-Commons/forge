from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from forge.mutation_audit import MutationAudit, RequestReplay, config_change_in, request_context
from forge.operator_auth import OperatorAuth, FLOW_COOKIE, SESSION_COOKIE, digest, read_cookie
from tests.test_dashboard import dashboard
from tests.test_settings_http import SettingsHTTPTests
from tests.test_sqlite_identity import selected_store

REQUEST = 'a75b2d4e-6861-44f1-a9cc-6c44ba21092d'
SECRET = 'credential-that-must-never-enter-audit'


class MutationAuditTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = selected_store(self.root)

    def context(self, route='/api/runtime'):
        context = MutationAudit(self.store, route, request_id=REQUEST)
        context.details['actor'] = 'local:control-capability'
        context.describe({'revision': 'a' * 64, 'config': {'token': SECRET}, 'reason': SECRET})
        return context

    def rows(self, kind='request_outcome'):
        with self.store.read_snapshot() as db:
            return [dict(row) for row in db.execute("SELECT * FROM operator_audit WHERE json_extract(details_json,'$.kind')=? ORDER BY sequence", (kind,))]

    def test_success_has_one_receipt_retry_cannot_reexecute_and_payload_is_excluded(self):
        context = self.context()
        with request_context(context):
            context.start()
            context.result({'ok': True, 'secret': SECRET, 'request': REQUEST})
            context.finish()
        with self.assertRaises(RequestReplay) as replay:
            self.context().start()
        self.assertEqual(replay.exception.receipt['outcome'], 'accepted')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        details = json.loads(rows[0]['details_json'])
        self.assertEqual(details['command_request_id'], REQUEST)
        self.assertEqual(details['expected_revision'], 'a' * 64)
        self.assertNotIn(SECRET, json.dumps(rows + self.rows('request_started')))
        self.assertNotIn('payload_digest', details)

    def test_concurrent_duplicate_claim_dispatches_once(self):
        barrier = threading.Barrier(2)
        release = threading.Event()
        def dispatch():
            context = self.context()
            barrier.wait(timeout=3)
            try:
                with request_context(context):
                    context.start()
                    release.wait(timeout=3)
                    context.result({'ok': True})
                    context.finish()
                    return 'dispatched'
            except RequestReplay as exc:
                release.set()
                return exc.receipt['outcome']
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: dispatch(), range(2)))
        self.assertEqual(sorted(results), ['dispatched', 'in_progress'])
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(len(self.rows('request_started')), 1)

    def test_crash_after_claim_becomes_unknown_and_late_completion_cannot_change_it(self):
        context = self.context()
        context.start()
        context.release()  # Process ended without a final response.
        for _ in range(2):
            with self.assertRaises(RequestReplay) as replay:
                self.context().start()
            self.assertEqual(replay.exception.receipt['outcome'], 'unknown')
        context.result({'ok': True})
        context.finish()
        self.assertEqual([r['outcome'] for r in self.rows()], ['unknown'])
        self.assertEqual(json.loads(self.rows()[0]['details_json'])['reason_code'], 'request_interrupted')

    def test_config_effect_is_digest_only_and_postcommit_failure_is_unknown(self):
        context = self.context()
        with request_context(context):
            context.start()
            with self.store.write() as db:
                config_change_in(db, domain='runtime.json', subject='instance', before=None,
                                 after={'token': SECRET}, revision=1)
            context.failed('operation_interrupted', uncertain=True)
            context.result({'ok': False, 'reason': SECRET})
            context.finish()
        self.assertEqual(self.rows()[0]['outcome'], 'unknown')
        changes = self.rows('configuration_change')
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0]['request_id'], self.rows()[0]['request_id'])
        self.assertNotIn(SECRET, json.dumps(changes + self.rows()))
        details = json.loads(changes[0]['details_json'])
        self.assertEqual(len(details['before_digest']), 64)
        self.assertEqual(len(details['after_digest']), 64)

    def test_metadata_rejects_secret_values_and_hostile_shapes(self):
        context = self.context()
        context.describe({'action': ['invalid'], 'key': SECRET, 'repo_id': SECRET,
                          'revision': SECRET, 'request_id': SECRET})
        context.result({'ok': False, 'error': SECRET, 'request': SECRET})
        context.finish()
        self.assertEqual(self.rows()[0]['outcome'], 'rejected')
        self.assertNotIn(SECRET, json.dumps(self.rows()))

    def test_audit_records_are_append_only(self):
        context = self.context()
        context.result({'ok': False})
        context.finish()
        for statement in ('DELETE FROM operator_audit', "UPDATE operator_audit SET outcome='accepted'"):
            with self.assertRaises(Exception):
                with self.store.write() as db:
                    db.execute(statement)
        self.assertEqual(self.rows()[0]['outcome'], 'rejected')


class MutationHTTPTests(MutationAuditTests):
    def setUp(self):
        super().setUp()
        patcher = patch.object(dashboard, 'WD', str(self.root))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.auth = OperatorAuth(self.root, identity=lambda *args: {'id': 123, 'login': 'operator'})
        self.auth.configure('https://forge.example', 'client-id', 's' * 40, [123])
        url, flow = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.token = read_cookie(self.auth.callback(state, 'code', read_cookie(flow, FLOW_COOKIE)), SESSION_COOKIE)
        self.session = self.auth.session(self.token)

    def request(self, path, value, *, signed=True, **headers):
        if signed:
            headers = {'Cookie': SESSION_COOKIE + '=' + self.token, 'X-Forge-Action': self.session['csrf'], **headers}
        handler = SettingsHTTPTests.handler(self, path, value, **{'X-Forwarded-Host': 'forge.example', **headers})
        handler.command = 'POST'
        handler.request_version = 'HTTP/1.1'
        handler.requestline = 'POST ' + path
        handler.wfile = io.BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler._send = dashboard.H._send.__get__(handler)
        handler.send_error = dashboard.H.send_error.__get__(handler)
        handler.do_POST()
        return handler

    def test_authenticated_success_and_validation_rejection_have_session_and_no_secrets(self):
        with patch.object(dashboard, 'settings_service') as settings:
            self.request('/api/settings/sync', {})
            self.request('/api/settings/sync', {'token': SECRET})
        rows = self.rows()
        self.assertEqual([r['outcome'] for r in rows], ['accepted', 'rejected'])
        for row in rows:
            self.assertEqual(row['operator_id'], '123')
            self.assertEqual(row['session_id'], digest(self.token))
        settings.return_value.start_refresh.assert_called_once()
        for secret in (SECRET, self.token, self.session['csrf']):
            self.assertNotIn(secret, json.dumps(rows))

    def test_http_exception_after_effect_is_unknown_and_retry_never_runs_it_again(self):
        def effect():
            with self.store.write() as db:
                config_change_in(db, domain='factory.json', subject='instance', before=None,
                                 after={'private': SECRET}, revision=1)
            raise OSError(SECRET)
        with patch.object(dashboard, 'settings_service') as settings:
            settings.return_value.start_refresh.side_effect = effect
            self.request('/api/settings/sync', {}, **{'X-Forge-Request-ID': REQUEST})
            handler = self.request('/api/settings/sync', {}, **{'X-Forge-Request-ID': REQUEST})
        settings.return_value.start_refresh.assert_called_once()
        handler.send_response.assert_called_once_with(409)
        self.assertEqual(json.loads(handler.wfile.getvalue())['receipt']['outcome'], 'unknown')
        self.assertEqual([r['outcome'] for r in self.rows()], ['unknown'])
        self.assertNotIn(SECRET, json.dumps(self.rows()))

    def test_interrupted_retry_preserves_original_claim_after_body_change_and_relogin(self):
        original_token = self.token
        original = MutationAudit(self.store, '/api/issues/action', request_id=REQUEST)
        original.bind(session=self.session, session_id=digest(original_token))
        original.describe({'key': '123:456', 'revision': 'a' * 64, 'action': 'pause', 'reason': SECRET})
        original.start()
        original.release()  # The original process stopped without its receipt.
        saved_claim = self.rows('request_started')[0]

        self.auth.logout(original_token)
        url, flow = self.auth.begin()
        state = parse_qs(urlsplit(url).query)['state'][0]
        self.token = read_cookie(self.auth.callback(state, 'code', read_cookie(flow, FLOW_COOKIE)), SESSION_COOKIE)
        self.session = self.auth.session(self.token)
        self.assertNotEqual(digest(self.token), saved_claim['session_id'])

        with patch('forge.factory.issue_actions.decide') as decide:
            handler = self.request('/api/issues/action',
                {'key': '123:789', 'revision': 'b' * 64, 'action': 'resume', 'reason': 'different'},
                **{'X-Forge-Request-ID': REQUEST})
        decide.assert_not_called()
        handler.send_response.assert_called_once_with(409)
        self.assertEqual(json.loads(handler.wfile.getvalue())['receipt']['outcome'], 'unknown')
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        terminal = rows[0]
        for field in ('operator_id', 'session_id', 'connection_id', 'request_id', 'action'):
            self.assertEqual(terminal[field], saved_claim[field])
        self.assertEqual(json.loads(terminal['details_json']),
                         {**json.loads(saved_claim['details_json']), 'kind': 'request_outcome',
                          'reason_code': 'request_interrupted'})
        self.assertNotIn(SECRET, json.dumps(terminal))
        self.assertNotIn(original_token, json.dumps(terminal))
        self.assertNotIn(self.token, json.dumps(terminal))

    def test_failed_auth_and_csrf_are_rejected_without_dispatch(self):
        with patch.object(dashboard, 'settings_service') as settings:
            self.request('/api/settings/sync', {}, signed=False)
            self.request('/api/settings/sync', {}, **{'X-Forge-Action': SECRET})
        settings.assert_not_called()
        rows = self.rows()
        self.assertEqual([r['outcome'] for r in rows], ['rejected', 'rejected'])
        self.assertEqual([json.loads(r['details_json'])['reason_code'] for r in rows], ['http_401', 'http_403'])
        self.assertNotIn(SECRET, json.dumps(rows))
