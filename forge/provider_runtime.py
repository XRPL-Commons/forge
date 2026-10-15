"""Durable API-key capabilities owned by the existing host mirror service.

Only trusted launch code creates records. The worker receives an opaque grant;
account selection, upstream credentials and current execution ownership stay here.
"""
import hashlib
import http.client
import socket
import json
import os
from pathlib import Path
import secrets
import stat
import threading
import time

from .credentials import Credentials
from .provider_proxy import Assignment, Broker, ProxyError, SessionServer
from .settings import digest, write_json
from .store.mirror import MirrorStore
from .mirror.policy import MirrorError

REFERENCE = 'ANTHROPIC_API_KEY'
BASE_URL = 'http://forge-provider:8082'
# Observed with the exact pinned binary against a credential-free fake upstream.
# Updating the harness requires repeating tests/qualification/claude_models.py.
MODELS = {None:'claude-opus-5', 'sonnet':'claude-sonnet-5', 'opus':'claude-opus-5',
          'haiku':'claude-haiku-4-5-20251001'}


def model(config):
    from .harnesses.registry import manifest
    if manifest('claude')['version'] != '2.1.273':
        raise ProxyError('provider_version_unqualified')
    selected = config.get('model')
    resolved = MODELS.get(selected, selected)
    if not isinstance(resolved, str) or not resolved.startswith('claude-') or len(resolved) > 128:
        raise ProxyError('model_unqualified')
    return resolved


def enabled(config):
    return (config.get('provider') == 'claude' and not config.get('provider_auth_file_env')
            and config.get('provider_environment') == [REFERENCE])


def directory(root, job_id):
    return Path(root) / 'state/provider-sockets' / hashlib.sha256(job_id.encode()).hexdigest()[:24]


def record_path(root, job_id):
    return Path(root) / 'state/provider-grants' / (hashlib.sha256(job_id.encode()).hexdigest() + '.json')


def read_record(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_size > 16384):
        raise ProxyError('grant_invalid')
    return json.loads(path.read_text())


def prepare(root, credential_root, store, job_id, config, *, clock=time.time):
    """Capture once; retries and service restarts reuse the same grant."""
    if not enabled(config):
        raise ProxyError('auth_mode_not_supported')
    selected_model = model(config)
    authority = MirrorStore(store, clock=clock).authority(job_id)
    generation = Credentials(credential_root).login_generations(only=[REFERENCE]).get(REFERENCE)
    if not generation:
        raise ProxyError('installation_account_required')
    expected = dict(job_id=job_id, execution_fence=authority['execution_fence'],
                    manifest_digest=digest(authority['manifest']), deadline=authority['deadline'],
                    account_generation=generation, model=selected_model)
    path = record_path(root, job_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    endpoint = directory(root, job_id)
    endpoint.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.exists():
        record = read_record(path)
        if {key: record.get(key) for key in expected} != expected:
            raise ProxyError('assignment_changed')
    else:
        record = {**expected, 'capability': secrets.token_urlsafe(32)}
        write_json(path, record)
    return record


def revoke(root, job_id):
    # Unlink first: all active streams recheck this same durable grant.
    record_path(root, job_id).unlink(missing_ok=True)


def ready(root, job_id, *, timeout=3):
    deadline = time.monotonic() + timeout
    path = directory(root, job_id) / 'session.sock'
    while True:
        try:
            info = path.lstat()
            if stat.S_ISSOCK(info.st_mode) and info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) == 0o600:
                record = read_record(record_path(root, job_id))
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                    peer.settimeout(.5); peer.connect(str(path))
                    peer.sendall(('GET /health HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer '
                                  + record['capability'] + '\r\nConnection: close\r\n\r\n').encode())
                    response = http.client.HTTPResponse(peer); response.begin()
                    if response.status == 204:
                        return True
        except (OSError, http.client.HTTPException):
            pass
        if time.monotonic() >= deadline:
            raise ProxyError('provider_service_unavailable', 503)
        time.sleep(.05)


class Service:
    def __init__(self, root, credential_root, store, *, clock=time.time):
        self.root, self.credentials = Path(root), Credentials(credential_root)
        self.mirror = MirrorStore(store, clock=clock)
        self.clock, self.servers = clock, {}

    def authorized(self, record):
        try:
            if read_record(record_path(self.root, record['job_id'])) != record:
                return False
            authority = self.mirror.authority(record['job_id'])
            return (authority['execution_fence'] == record['execution_fence']
                    and digest(authority['manifest']) == record['manifest_digest']
                    and self.clock() < record['deadline'] <= authority['deadline']
                    and self.credentials.login_generations(only=[REFERENCE]).get(REFERENCE) == record['account_generation'])
        except (OSError, ValueError, KeyError, ProxyError, MirrorError):
            return False

    def key(self, record):
        # A reconnect cannot race the generation check and secret load.
        with self.credentials.locked():
            if not self.authorized(record):
                raise ProxyError('execution_unavailable')
            return self.credentials.environment(only=[REFERENCE]).get(REFERENCE)

    def stop(self, identifier):
        server, thread, stopped, record = self.servers.pop(identifier)
        stopped.set()  # End already admitted streams before stopping acceptance.
        server.shutdown(); server.server_close(); thread.join(timeout=2)
        Path(server.server_address).unlink(missing_ok=True)
        if not record_path(self.root, identifier).exists():
            Path(server.server_address).parent.rmdir()

    def step(self):
        parent = self.root / 'state/provider-grants'
        records = {}
        if parent.exists():
            for path in parent.glob('*.json'):
                try:
                    record = read_record(path)
                except FileNotFoundError:
                    continue  # Concurrent execution cleanup withdrew this grant.
                if path != record_path(self.root, record['job_id']):
                    raise ProxyError('grant_invalid')
                if self.authorized(record):
                    records[record['job_id']] = record
        for identifier in list(self.servers):
            if identifier not in records or self.servers[identifier][3] != records[identifier]:
                self.stop(identifier)
        for identifier, record in records.items():
            if identifier in self.servers:
                continue
            path = directory(self.root, identifier) / 'session.sock'
            if path.exists():
                if not path.is_socket() or path.stat().st_uid != os.geteuid():
                    raise ProxyError('session_socket_invalid')
                path.unlink()
            stopped = threading.Event()
            assignment = Assignment(identifier, record['account_generation'], record['model'],
                                    record['deadline'], record['capability'])
            broker = Broker(assignment, api_key=lambda r=record: self.key(r),
                authorized=lambda _, r=record, s=stopped: not s.is_set() and self.authorized(r), clock=self.clock)
            server = SessionServer(path, broker)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers[identifier] = server, thread, stopped, record
        return len(self.servers)

    def close(self):
        # Durable capabilities survive restart; no new grant is issued.
        for identifier in list(self.servers):
            self.stop(identifier)
