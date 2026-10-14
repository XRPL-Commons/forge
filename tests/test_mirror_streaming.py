"""Large native Git transfers retain authority and bounded memory through HTTP."""
from contextlib import contextmanager
import hashlib
import http.client
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import tracemalloc
import unittest
from unittest.mock import patch

from forge.mirror.frontend import Frontend
from forge.mirror.git_store import CHUNK_BYTES, HttpBody, MAX_RESPONSE, environment, run_git, spool_git
from forge.mirror.policy import MirrorError
from forge.mirror.service import SessionServer, send
from tests import test_mirror_service as fixture_tests


@contextmanager
def served(fixture):
    socketpath = fixture.root / 'stream.sock'
    with SessionServer(socketpath, fixture.broker(fixture.cache)) as service:
        thread = threading.Thread(target=service.serve_forever, daemon=True)
        thread.start()
        try:
            with Frontend(('127.0.0.1', 0), str(socketpath)) as frontend:
                front_thread = threading.Thread(target=frontend.serve_forever, daemon=True)
                front_thread.start()
                try:
                    yield frontend
                finally:
                    frontend.shutdown()
                    front_thread.join()
        finally:
            service.shutdown()
            thread.join()


class StreamingTests(unittest.TestCase):
    def fixture(self):
        fixture = fixture_tests.SmartHTTPTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    def body(self, size=2 * CHUNK_BYTES, alive=lambda: True):
        spool = tempfile.TemporaryFile()
        self.addCleanup(spool.close)
        spool.write(b'x' * size)
        return HttpBody(spool, 0, size, alive=alive)

    def test_native_clone_above_old_cap_is_streamed_with_bounded_python_memory(self):
        fixture = self.fixture()
        source = fixture.root / 'large.bin'
        expected = hashlib.sha256()
        # Generated only in the temporary test directory; incompressible so the
        # native upload-pack response actually crosses the former 64 MiB cap.
        with source.open('wb') as output:
            for _ in range((MAX_RESPONSE // CHUNK_BYTES) + 16):
                chunk = os.urandom(CHUNK_BYTES)
                output.write(chunk)
                expected.update(chunk)
        blob = run_git(['--git-dir', str(fixture.gitdir), 'hash-object', '-w', str(source)]).decode().strip()
        tree = run_git(['--git-dir', str(fixture.gitdir), 'mktree'], data=f'100644 blob {blob}\tlarge.bin\n'.encode()).decode().strip()
        env = {**environment(), 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        commit = run_git(['--git-dir', str(fixture.gitdir), 'commit-tree', tree, '-m', 'large source'], env=env).decode().strip()
        run_git(['--git-dir', str(fixture.gitdir), 'update-ref', 'refs/heads/main', commit])
        closed = []
        close = HttpBody.close
        def record_close(body):
            close(body)
            closed.append(body.spool.closed)
        with served(fixture) as frontend, patch.object(HttpBody, 'close', record_close):
            tracemalloc.start()
            try:
                clone = fixture.root / 'large-clone'
                run_git(['-c', 'protocol.version=0', 'clone', '--quiet',
                         f'http://127.0.0.1:{frontend.server_port}/github/acme/project.git', str(clone)])
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
        actual = hashlib.sha256()
        with (clone / 'large.bin').open('rb') as incoming:
            while chunk := incoming.read(CHUNK_BYTES):
                actual.update(chunk)
        self.assertEqual(actual.digest(), expected.digest())
        self.assertEqual(run_git(['-C', str(clone), 'rev-parse', 'HEAD']).decode().strip(), commit)
        receipts = fixture.store.receipts('aux:triage:1')
        self.assertTrue(any(row['details'].get('response_bytes', 0) > MAX_RESPONSE for row in receipts))
        self.assertLess(peak, 16 * 1024 ** 2)
        self.assertTrue(closed and all(closed))

    def test_quota_refusal_and_post_generation_revocation_close_spool(self):
        for revoke in (False, True):
            with self.subTest(revoke=revoke):
                fixture = self.fixture()
                body = self.body()
                class Backend:
                    def ensure(self, *args, **kwargs):
                        return {'cache': True, 'host_fetch': False}
                    def http(self, *args, **kwargs):
                        if revoke:
                            fixture.store.revoke(fixture.session_id)
                        return {}, body
                if not revoke:
                    fixture.store.max_response_bytes = len(body) - 1
                broker = fixture.broker(Backend())
                metadata = dict(method='POST', path='/github/acme/project.git/git-upload-pack',
                                protocol='', content_type='application/x-git-upload-pack-request', body_length=0)
                self.assertEqual(broker.handle(metadata, b'')[0], 403 if revoke else 503)
                self.assertTrue(body.spool.closed)
                self.assertFalse(any(row['details']['outcome'] == 'served' for row in fixture.store.receipts('aux:triage:1')))

    def test_transfer_stops_at_chunk_boundary_when_authority_changes(self):
        allowed = True
        body = self.body(alive=lambda: allowed)
        writes = []
        class Destination:
            def sendall(self, value):
                nonlocal allowed
                writes.append(len(value))
                if len(writes) == 2:
                    allowed = False
        with self.assertRaisesRegex(MirrorError, 'session_revoked'):
            send(Destination(), {'status': 200, 'headers': {}}, body)
        self.assertEqual(writes[1:], [CHUNK_BYTES])

    def test_client_disconnect_closes_owned_spool_without_extra_request_receipt(self):
        body = self.body(4 * CHUNK_BYTES)
        closed = threading.Event()
        real_close = body.close
        def close():
            real_close()
            closed.set()
        body.close = close
        class Broker:
            malformed_count = 0
            def handle(self, *_):
                return 200, {}, body
            def malformed(self):
                self.malformed_count += 1
        broker = Broker()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'disconnect.sock'
            with SessionServer(path, broker) as server:
                thread = threading.Thread(target=server.serve_forever)
                thread.start()
                try:
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.connect(str(path))
                        send(client, {}, b'')
                        client.shutdown(socket.SHUT_RDWR)
                    self.assertTrue(closed.wait(5))
                    self.assertTrue(body.spool.closed)
                    self.assertEqual(broker.malformed_count, 0)
                finally:
                    server.shutdown()
                    thread.join()

    def test_backend_output_limit_is_exact_and_tempfiles_close_on_failure(self):
        opened = []
        temporary = tempfile.TemporaryFile
        def record(*args, **kwargs):
            value = temporary(*args, **kwargs)
            opened.append(value)
            return value
        popen = subprocess.Popen
        def writer(_args, **kwargs):
            return popen([sys.executable, '-c', 'import os,time; os.write(1,b"x"*65536); time.sleep(30)'], **kwargs)
        with patch('forge.mirror.git_store.tempfile.TemporaryFile', side_effect=record), \
                patch('forge.mirror.git_store.subprocess.Popen', side_effect=writer):
            with self.assertRaisesRegex(MirrorError, 'mirror_capacity'):
                spool_git(['fixture'], limit=10)
        self.assertTrue(opened and all(value.closed for value in opened))

    def test_timeout_kills_descendants_after_the_leader_has_exited(self):
        popen, killpg = subprocess.Popen, os.killpg
        processes, kills = [], []
        code = ('import subprocess,sys; '
                'subprocess.Popen([sys.executable,"-c","import time; time.sleep(30)"]); '
                'sys.exit(0)')
        def leader(_args, **kwargs):
            process = popen([sys.executable, '-c', code], **kwargs)
            processes.append(process)
            return process
        def terminate(pid, signum):
            kills.append((pid, processes[0].poll()))
            killpg(pid, signum)
        with patch('forge.mirror.git_store.subprocess.Popen', side_effect=leader), \
                patch('forge.mirror.git_store.os.killpg', side_effect=terminate):
            with self.assertRaisesRegex(MirrorError, 'mirror_timeout'):
                spool_git(['fixture'], timeout=.3)
        self.assertEqual(kills, [(processes[0].pid, 0)])
        self.assertTrue(processes[0].stdout.closed)

    def test_invalid_cgi_header_closes_backend_spool(self):
        fixture = self.fixture()
        spool = tempfile.TemporaryFile()
        self.addCleanup(spool.close)
        spool.write(b'not a CGI header')
        spool.seek(0)
        fixture.broker(fixture.cache)
        with patch('forge.mirror.git_store.spool_git', return_value=spool):
            with self.assertRaisesRegex(MirrorError, 'mirror_protocol_failed'):
                fixture.cache.http(fixture.repository, 'advertisement')
        self.assertTrue(spool.closed)

    def test_frontend_truncated_pack_closes_without_inserting_second_http_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'fake.sock')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as broker:
                broker.bind(path)
                broker.listen(1)
                def reply():
                    from forge.mirror.service import receive
                    import json
                    import struct
                    client, _ = broker.accept()
                    with client:
                        receive(client)
                        metadata = json.dumps({'status': 200, 'headers': {'content-type': 'application/x-git-upload-pack-result'},
                                               'body_length': 2 * CHUNK_BYTES}).encode()
                        client.sendall(struct.pack('!I', len(metadata)) + metadata)
                        client.sendall(b'p' * CHUNK_BYTES)
                thread = threading.Thread(target=reply)
                thread.start()
                with Frontend(('127.0.0.1', 0), path) as frontend:
                    worker = threading.Thread(target=frontend.serve_forever)
                    worker.start()
                    try:
                        client = http.client.HTTPConnection('127.0.0.1', frontend.server_port, timeout=5)
                        client.request('GET', '/github/acme/project.git/info/refs?service=git-upload-pack')
                        response = client.getresponse()
                        self.assertEqual(response.status, 200)
                        with self.assertRaises(http.client.IncompleteRead) as failure:
                            response.read()
                        self.assertEqual(failure.exception.partial, b'p' * CHUNK_BYTES)
                        client.close()
                    finally:
                        frontend.shutdown()
                        worker.join()
                thread.join()
