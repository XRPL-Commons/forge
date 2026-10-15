"""Real socket requests exercise cache reuse through distinct job gateways."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import http.client
import io
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock

from forge.registry_cache import ArchiveCache, Server, gateway


class Upstream:
    def __init__(self):
        self.calls = []
        self.body = b'package archive'
        self.headers = {'Content-Type': 'application/octet-stream', 'Cache-Control': 'public, max-age=60'}
        self.status = 200
        self.delay = 0
    def __call__(self, host, **kwargs):
        connection = Mock()
        def request(method, path, headers):
            self.calls.append((method, host, path, headers))
        connection.request.side_effect = request
        def response():
            time.sleep(self.delay)
            headers = {'Content-Length': str(len(self.body)), **self.headers}
            body = self.body if self.calls[-1][0] != 'HEAD' else b''
            raw = f'HTTP/1.1 {self.status} Response\r\n'.encode()
            raw += b''.join(f'{k}: {v}\r\n'.encode() for k, v in headers.items()) + b'\r\n' + body
            peer = Mock(); peer.makefile.return_value = io.BytesIO(raw)
            response = http.client.HTTPResponse(peer); response.begin()
            return response
        connection.getresponse.side_effect = response
        return connection


class RegistryCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='registry-cache-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.upstream = Upstream()
        self.cache = ArchiveCache(self.root / 'cache', upstream=self.upstream, clock=lambda: self.now)

    def fetch(self, path='/pkg/-/pkg.tgz', accept='*/*', method='GET'):
        with self.cache.response(method, 'registry.npmjs.org', path, accept) as result:
            status, headers, stream, hit = result
            return status, stream.read(), hit

    def test_fresh_archive_reuse_exact_url_representation_and_expiry(self):
        self.assertFalse(self.fetch()[2]); self.assertTrue(self.fetch()[2])
        self.assertFalse(self.fetch(accept='application/octet-stream')[2])
        self.assertFalse(self.fetch(path='/pkg/-/pkg.tgz?different=1')[2])
        self.assertEqual(len(self.upstream.calls), 3)
        self.now += 60
        self.assertFalse(self.fetch()[2])
        reopened = ArchiveCache(self.root / 'cache', upstream=self.upstream, clock=lambda: self.now)
        with reopened.response('GET', 'registry.npmjs.org', '/pkg/-/pkg.tgz', '*/*') as result:
            self.assertTrue(result[3]); self.assertEqual(result[2].read(), self.upstream.body)

    def test_metadata_private_stale_vary_and_errors_are_never_shared(self):
        cases = [('/pkg', {}), ('/pkg.tgz', {'Cache-Control': 'public, no-store, max-age=100'}),
            ('/pkg.tgz', {'Cache-Control': 'private, max-age=100'}),
            ('/pkg.tgz', {'Cache-Control': 'public, no-cache, max-age=100'}),
            ('/pkg.tgz', {'Cache-Control': 'max-age=100'}),
            ('/pkg.tgz', {'Vary': '*'}), ('/pkg.tgz', {'Vary': 'Authorization'}),
            ('/pkg.tgz', {'Set-Cookie': 'session=1'}), ('/pkg.tgz', {'Age': '60'}),
            ('/pkg.tgz', {'Date': 'Thu, 01 Jan 1970 00:00:00 GMT'}),
            ('/pkg.tgz', {'Content-Type': 'application/json'})]
        for index, (path, headers) in enumerate(cases):
            with self.subTest(headers=headers, path=path):
                self.upstream.headers = {'Content-Type': 'application/octet-stream',
                    'Cache-Control': 'public, max-age=60', **headers}
                target = path + '?case=' + str(index)
                self.assertFalse(self.fetch(target)[2]); self.assertFalse(self.fetch(target)[2])
        self.upstream.status = 404
        self.assertFalse(self.fetch()[2]); self.assertFalse(self.fetch()[2])
        self.assertFalse(list((self.root / 'cache').glob('*.body')))

    def test_incomplete_oversized_encoded_and_redirect_responses_do_not_publish(self):
        for headers in ({'Content-Length': '30'}, {'Content-Length': str(gateway.MAX_DOWNLOAD + 1)},
                        {'Content-Encoding': 'gzip'}, {'Location': 'https://127.0.0.1/a'}):
            with self.subTest(headers=headers):
                self.upstream.headers = headers
                with self.assertRaises(ValueError): self.fetch()
                self.assertFalse(list((self.root / 'cache').iterdir()))

    def test_byte_and_entry_limits_evict_lru(self):
        self.cache.capacity = 2 * len(self.upstream.body)
        self.cache.entries = 2
        self.fetch('/a.tgz'); self.now += 1; self.fetch('/b.tgz')
        self.now += 1; self.assertTrue(self.fetch('/a.tgz')[2])
        self.now += 1; self.fetch('/c.tgz')
        self.assertTrue(self.fetch('/a.tgz')[2])
        self.assertFalse(self.fetch('/b.tgz')[2])
        paths = list((self.root / 'cache').glob('*.body'))
        self.assertLessEqual(len(paths), 2)
        self.assertLessEqual(sum(path.stat().st_size for path in paths), self.cache.capacity)
        reopened = ArchiveCache(self.root / 'cache', capacity=self.cache.capacity, entries=2,
                                upstream=self.upstream, clock=lambda: self.now)
        self.assertEqual(len(list(reopened.directory.glob('*.body'))), 2)

    def test_concurrent_archive_requests_coalesce_and_head_cannot_poison_get(self):
        self.upstream.delay = .03
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.fetch(), range(2)))
        self.assertEqual(len(self.upstream.calls), 1)
        self.assertEqual(sorted(row[2] for row in results), [False, True])
        self.assertFalse(self.cache.fetch_locks)
        self.assertFalse(self.fetch('/head.tgz', method='HEAD')[2])
        self.assertEqual(self.fetch('/head.tgz')[1], self.upstream.body)

    @contextmanager
    def running(self, server):
        with server:
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
            thread.start()
            try: yield server
            finally: server.shutdown(); thread.join()

    def test_two_job_gateways_share_cache_without_forwarding_worker_credentials(self):
        socket_path = self.root / 'registry.sock'
        with self.running(Server(socket_path, self.cache)):
            for _ in range(2):
                with self.running(gateway.Server(('127.0.0.1', 0), 'registry')) as job:
                    job.cache_socket = str(socket_path)
                    connection = http.client.HTTPConnection(*job.server_address, timeout=2)
                    connection.request('GET', '/registry.npmjs.org/pkg/-/pkg.tgz',
                        headers={'Authorization': 'never-forward', 'Cookie': 'never-forward'})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), self.upstream.body); connection.close()
                    connection = http.client.HTTPConnection(*job.server_address, timeout=2)
                    connection.request('HEAD', '/registry.npmjs.org/pkg/-/pkg.tgz')
                    response = connection.getresponse()
                    self.assertEqual(response.getheader('Content-Length'), '0')  # Existing gateway HEAD contract.
                    self.assertEqual(response.read(), b''); connection.close()
            self.assertEqual(len(self.upstream.calls), 1)
            sent = self.upstream.calls[0][3]
            self.assertNotIn('Authorization', sent); self.assertNotIn('Cookie', sent)

    def test_socket_rejects_uploads_and_arbitrary_hosts_before_upstream(self):
        path = self.root / 'registry.sock'
        with self.running(Server(path, self.cache)):
            for method, target in [('POST', '/registry.npmjs.org/a'), ('GET', '/github.com/a'),
                                   ('CONNECT', 'pypi.org:443')]:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(2); peer.connect(str(path))
                    peer.sendall(f'{method} {target} HTTP/1.1\r\n\r\n'.encode())
                    self.assertIn(b'403 Forbidden', peer.recv(4096))
        self.assertFalse(self.upstream.calls)
