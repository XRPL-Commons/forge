"""Host-owned, bounded public package archive cache behind the job gateway.

Only the trusted gateway receives this Unix socket. Workers receive neither
the socket nor cache files. Metadata is fetched live and is rewritten by the
existing gateway; this service preserves upstream bytes and cache semantics.
"""
import argparse
from contextlib import contextmanager, closing
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import socketserver
import tempfile
import threading
import time
from urllib.parse import urlsplit


_spec = importlib.util.spec_from_file_location('forge_registry_gateway',
    Path(__file__).resolve().parents[1] / 'deploy/runner/gateway.py')
gateway = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gateway)
CAPACITY = 2 * 1024**3
MAX_ENTRIES = 4096
ARCHIVES = ('.tgz', '.whl', '.zip', '.tar.gz', '.tar.bz2', '.tar.xz')


def socket_directory(root):
    return Path(root) / 'registry/socket'


def lifetime(headers, *, host, path, content_type, now):
    """Reuse public archives only for explicit, still-fresh upstream lifetimes."""
    if not urlsplit(path).path.lower().endswith(ARCHIVES) or any(v in content_type for v in ('json', 'html')):
        return 0
    directives = {}
    for value in headers.get('cache-control', '').lower().split(','):
        key, _, setting = value.strip().partition('=')
        directives[key] = setting.strip('"')
    if any(key in directives for key in ('private', 'no-store', 'no-cache')) or 'public' not in directives:
        return 0
    vary = {value.strip().lower() for value in headers.get('vary', '').split(',') if value.strip()}
    if vary - {'accept', 'accept-encoding'} or headers.get('set-cookie'):
        return 0
    value = directives.get('s-maxage', directives.get('max-age', ''))
    age = headers.get('age', '0')
    if not value.isdecimal() or not age.isdecimal():
        return 0
    apparent_age = 0
    if headers.get('date'):
        try:
            apparent_age = max(0, now - parsedate_to_datetime(headers['date']).timestamp())
        except (ValueError, TypeError, OverflowError):
            return 0
    return max(0, min(int(value) - max(int(age), apparent_age), 86400))


