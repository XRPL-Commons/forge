from contextlib import closing
import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from forge.provider_proxy import (Assignment, Broker, ProxyError, SessionServer,
                                  MAX_BODY, MAX_RESPONSE, claude_environment)


class Response:
    status = 200
    def __init__(self, chunks=()): self.chunks = iter(chunks)
    def getheaders(self): return [('content-type', 'text/event-stream'), ('retry-after', '7'), ('anthropic-ratelimit-unified-status', 'allowed'), ('set-cookie', 'never-forward'), ('authorization', 'never-forward')]
    def read1(self, _): return next(self.chunks, b'')


class Connection:
    def __init__(self, response=None):
        self.response = response or Response([b'event: ping\n\n', b'data: {"tool_use":"Read"}\n\n'])
        self.sock = Mock()
        self.calls = []
        self.closed = False
    def connect(self): pass
    def request(self, *args, **kwargs): self.calls.append((args, kwargs))
    def getresponse(self): return self.response
    def close(self): self.closed = True


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path): super().__init__('localhost', timeout=3); self.path = path
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(self.path)


class ProxyTests(unittest.TestCase):
    def setUp(self):
        self.assignment = Assignment('job-a', 'account-generation-a', 'claude-sonnet-4-6', 1000, 'job-capability')
        self.authorized = Mock(return_value=True)
        self.key = Mock(return_value='host-only-api-key')
        self.connection = Connection()
        self.broker = Broker(self.assignment, api_key=self.key, authorized=self.authorized,
                             clock=lambda: 10, connection=lambda: self.connection)
        self.body = json.dumps({'model': self.assignment.model, 'stream': True, 'tools': [{'name':'Read'}], 'messages': []}).encode()
        self.headers = {'authorization':'Bearer job-capability', 'x-api-key':'worker-injected-key', 'cookie':'worker-cookie', 'host':'evil.example', 'anthropic-beta':'new-capability', 'anthropic-version':'2023-06-01', 'x-claude-code-session-id':'session-a', 'content-type':'application/json'}

    def test_forwards_unchanged_body_stream_and_capability_headers_with_host_key(self):
        with self.broker.open('POST', '/v1/messages?beta=true', self.headers, self.body) as (status, headers, chunks):
            self.assertEqual(status, 200)
            self.assertEqual(b''.join(chunks), b'event: ping\n\ndata: {"tool_use":"Read"}\n\n')
            self.assertEqual(headers, {'content-type':'text/event-stream','retry-after':'7','anthropic-ratelimit-unified-status':'allowed'})
        args, kwargs = self.connection.calls[0]
        self.assertEqual(args, ('POST', '/v1/messages?beta=true'))
        self.assertEqual(kwargs['body'], self.body)
        self.assertEqual(kwargs['headers']['x-api-key'], 'host-only-api-key')
        self.assertEqual(kwargs['headers']['anthropic-beta'], 'new-capability')
        self.assertEqual(kwargs['headers']['x-claude-code-session-id'], 'session-a')
        for name in ('authorization','cookie','host'):
            self.assertNotIn(name, kwargs['headers'])
        self.assertTrue(self.connection.closed)
        self.authorized.assert_called_with(self.assignment)

    def test_rejects_other_job_capability_before_loading_key(self):
        with self.assertRaisesRegex(ProxyError, 'execution_unavailable'):
            self.broker.admit('POST', '/v1/messages', {**self.headers, 'authorization':'Bearer other-job'}, self.body)
        self.key.assert_not_called()

    def test_revoked_account_or_fenced_job_rejected(self):
        self.authorized.return_value = False
        with self.assertRaisesRegex(ProxyError, 'execution_unavailable'):
            self.broker.admit('POST', '/v1/messages', self.headers, self.body)
        self.key.assert_not_called()

    def test_expiry_rejected(self):
        self.broker.clock = lambda: 1000
        with self.assertRaisesRegex(ProxyError, 'execution_unavailable'):
            self.broker.admit('POST', '/v1/messages', self.headers, self.body)

    def test_no_arbitrary_host_route_query_or_model(self):
        for path in ('https://evil.example/v1/messages', '/v1/messages?target=evil', '/v1/models', '/oauth/token', '//api.anthropic.com/v1/messages'):
            with self.subTest(path=path), self.assertRaisesRegex(ProxyError, 'route_not_allowed'):
                self.broker.admit('POST', path, self.headers, self.body)
        with self.assertRaisesRegex(ProxyError, 'model_not_allowed'):
            self.broker.admit('POST', '/v1/messages', self.headers, b'{"model":"other"}')
        self.key.assert_not_called()

    def test_count_tokens_retains_model_constraint(self):
        self.assertEqual(self.broker.admit('POST','/v1/messages/count_tokens',self.headers,self.body)['x-api-key'],'host-only-api-key')

    def test_size_and_invalid_json_rejected(self):
        for body, reason in ((b'a'*(MAX_BODY+1),'request_too_large'), (b'no','invalid_request'), (b'[]','model_not_allowed')):
            with self.subTest(reason=reason), self.assertRaisesRegex(ProxyError, reason):
                self.broker.admit('POST', '/v1/messages', self.headers, body)
        self.key.assert_not_called()

    def test_revocation_stops_existing_stream_before_next_chunk(self):
        with self.broker.open('POST','/v1/messages',self.headers,self.body) as (_,_,chunks):
            self.assertEqual(next(chunks), b'event: ping\n\n')
            self.authorized.return_value = False
            with self.assertRaisesRegex(ProxyError, 'execution_unavailable'): next(chunks)

    def test_watchdog_interrupts_idle_upstream_on_fence(self):
        with self.broker.open('POST','/v1/messages',self.headers,self.body):
            self.authorized.return_value = False
            for _ in range(30):
                if self.connection.sock.shutdown.called: break
                time.sleep(.02)
            self.connection.sock.shutdown.assert_called_once_with(socket.SHUT_RDWR)

    def test_request_deadline_stops_stream_even_when_job_remains_live(self):
        with self.broker.open('POST','/v1/messages',self.headers,self.body) as (_,_,chunks):
            self.broker.clock = lambda: 611
            with self.assertRaisesRegex(ProxyError, 'execution_unavailable'): next(chunks)

    def test_refuses_redirect_without_following(self):
        self.connection.response.status=307
        with self.assertRaisesRegex(ProxyError, 'upstream_redirect_refused'):
            with self.broker.open('POST','/v1/messages',self.headers,self.body): pass
        self.assertEqual(len(self.connection.calls),1)

    def test_response_size_bounded(self):
        self.connection.response=Response([b'x'*(MAX_RESPONSE+1)])
        with self.broker.open('POST','/v1/messages',self.headers,self.body) as (_,_,chunks):
            with self.assertRaisesRegex(ProxyError,'response_too_large'): next(chunks)

    def test_upstream_error_status_body_and_retry_metadata_preserved(self):
        self.connection.response=Response([b'{"error":{"type":"rate_limit_error"}}'])
        self.connection.response.status=429
        with self.broker.open('POST','/v1/messages',self.headers,self.body) as (status,headers,chunks):
            self.assertEqual(status,429)
            self.assertEqual(headers['retry-after'],'7')
            self.assertEqual(b''.join(chunks),b'{"error":{"type":"rate_limit_error"}}')

    def test_worker_environment_removes_provider_credentials(self):
        original={key:'real-secret' for key in ('ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_OAUTH_TOKEN','FORGE_HARNESS_AUTH','FORGE_AUTH_DIGEST')}
        env=claude_environment({**original,'PATH':'/usr/bin'},'http://forge-provider:8082','job-capability',auth_mode='api_key')
        self.assertNotIn('real-secret',env.values())
        self.assertEqual(env,{'PATH':'/usr/bin','ANTHROPIC_BASE_URL':'http://forge-provider:8082','ANTHROPIC_AUTH_TOKEN':'job-capability'})

    def test_browser_login_cannot_silently_switch_to_api_billing(self):
        with self.assertRaisesRegex(ProxyError, 'auth_mode_not_supported'):
            claude_environment({}, 'http://forge-provider:8082', 'job-capability', auth_mode='browser')

    def test_http_unix_socket_transports_stream_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'job.sock')
            with SessionServer(path,self.broker) as server:
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    with closing(UnixConnection(path)) as client:
                        client.request('POST','/v1/messages?beta=true',self.body,self.headers)
                        response=client.getresponse()
                        self.assertEqual(response.status,200)
                        self.assertEqual(response.read(),b'event: ping\n\ndata: {"tool_use":"Read"}\n\n')
                    self.assertEqual(Path(path).stat().st_mode & 0o777,0o600)
                finally: server.shutdown();thread.join()

    def test_http_oversized_body_rejected_before_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path=str(Path(directory)/'job.sock')
            with SessionServer(path,self.broker) as server:
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    with closing(UnixConnection(path)) as client:
                        client.putrequest('POST','/v1/messages')
                        client.putheader('Content-Length',str(MAX_BODY+1))
                        client.putheader('Authorization','Bearer job-capability');client.endheaders()
                        self.assertEqual(client.getresponse().status,413)
                    self.key.assert_not_called()
                finally: server.shutdown();thread.join()


if __name__ == '__main__': unittest.main()
