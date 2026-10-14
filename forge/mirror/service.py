"""Job-bound smart HTTP broker. The socket, never request data, selects a job."""
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import socketserver
import struct
import threading
import time

from .git_store import MAX_REQUEST, HttpBody
from .policy import MirrorError, authorize, canonical_name

MAX_HEADER = 8192
PATH = re.compile(r'/github/([A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,104})/(info/refs\?service=git-upload-pack|git-upload-pack)\Z')


def receive_exact(stream, count):
    data = bytearray()
    while len(data) < count:
        part = stream.recv(min(65536, count - len(data)))
        if not part:
            raise MirrorError('request_invalid')
        data.extend(part)
    return bytes(data)


def receive(stream):
    size = struct.unpack('!I', receive_exact(stream, 4))[0]
    if size > MAX_HEADER:
        raise MirrorError('request_invalid')
    try:
        metadata = json.loads(receive_exact(stream, size))
    except (ValueError, UnicodeError):
        raise MirrorError('request_invalid') from None
    length = metadata.get('body_length') if isinstance(metadata, dict) else None
    if type(length) is not int or not 0 <= length <= MAX_REQUEST:
        raise MirrorError('request_invalid')
    return metadata, receive_exact(stream, length)


def send(stream, metadata, body):
    raw = json.dumps({**metadata, 'body_length': len(body)}, separators=(',', ':')).encode()
    if len(raw) > MAX_HEADER:
        raise MirrorError('request_invalid')
    stream.sendall(struct.pack('!I', len(raw)) + raw)
    chunks = body.chunks() if isinstance(body, HttpBody) else (body, )
    for chunk in chunks:
        stream.sendall(chunk)


def wants(body):
    """Record protocol request OIDs, never assert that these were served roots."""
    requested = []
    offset = 0
    while offset + 4 <= len(body):
        try:
            size = int(body[offset:offset + 4], 16)
        except ValueError:
            break
        if size in (0, 1, 2):
            offset += 4
            continue
        if size < 4 or offset + size > len(body):
            break
        packet = body[offset + 4:offset + size]
        match = re.match(rb'want ([0-9a-f]{40}|[0-9a-f]{64})(?:[ \n\x00]|$)', packet)
        if match:
            requested.append(match[1].decode())
        offset += size
    ordered = sorted(set(requested))
    return {'requested_oids': ordered[:512],
            'requested_oids_digest': hashlib.sha256('\n'.join(ordered).encode()).hexdigest()}