class ArchiveCache:
    def __init__(self, directory, *, capacity=CAPACITY, entries=MAX_ENTRIES, clock=time.time, upstream=gateway.PublicHTTPS):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.capacity, self.entries, self.clock, self.upstream = capacity, entries, clock, upstream
        self.lock = threading.RLock()
        # The server admits two requests; this table only holds active keys.
        self.fetch_locks = {}
        self.hits = self.misses = 0
        with self.lock:
            for path in [*self.directory.glob('fetch-*'), *self.directory.glob('*.pending')]:
                path.unlink()
            self._prune()

    def _rows(self):
        rows = []
        for path in self.directory.glob('*.json'):
            value = json.loads(path.read_text())
            body = path.with_suffix('.body')
            if value['expires'] <= self.clock() or not body.exists():
                path.unlink(); body.unlink(missing_ok=True)
            else:
                rows.append((value['used'], path, value))
        for body in self.directory.glob('*.body'):
            if not body.with_suffix('.json').exists():
                body.unlink()
        return sorted(rows)

    def _prune(self, *, required=0, admit=False):
        rows = self._rows()
        total = sum(row[2]['size'] for row in rows)
        while rows and (total + required > self.capacity or len(rows) + int(admit) > self.entries):
            _, path, value = rows.pop(0)
            path.unlink(); path.with_suffix('.body').unlink(missing_ok=True)
            total -= value['size']

    def _save(self, path, value):
        temporary = path.with_suffix('.pending')
        temporary.write_text(json.dumps(value, separators=(',', ':')))
        os.replace(temporary, path)

    def _hit(self, key):
        with self.lock:
            path = self.directory / (key + '.json')
            if not path.exists():
                return None
            value = json.loads(path.read_text())
            if value['expires'] <= self.clock():
                return None
            stream = path.with_suffix('.body').open('rb')
            value['used'] = self.clock()
            self._save(path, value)
            return value['status'], value['headers'], stream, True

    @contextmanager
    def _fetching(self, key):
        with self.lock:
            entry = self.fetch_locks.setdefault(key, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self.lock:
                entry[1] -= 1
                if not entry[1]:
                    del self.fetch_locks[key]

    @contextmanager
    def response(self, method, host, path, accept):
        # The caller supplies only allowlisted targets; validate again because
        # this socket is an independently reachable trusted service boundary.
        gateway.registry_target('/' + host + path)
        if method not in ('GET', 'HEAD'):
            raise ValueError('read-only registry')
        key = hashlib.sha256(json.dumps([host, path, accept, 'identity']).encode()).hexdigest()
        result = self._hit(key)
        if result is None:
            with self._fetching(key):
                result = self._hit(key)
                if result is None:
                    result = self._fetch(method, host, path, accept, key)
        with self.lock:
            if result[3]: self.hits += 1
            else: self.misses += 1
        try:
            yield result
        finally:
            result[2].close()

    def _fetch(self, method, host, path, accept, key):
        with closing(self.upstream(host, timeout=20)) as connection:
            connection.request(method, path, headers={'Accept': accept, 'Accept-Encoding': 'identity',
                'User-Agent': 'Forge-readonly-registry/1'})
            response = connection.getresponse()
            headers = {name.lower(): response.getheader(name) for name, _ in response.getheaders()}
            if sum(len(name) + len(value) for name, value in headers.items()) > gateway.MAX_HEADERS:
                raise ValueError('registry headers exceed limit')
            content_type = headers.get('content-type', 'application/octet-stream')
            if headers.get('content-encoding', 'identity').lower() != 'identity':
                raise ValueError('registry did not honor identity encoding')
            selected = [('Content-Type', content_type)]
            if headers.get('location'):
                location = headers['location']
                gateway.registry_url('https://' + host + location if location.startswith('/') else location)
                selected.append(('Location', location))
            if any('\r' in value or '\n' in value for _, value in selected):
                raise ValueError('invalid upstream header')
            maximum = gateway.MAX_METADATA if any(v in content_type for v in ('json', 'html')) else gateway.MAX_DOWNLOAD
            declared = headers.get('content-length')
            if declared is not None and (not declared.isdecimal() or int(declared) > maximum):
                raise ValueError('registry response exceeds limit')
            if method == 'HEAD' and declared is not None:
                selected.append(('Content-Length', declared))
            observed_at = self.clock()
            ttl = lifetime(headers, host=host, path=path, content_type=content_type, now=observed_at)
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix='fetch-', delete=False) as output:
                temporary = Path(output.name)
                try:
                    total = 0
                    deadline = time.monotonic() + 300
                    if method != 'HEAD':
                        while True:
                            if time.monotonic() >= deadline:
                                raise TimeoutError('registry download deadline')
                            chunk = response.read(min(65536, maximum + 1 - total))
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > maximum:
                                raise ValueError('registry response exceeds limit')
                            output.write(chunk)
                        if declared is not None and total != int(declared):
                            raise ValueError('incomplete registry response')
                    output.flush()
                    stream = temporary.open('rb')
                    try:
                        if method == 'GET' and response.status == 200 and observed_at + ttl > self.clock() and total <= self.capacity:
                            with self.lock:
                                self._prune(required=total, admit=True)
                                target = self.directory / (key + '.body')
                                os.replace(temporary, target)
                                self._save(target.with_suffix('.json'), dict(status=response.status,
                                    headers=selected, size=total, expires=observed_at + ttl, used=self.clock()))
                    except BaseException:
                        stream.close()
                        raise
                    return response.status, selected, stream, False
                finally:
                    temporary.unlink(missing_ok=True)


class Handler(gateway.Handler):
    def registry(self, method, host, path, headers):
        with self.server.cache.response(method, host, path, headers.get('accept', '*/*')) as result:
            status, selected, stream, hit = result
            size = next((value for name, value in selected if name == 'Content-Length'),
                        os.fstat(stream.fileno()).st_size)
            self.wfile.write(f'HTTP/1.1 {status} Response\r\nConnection: close\r\nContent-Length: {size}\r\n'.encode())
            for name, value in [*selected, ('X-Forge-Cache', 'hit' if hit else 'miss')]:
                if name == 'Content-Length': continue
                if '\r' in value or '\n' in value:
                    raise ValueError('invalid upstream header')
                self.wfile.write(f'{name}: {value}\r\n'.encode())
            self.wfile.write(b'\r\n')
            if method == 'GET':
                while chunk := stream.read(65536):
                    self.wfile.write(chunk)
            self.wfile.flush()


class Server(gateway.Server, socketserver.UnixStreamServer):
    address_family = socket.AF_UNIX
    def __init__(self, path, cache):
        self.cache = cache
        super().__init__(str(path), 'registry')
        self.RequestHandlerClass = Handler


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    directory = socket_directory(args.root)
    path = directory / 'registry.sock'
    if args.check:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
            peer.settimeout(2); peer.connect(str(path))
        return
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory.parent / 'daemon.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path.unlink(missing_ok=True)
        cache = ArchiveCache(Path(args.root) / 'registry/cache')
        with Server(path, cache) as server:
            os.chmod(path, 0o600)
            server.serve_forever()


if __name__ == '__main__':
    main()
