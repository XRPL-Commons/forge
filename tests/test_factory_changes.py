"""Committed sequence delivery, bounded resync and idle long-poll isolation."""
import json
import socket
import unittest
from unittest.mock import Mock, patch

from forge.factory.changes import Changes, ChangeFeedCancelled, MAX_EVENTS, parse_cursor, socket_eof
from forge.factory.config import validate
from forge.factory.store_views import PipelineViews
from tests import test_store_result_controller as fixtures


class FactoryChangesTests(unittest.TestCase):
    setUp = fixtures.ResultControllerTests.setUp
    command = fixtures.ResultControllerTests.command
    view = fixtures.ResultControllerTests.view
    reserve = fixtures.ResultControllerTests.reserve

    def views(self):
        return PipelineViews(self.store, runtime=validate({}), clock=lambda:100)

    def test_bootstrap_and_committed_events_share_current_projections(self):
        feed = Changes(self.views())
        first = feed.read(wait=0)
        self.assertTrue(first['resync'])
        self.assertEqual(first['cursor'], first['board']['cursor'])
        self.life.open_gate(self.command(), 'label_hold', details={'reason':'secret diagnostics'})
        changed = feed.read(since=first['cursor'], wait=0)
        self.assertFalse(changed['resync'])
        self.assertGreater(parse_cursor(changed['cursor'])[1], parse_cursor(first['cursor'])[1])
        self.assertEqual(changed['issues']['i']['status'], 'blocked')
        self.assertTrue(changed['events'])
        self.assertTrue(all(set(event) == {'sequence','unit_id','issue_id','job_id','kind','at'} for event in changed['events']))
        self.assertNotIn('secret diagnostics', json.dumps(changed['events']))
        self.assertNotIn('body', changed['issues']['i'])
        self.assertEqual(changed['cursor'], changed['board']['cursor'])
        same = feed.read(since=changed['cursor'], wait=0)
        self.assertEqual(same['events'], [])
        self.assertEqual(same['issues'], {})
        self.assertFalse(same['resync'])

    def test_wait_has_no_snapshot_and_observes_committed_transition(self):
        start = Changes(self.views()).read(wait=0)['cursor']
        sleeps = []
        def sleep(delay):
            self.assertIsNone(self.store._reader.get())
            sleeps.append(delay)
            self.life.open_gate(self.command(), 'label_hold', details={})
        result = Changes(self.views(), monotonic=lambda:0, sleep=sleep).read(since=start)
        self.assertEqual(sleeps, [0.25])
        self.assertTrue(result['events'])
        self.assertEqual(result['issues']['i']['status'], 'blocked')

    def test_disconnected_proxy_stops_wait_before_projection(self):
        views = self.views()
        start = Changes(views).read(wait=0)['cursor']
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        self.assertFalse(socket_eof(server))
        sleeps = []
        def sleep(delay):
            sleeps.append(delay)
            client.close()
        with patch.object(views, 'factory', side_effect=AssertionError('must not project a disconnected response')):
            with self.assertRaises(ChangeFeedCancelled):
                Changes(views, sleep=sleep).read(since=start, cancelled=lambda: socket_eof(server))
        self.assertEqual(sleeps, [0.25])
        self.assertIsNone(self.store._reader.get())

    def test_socket_peek_does_not_consume_pipelined_request(self):
        server, client = socket.socketpair()
        self.addCleanup(server.close)
        self.addCleanup(client.close)
        client.sendall(b'GET /next HTTP/1.1\r\n')
        self.assertFalse(socket_eof(server))
        self.assertEqual(server.recv(100), b'GET /next HTTP/1.1\r\n')

    def test_idle_timeout_refreshes_global_observation_without_events(self):
        start = Changes(self.views()).read(wait=0)['cursor']
        now = [0]
        def sleep(delay):
            now[0] += delay
            with self.store.write() as db:
                db.execute("INSERT OR REPLACE INTO store_metadata VALUES('supervisor:heartbeat',?)",
                           (json.dumps({'finished_at':99,'errors':{}}),))
        result = Changes(self.views(), monotonic=lambda:now[0], sleep=sleep).read(since=start, wait=0.5)
        self.assertEqual(now[0], 0.5)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['board']['observation']['at'], 99)
        self.assertEqual(result['cursor'], start)

    def test_overflow_wrong_instance_and_future_cursor_request_resync(self):
        feed = Changes(self.views())
        start = feed.read(wait=0)['cursor']
        with self.store.write() as db:
            db.executemany("INSERT INTO events(transaction_id,unit_id,kind,at,payload_json) VALUES(?,'u','issue_observed',100,'{}')",
                           [(str(i),) for i in range(MAX_EVENTS + 1)])
        for previous in (start, 'another:0', f'{self.store.instance_id}:9223372036854775807'):
            result = feed.read(since=previous, wait=0)
            self.assertTrue(result['resync'])
            self.assertEqual(result['events'], [])
            self.assertEqual(result['issues'], {})
            self.assertEqual(result['cursor'], result['board']['cursor'])

    def test_invalid_cursor_and_wait_fail_before_waiting(self):
        feed = Changes(self.views(), sleep=Mock(side_effect=AssertionError('must not wait')))
        for previous in ('', '1', 'instance:-1', 'instance:1.0', 'instance:١', 'instance:9223372036854775808'):
            with self.assertRaises(ValueError):feed.read(since=previous)
        for wait in (-1, 26, float('nan'), float('inf'), True, '1'):
            with self.assertRaises(ValueError):feed.read(wait=wait)

    def test_issue_detail_contains_worker_metadata_without_second_factory_read(self):
        job = self.reserve()
        view = self.views().issues(key='i')
        self.assertEqual(view['jobs'][0]['id'], job)
        self.assertNotIn('logs', view['jobs'][0])
        card = self.views().factory(issue='i')['issues'][0]
        self.assertEqual(view['factory_issue'], card)
        self.assertEqual(view['factory_issue']['attempts'][0]['id'], job)
        self.assertIn('deployments', view)
        self.assertEqual(view['cursor'], self.views().factory(section='board')['cursor'])