class Broker:
    def __init__(self, session_id, store, git_store, *, resolve, live_authority, live_policy, clock=time.time, revalidate=None):
        self.session_id, self.store, self.git_store = session_id, store, git_store
        self.resolve, self.live_authority, self.live_policy, self.clock = resolve, live_authority, live_policy, clock
        self.revalidate = revalidate or (lambda repository: repository)

    def _session(self):
        session = self.store.session(self.session_id)
        if session['revoked_at'] is not None or session['deadline'] <= self.clock() or self.live_authority(session) is not True:
            raise MirrorError('session_revoked')
        return session

    def _authorize(self, session, repository):
        current = self.live_policy(session)
        return authorize(session['policy'], repository,
                         live_installations=current['organization_installations'],
                         live_denied=current.get('denied_repositories', []))

    def handle(self, metadata, body):
        started = self.clock()
        details = dict(request_kind='invalid', request_bytes=len(body), response_bytes=0,
                       started_at=started, outcome='started')
        repository = None
        request_id = None
        response = None
        handed_off = False
        try:
            request_id = self.store.append_receipt(self.session_id, details)
            # Only these bounded fields cross the trusted frontend connection.
            if (not isinstance(metadata, dict) or set(metadata) != {'method', 'path', 'protocol', 'content_type', 'body_length'}
                    or metadata['body_length'] != len(body) or len(body) > MAX_REQUEST
                    or metadata['protocol'] not in ('', 'version=1', 'version=2')):
                raise MirrorError('request_invalid')
            match = PATH.fullmatch(metadata['path']) if isinstance(metadata['path'], str) else None
            if not match:
                raise MirrorError('request_invalid')
            name = canonical_name(match[1].removesuffix('.git'))
            kind = 'advertisement' if match[2].startswith('info/refs') else 'upload'
            if (metadata['method'] != ('GET' if kind == 'advertisement' else 'POST')
                    or kind == 'advertisement' and body
                    or kind == 'upload' and metadata['content_type'] != 'application/x-git-upload-pack-request'):
                raise MirrorError('request_invalid')
            details.update(repository=name, request_kind=kind, **wants(body))
            session = self._session()
            repository = self.resolve(name)
            # A redirect/rename is independently resolved; raw worker names never
            # become paths, but receipt preserves the actual trusted identity.
            details.update(repository=canonical_name(repository.canonical_name), repository_id=repository.repository_id)
            self._authorize(session, repository)

            def alive():
                try:
                    current = self._session()
                    self._authorize(current, self.revalidate(repository))
                    return True
                except MirrorError:
                    return False

            cache = self.git_store.ensure(repository, alive=alive)
            details.update(cache)
            self._authorize(self._session(), self.revalidate(repository))
            headers, response = self.git_store.http(repository, kind, body=body, protocol=metadata['protocol'], alive=alive)
            # No stream is handed back after a revoked execution or deny change.
            self._authorize(self._session(), self.revalidate(repository))
            details.update(outcome='served', response_bytes=len(response), finished_at=self.clock())
            if kind == 'advertisement':
                details['refs_digest'] = response.digest() if isinstance(response, HttpBody) else hashlib.sha256(response).hexdigest()
            # Served records the entire admitted payload, including transfers
            # interrupted by a disconnect. Admission precedes exposing any byte.
            self.store.append_receipt(self.session_id, details, request_id=request_id)
            handed_off = True
            return 200, headers, response
        except MirrorError as error:
            details.update(outcome='failed' if error.transient else 'denied', reason=error.reason,
                           transient=error.transient, finished_at=self.clock())
            if request_id is not None:
                self.store.append_receipt(self.session_id, details, request_id=request_id)
            status = 503 if error.transient or error.reason == 'mirror_capacity' else 403
            return status, {'content-type': 'text/plain'}, ('Forge mirror: ' + error.reason + '\n').encode()
        finally:
            if not handed_off and isinstance(response, HttpBody):
                response.close()

    def malformed(self):
        # A malformed frame is still an attempt. Revoked/exhausted sessions
        # reject future frames without extending durable receipt history.
        try:
            self.store.append_receipt(self.session_id, dict(request_kind='invalid', outcome='denied',
                reason='request_invalid', request_bytes=0, response_bytes=0, started_at=self.clock(), finished_at=self.clock()))
        except MirrorError:
            pass


class SessionServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """One root-created socket per session, mounted into its trusted frontend."""
    daemon_threads = True
    block_on_close = True

    def __init__(self, path, broker, *, peer_uid=None, max_concurrent=4):
        self.broker, self.peer_uid = broker, os.geteuid() if peer_uid is None else peer_uid
        self.slots = threading.BoundedSemaphore(max_concurrent)
        path = Path(path)
        if path.exists() or path.is_symlink():
            raise MirrorError('session_socket_exists')
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        super().__init__(str(path), _Handler)
        path.chmod(0o600)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.broker.malformed()
            request.close()
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        # Default socketserver traceback might include request content. Record
        # the owned host fault through the caller's service monitor instead.
        return


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.settimeout(150)
        response = None
        admitted = False
        try:
            if hasattr(socket, 'SO_PEERCRED'):
                _, uid, _ = struct.unpack('3i', self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            elif hasattr(self.request, 'getpeereid'):
                uid, _ = self.request.getpeereid()
            elif __import__('sys').platform == 'darwin':
                # Python does not expose BSD getpeereid on every macOS build.
                import ctypes
                libc = ctypes.CDLL(None, use_errno=True)
                uid_value, gid_value = ctypes.c_uint(), ctypes.c_uint()
                if libc.getpeereid(self.request.fileno(), ctypes.byref(uid_value), ctypes.byref(gid_value)) != 0:
                    raise MirrorError('session_peer_invalid')
                uid = uid_value.value
            else:
                raise MirrorError('session_peer_invalid')
            if uid != self.server.peer_uid:
                raise MirrorError('session_peer_invalid')
            metadata, body = receive(self.request)
            status, headers, response = self.server.broker.handle(metadata, body)
            admitted = True
            send(self.request, {'status': status, 'headers': headers}, response)
        except (MirrorError, TimeoutError, OSError):
            if not admitted:
                self.server.broker.malformed()
        finally:
            if isinstance(response, HttpBody):
                response.close()
