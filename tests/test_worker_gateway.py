"""Actual HTTP requests prove registry mutations and GitHub CONNECT fail closed."""
import importlib.util
import io
import http.client
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import socket
import socketserver
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch, Mock

spec=importlib.util.spec_from_file_location('worker_gateway',Path(__file__).parents[1]/'deploy/runner/gateway.py')
gateway=importlib.util.module_from_spec(spec);spec.loader.exec_module(gateway)


class WorkerGatewayTests(unittest.TestCase):
    def request(self,mode,request, **configuration):
        with gateway.Server(('127.0.0.1',0),mode) as server:
            for key, value in configuration.items(): setattr(server, key, value)
            thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01});thread.start()
            try:
                with socket.create_connection(server.server_address,timeout=2) as peer:
                    peer.sendall(request)
                    result=b''
                    while data:=peer.recv(65536):result+=data
                return result
            finally:server.shutdown();thread.join()

    def test_registry_methods_and_body_are_denied_before_any_upstream(self):
        with patch.object(gateway,'connect_public') as connect:
            for method in ('POST','PUT','DELETE','PATCH','CONNECT'):
                with self.subTest(method=method):
                    result=self.request('registry',f'{method} /registry.npmjs.org/package HTTP/1.1\r\nHost: forge-registry\r\n\r\n'.encode())
                    self.assertIn(b'403 Forbidden',result)
            self.assertIn(b'403 Forbidden',self.request('registry',b'GET /pypi.org/simple/ HTTP/1.1\r\nContent-Length: 1\r\n\r\n'))
            connect.assert_not_called()

    def test_github_direct_hosts_and_provider_suffix_spoofs_are_explicitly_denied(self):
        with patch.object(gateway,'connect_public') as connect:
            for host in ('github.com','api.github.com','codeload.github.com','raw.githubusercontent.com',
                         'registry.npmjs.org','api.openai.com.evil.test','127.0.0.1','169.254.169.254'):
                with self.subTest(host=host):
                    result=self.request('provider',f'CONNECT {host}:443 HTTP/1.1\r\nHost: {host}\r\n\r\n'.encode())
                    self.assertIn(b'HTTP/1.1 403 Forbidden\r\n',result)
            connect.assert_not_called()

    def test_registry_paths_and_redirects_cannot_escape_allowlist(self):
        for target in ('/github.com/a','//pypi.org/a','/pypi.org:443/a','/pypi.org@github.com/a',
                       '/pypi.org/a\r\nX: bad','/pypi.org\\github.com/a'):
            with self.subTest(target=target),self.assertRaises(ValueError):gateway.registry_target(target)
        for target in ('https://github.com/repo','http://pypi.org/a','https://u:p@pypi.org/a','https://pypi.org:444/a'):
            with self.subTest(target=target),self.assertRaises(ValueError):gateway.registry_url(target)
        self.assertEqual(gateway.registry_target('/registry.npmjs.org/@scope%2fpkg'),('registry.npmjs.org','/@scope%2fpkg'))

    def test_metadata_keeps_packages_on_readonly_route_and_descriptions_unchanged(self):
        value=gateway.metadata(b'{"dist":{"tarball":"https://registry.npmjs.org/a/-/a.tgz"},"homepage":"https://github.com/example/a"}', 'application/json')
        self.assertIn(b'http://forge-registry:8081/registry.npmjs.org/a/-/a.tgz',value)
        self.assertIn(b'https://github.com/example/a',value)
        html=gateway.metadata(b'<a href="https://files.pythonhosted.org/a.whl#sha256=abc">a</a>','text/html')
        self.assertIn(b'http://forge-registry:8081/files.pythonhosted.org/a.whl#sha256=abc',html)

    def test_public_resolution_checks_every_address(self):
        address=lambda ip:(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))
        self.assertEqual(gateway.destinations('api.openai.com',lambda *a,**k:[address('8.8.8.8')])[0][4][0],'8.8.8.8')
        for ip in ('127.0.0.1','10.1.2.3','169.254.169.254','192.168.1.1'):
            with self.subTest(ip=ip),self.assertRaises(ValueError):
                gateway.destinations('api.openai.com',lambda *a,**k:[address('8.8.8.8'),address(ip)])

    def upstream(self, payload):
        sock=Mock();sock.makefile.return_value=io.BytesIO(payload)
        response=http.client.HTTPResponse(sock);response.begin()
        connection=Mock();connection.getresponse.return_value=response;connection.close.side_effect=response.close
        return connection

    def test_get_metadata_uses_fixed_host_without_worker_credentials(self):
        body=b'{"dist":{"tarball":"https://registry.npmjs.org/a.tgz"}}'
        connection=self.upstream(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        with patch.object(gateway,'PublicHTTPS',return_value=connection) as create:
            result=self.request('registry',b'GET /registry.npmjs.org/a HTTP/1.1\r\nAuthorization: worker-secret\r\nCookie: session=secret\r\n\r\n')
        self.assertIn(b'200 OK',result)
        self.assertIn(b'http://forge-registry:8081/registry.npmjs.org/a.tgz',result)
        self.assertEqual(create.call_args.args,('registry.npmjs.org',))
        method,path=connection.request.call_args.args
        self.assertEqual((method,path),('GET','/a'))
        sent=connection.request.call_args.kwargs['headers']
        self.assertNotIn('Authorization',sent);self.assertNotIn('Cookie',sent)
        connection.close.assert_called_once()

    def test_upstream_github_redirect_is_not_forwarded(self):
        connection=self.upstream(b'HTTP/1.1 302 Found\r\nLocation: https://github.com/package\r\nContent-Length: 0\r\n\r\n')
        with patch.object(gateway,'PublicHTTPS',return_value=connection):
            result=self.request('registry',b'GET /pypi.org/simple/a HTTP/1.1\r\n\r\n')
        self.assertIn(b'403 Forbidden',result);self.assertNotIn(b'Location:',result)

    def test_binary_limit_does_not_emit_successful_end_of_chunk_stream(self):
        for body,complete in ((b'abcd',True),(b'abcde',False)):
            connection=self.upstream(b'HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n\r\n'+body)
            with patch.object(gateway,'PublicHTTPS',return_value=connection),patch.object(gateway,'MAX_DOWNLOAD',4):
                result=self.request('registry',b'GET /files.pythonhosted.org/a.whl HTTP/1.1\r\n\r\n')
            self.assertIn(b'4\r\nabcd\r\n',result)
            self.assertEqual(result.endswith(b'0\r\n\r\n'),complete)

    def test_client_hello_requires_exact_tls_host(self):
        incoming,outgoing=ssl.MemoryBIO(),ssl.MemoryBIO()
        client=ssl.create_default_context().wrap_bio(incoming,outgoing,server_side=False,server_hostname='api.openai.com')
        with self.assertRaises(ssl.SSLWantReadError):client.do_handshake()
        hello=outgoing.read()
        self.assertEqual(gateway.tls_server_name(hello),'api.openai.com')
        for invalid in (b'',b'GET / HTTP/1.1\r\n',hello[:-1],hello[:8]):
            with self.assertRaises(ValueError):gateway.tls_server_name(invalid)

    def test_proxy_mode_refuses_every_direct_provider_tunnel(self):
        with patch.object(gateway, 'connect_public') as connect:
            for host in ('api.anthropic.com', 'api.openai.com', 'chatgpt.com', 'auth.x.ai'):
                result = self.request('provider', f'CONNECT {host}:443 HTTP/1.1\r\n\r\n'.encode(),
                                      provider_socket='/provider/session.sock')
                self.assertIn(b'403 Forbidden', result)
            connect.assert_not_called()

    def test_inference_refuses_arbitrary_routes_and_oversized_or_chunked_bodies(self):
        with patch.object(gateway, 'CacheConnection') as connect:
            for method, path, extra in [('GET', '/v1/messages', ''), ('POST', '/v1/models', ''),
                ('POST', 'https://api.anthropic.com/v1/messages', ''),
                ('POST', '/v1/messages', 'Transfer-Encoding: chunked\r\n')]:
                result = self.request('inference',
                    f'{method} {path} HTTP/1.1\r\nContent-Length: 0\r\n{extra}\r\n'.encode(),
                    provider_socket='/provider/session.sock')
                self.assertIn(b'403 Forbidden', result)
            result = self.request('inference',
                f'POST /v1/messages HTTP/1.1\r\nContent-Length: {gateway.MAX_INFERENCE_BODY + 1}\r\n\r\n'.encode())
            self.assertIn(b'403 Forbidden', result)
            connect.assert_not_called()


class InferenceFixture(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *_): pass
    def do_POST(self):
        self.server.received = (self.path, dict(self.headers), self.rfile.read(int(self.headers['Content-Length'])))
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Retry-After', '7'); self.send_header('Connection', 'close'); self.end_headers()
        try:
            self.wfile.write(b'event: ping\n\n'); self.wfile.flush()
            self.server.release.wait(3)
            self.wfile.write(b'data: done\n\n'); self.wfile.flush()
        except OSError: pass
        self.close_connection = True


class InferenceServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class InferenceGatewayTests(unittest.TestCase):
    @contextmanager
    def route(self):
        with tempfile.TemporaryDirectory(prefix='provider-route-') as directory:
            path = str(Path(directory) / 'session.sock')
            with InferenceServer(path, InferenceFixture) as broker, gateway.Server(('127.0.0.1', 0), 'inference') as frontend:
                broker.release = threading.Event(); frontend.provider_socket = path
                threads = [threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
                           for server in (broker, frontend)]
                for thread in threads: thread.start()
                try: yield broker, frontend
                finally:
                    broker.release.set(); broker.shutdown(); frontend.shutdown()
                    for thread in threads: thread.join()

    def test_incremental_sse_and_capability_transport_without_arbitrary_credentials(self):
        with self.route() as (broker, frontend):
            client = http.client.HTTPConnection(*frontend.server_address, timeout=2)
            client.request('POST', '/v1/messages?beta=true', b'{"model":"pinned"}', headers={
                'Authorization': 'Bearer job-capability', 'X-Api-Key': 'never-forward', 'Cookie': 'never-forward',
                'Anthropic-Version': '2023-06-01'})
            response = client.getresponse()
            self.assertEqual(response.getheader('Retry-After'), '7')
            self.assertEqual(response.read1(1024), b'event: ping\n\n')
            self.assertFalse(broker.release.is_set())
            broker.release.set()
            self.assertEqual(response.read(), b'data: done\n\n'); client.close()
            path, headers, body = broker.received
            self.assertEqual((path, body), ('/v1/messages?beta=true', b'{"model":"pinned"}'))
            self.assertEqual(headers['authorization'], 'Bearer job-capability')
            self.assertNotIn('x-api-key', headers); self.assertNotIn('cookie', headers)

    def test_response_bound_and_total_deadline_truncate_without_success_marker(self):
        for setting, value in [('MAX_INFERENCE_RESPONSE', 2), ('INFERENCE_SECONDS', .15)]:
            with self.subTest(setting=setting), patch.object(gateway, setting, value), self.route() as (broker, frontend):
                client = http.client.HTTPConnection(*frontend.server_address, timeout=2)
                client.request('POST', '/v1/messages', b'{}')
                response = client.getresponse()
                with self.assertRaises(http.client.IncompleteRead): response.read()
                client.close()