class FactoryChangesHTTPTests(unittest.TestCase):
    def handler(self, path='/api/factory/changes?wait=0'):
        from tests.test_dashboard import dashboard
        handler = object.__new__(dashboard.H)
        handler.path = path
        handler._action_token = 'operator-token'
        handler._factory_paused = False
        for name in ('_trusted_host', '_upstream', '_operator_guard', '_signed_in'):
            setattr(handler, name, Mock(return_value=True))
        handler._auth_get = Mock(return_value=False)
        handler._send = Mock()
        return handler

    def test_changes_never_bypass_auth_and_recheck_session_after_wait(self):
        from tests.test_dashboard import dashboard
        result = dict(cursor='instance:1', events=[], issues={}, board={}, resync=False)
        with patch.object(dashboard, 'runtime_control', return_value=False), \
                patch('forge.factory.control.public_views', return_value=Mock()), \
                patch('forge.factory.changes.Changes.read', return_value=result) as read:
            denied = self.handler()
            denied._signed_in.return_value = False
            denied.do_GET()
            read.assert_not_called()
            expired = self.handler()
            expired._signed_in.side_effect = [True, False]
            expired.do_GET()
            expired._send.assert_not_called()
            self.assertEqual(expired._operator_guard.call_count, 2)
            allowed = self.handler()
            allowed.do_GET()
            body = json.loads(allowed._send.call_args.args[0])
            self.assertEqual(body['board'], dict(ok=True, controls_token='operator-token'))

    def test_disconnected_wait_sends_no_response(self):
        from tests.test_dashboard import dashboard
        with patch.object(dashboard, 'runtime_control', return_value=False), \
                patch('forge.factory.control.public_views', return_value=Mock()), \
                patch('forge.factory.changes.Changes.read', side_effect=ChangeFeedCancelled) as read:
            handler = self.handler()
            handler.do_GET()
            self.assertTrue(callable(read.call_args.kwargs['cancelled']))
            handler._send.assert_not_called()

    def test_invalid_query_and_unavailable_store_have_explicit_errors(self):
        from tests.test_dashboard import dashboard
        with patch.object(dashboard, 'runtime_control', return_value=False), \
                patch('forge.factory.control.public_views', side_effect=OSError('store unavailable')) as views:
            for query in ('?wait=0&unknown=yes', '?since=', '?wait=0&wait=1'):
                handler = self.handler('/api/factory/changes'+query)
                handler.do_GET()
                self.assertEqual(handler._send.call_args.kwargs['status'], 400)
            views.assert_not_called()
            handler = self.handler()
            handler.do_GET()
            self.assertEqual(handler._send.call_args.kwargs['status'], 503)
