"""API-key-only inference broker; one Unix socket and capability per execution.

No caller chooses an account, upstream, or model. The lifecycle owner supplies
those and a live authorization callback. Browser credentials are not supported.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import hmac
import http.client
from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import socket
import socketserver
import threading
import time

MAX_BODY = 8 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
MAX_HEADERS = 16384
REQUEST_SECONDS = 600
IDLE_SECONDS = 90
UPSTREAM = 'api.anthropic.com'
PATHS = frozenset({'/v1/messages', '/v1/messages?beta=true',
                   '/v1/messages/count_tokens', '/v1/messages/count_tokens?beta=true'})


class ProxyError(Exception):
    def __init__(self, reason, status=403):
        self.reason, self.status = reason, status
        super().__init__(reason)


@dataclass(frozen=True)
class Assignment:
    execution_id: str
    account_generation: str
    model: str
    deadline: float
    capability: str


class Broker:
    def __init__(self, assignment, *, api_key, authorized, clock=time.time, connection=None):
        """authorized(assignment) must read current fence and account generation.

        api_key is a host-only loader. It is called only after admission and may
        not select credentials using request fields. No secrets are logged.
        """
        self.assignment, self.api_key, self.authorized, self.clock = assignment, api_key, authorized, clock
        self.connection = connection or (lambda: http.client.HTTPSConnection(UPSTREAM, timeout=IDLE_SECONDS))

    def alive(self):
        try:
            return self.clock() < self.assignment.deadline and self.authorized(self.assignment) is True
        except Exception:
            return False

    def admit(self, method, path, headers, body):
        if not self.alive():
            raise ProxyError('execution_unavailable')
        token = headers.get('authorization', '')
        if not hmac.compare_digest(token.encode(), ('Bearer ' + self.assignment.capability).encode()):
            raise ProxyError('execution_unavailable')
        if method != 'POST' or path not in PATHS:
            raise ProxyError('route_not_allowed')
        if len(body) > MAX_BODY:
            raise ProxyError('request_too_large', 413)
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise ProxyError('invalid_request', 400) from None
        if not isinstance(payload, dict) or payload.get('model') != self.assignment.model:
            raise ProxyError('model_not_allowed')
        # Preserve Anthropic capability/version and session headers; caller
        # credentials, forwarding headers, cookies and Host never cross over.
        forwarded = {name: value for name, value in headers.items()
                     if name.startswith(('anthropic-', 'x-claude-code-'))
                     or name in {'content-type', 'accept', 'user-agent'}}
        key = self.api_key()
        if not isinstance(key, str) or not key or '\r' in key or '\n' in key:
            raise ProxyError('account_unavailable', 503)
        forwarded.update({'x-api-key': key, 'content-length': str(len(body))})
        return forwarded

    @contextmanager
    def open(self, method, path, headers, body):
        forwarded = self.admit(method, path, headers, body)
        connection = self.connection()
        stopped = threading.Event()
        request_deadline = min(self.assignment.deadline, self.clock() + REQUEST_SECONDS)
        peer = None

        def valid():
            return self.clock() < request_deadline and self.alive()

        def abort_when_withdrawn():
            while not stopped.wait(0.25):
                if not valid():
                    if peer is not None:
                        try:
                            peer.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                    connection.close()
                    return

        watcher = None
        try:
            connection.connect()
            peer = connection.sock
            if not valid():
                raise ProxyError('execution_unavailable')
            watcher = threading.Thread(target=abort_when_withdrawn, daemon=True)
            watcher.start()
            connection.request(method, path, body=body, headers=forwarded)
            response = connection.getresponse()
            if not valid():
                raise ProxyError('execution_unavailable')
            # Redirects cannot redirect the credential or select another host.
            if 300 <= response.status < 400:
                raise ProxyError('upstream_redirect_refused', 502)
            output_headers = {name.lower(): value for name, value in response.getheaders()
                              if name.lower() in {'content-type', 'retry-after', 'x-should-retry', 'request-id'}
                              or name.lower().startswith('anthropic-')}

            def chunks():
                total = 0
                while True:
                    if not valid():
                        raise ProxyError('execution_unavailable')
                    chunk = response.read1(65536)
                    if not valid():
                        raise ProxyError('execution_unavailable')
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_RESPONSE:
                        raise ProxyError('response_too_large', 502)
                    yield chunk
            yield response.status, output_headers, chunks()
        finally:
            stopped.set()
            connection.close()
            if watcher:
                watcher.join(timeout=1)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(IDLE_SECONDS)
        remaining = min(REQUEST_SECONDS, max(0, self.server.broker.assignment.deadline - self.server.broker.clock()))
        self.deadline_timer = threading.Timer(remaining, self.expire)
        self.deadline_timer.daemon = True
        self.deadline_timer.start()

    def expire(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def finish(self):
        self.deadline_timer.cancel()
        super().finish()

    def log_message(self, *_):
        pass

    def do_POST(self):
        sent = False
        try:
            if sum(len(k) + len(v) for k, v in self.headers.items()) > MAX_HEADERS:
                raise ProxyError('headers_too_large', 431)
            lengths = self.headers.get_all('Content-Length', [])
            if (self.headers.get('Transfer-Encoding') is not None or len(lengths) != 1
                    or not lengths[0].isdigit()
                    or len(self.headers.get_all('Authorization', [])) != 1):
                raise ProxyError('invalid_request', 400)
            length = int(lengths[0])
            if length > MAX_BODY:
                raise ProxyError('request_too_large', 413)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ProxyError('invalid_request', 400)
            headers = {k.lower(): v for k, v in self.headers.items()}
            with self.server.broker.open(self.command, self.path, headers, body) as (status, headers, chunks):
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header('Connection', 'close')
                self.end_headers()
                sent = True
                for chunk in chunks:
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except ProxyError as error:
            if not sent:
                self.reply(error.status, error.reason)
        except (OSError, http.client.HTTPException, ValueError):
            if not sent:
                self.reply(502, 'upstream_unavailable')
        finally:
            self.close_connection = True

    def reply(self, status, reason):
        body = json.dumps({'error': {'type': 'forge_proxy_error', 'message': reason}}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)


class SessionServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    """Private socket mounted only into the execution's trusted gateway."""
    daemon_threads = True

    def __init__(self, path, broker, max_concurrent=4):
        self.broker = broker
        self.slots = threading.BoundedSemaphore(max_concurrent)
        path = Path(path)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        super().__init__(str(path), Handler)
        path.chmod(0o600)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            request.close()
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, *_):
        pass


def claude_environment(env, base_url, capability, *, auth_mode):
    """Explicit API-key route; browser sign-ins retain their existing path."""
    if auth_mode != 'api_key':
        raise ProxyError('auth_mode_not_supported')
    return {**{k: v for k, v in env.items() if k not in {
        'ANTHROPIC_API_KEY', 'ANTHROPIC_AUTH_TOKEN', 'CLAUDE_CODE_OAUTH_TOKEN',
        'FORGE_HARNESS_AUTH', 'FORGE_AUTH_DIGEST'}},
        'ANTHROPIC_BASE_URL': base_url, 'ANTHROPIC_AUTH_TOKEN': capability}
