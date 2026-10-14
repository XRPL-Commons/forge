"""Host mirror service: bind/revoke sockets from durable execution authority."""
import argparse
from contextlib import contextmanager
import fcntl
import os
import json
import stat
from pathlib import Path
import signal
import socket
import threading
import time

from .app_access import AccessError
from .factory.source_preparation import access_call, denied_repositories, session_directory
from .mirror.git_store import GitStore
from .mirror.policy import MirrorError, authorize
from .mirror.service import Broker, SessionServer
from .mirror_access import MirrorAccess
from .settings import Settings
from .store.mirror import MirrorStore
from .store.runtime import selected


class MirrorDaemon:
    def __init__(self, root, store, *, settings=None, access=None, git_store=None, clock=time.time, peer_uid=None):
        self.root, self.clock = Path(root), clock
        self.settings = settings or Settings(root)
        self.access = access or MirrorAccess(self.settings)
        self.mirror = MirrorStore(store, clock=clock)
        self.git_store = git_store or GitStore(self.root / 'state/factory/mirror/cache', credentials=self.credentials)
        self.peer_uid = os.geteuid() if peer_uid is None else peer_uid
        self.servers = {}
        self.observations = {}
        self.observation_lock = threading.RLock()

    @contextmanager
    def credentials(self, repository):
        try:
            with self.access.credentials(repository) as token:
                yield token
        except AccessError as error:
            if error.status in (401, 403, 404):
                raise MirrorError('installation_access_missing') from None
            raise MirrorError('mirror_unavailable', transient=True) from None

    def _local_connections(self):
        pairs = set()
        for connection in self.settings.load()['connections'].values():
            for identifier in connection.get('installation_ids', [connection.get('installation_id')]):
                pairs.add((connection['app_id'], identifier))
        return pairs

    def policy(self, session):
        # Local denies and configured installation removals apply immediately.
        # Fresh remote repository/install access is checked on every request by
        # resolve(). Long Git subprocesses repeat it after a bounded interval.
        allowed = self._local_connections()
        organizations = [item for item in session['policy']['organization_installations']
                         if (item['app_id'], item['installation_id']) in allowed]
        return {'organization_installations': organizations,
                'denied_repositories': denied_repositories(self.settings, session['policy']['project_repository_id'])}

    def resolve(self, name):
        repository = access_call(self.access.resolve, name)
        with self.observation_lock:
            self.observations[repository.repository_id] = (self.clock(), repository)
        return repository

    def live_authority(self, session):
        if not self.mirror.live_authority(session):
            return False
        return True

    def step(self):
        active = {session['id']: session for session in self.mirror.active_sessions()}
        for identifier, (server, thread) in list(self.servers.items()):
            session = active.get(identifier)
            if session is None or not self.live_authority(session):
                self.mirror.revoke(identifier)
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
                path = Path(server.server_address)
                path.unlink(missing_ok=True)
                del self.servers[identifier]
        for identifier, session in active.items():
            if identifier in self.servers:
                continue
            if not self.live_authority(session):
                self.mirror.revoke(identifier)
                continue
            directory = session_directory(self.root, identifier)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            path = directory / 'session.sock'
            if path.exists():
                # main() holds the singleton host lock. A prior process's
                # socket cannot be inherited as a new execution identity.
                if not path.is_socket() or path.stat().st_uid != os.geteuid():
                    raise MirrorError('session_socket_invalid')
                path.unlink()
            broker = Broker(identifier, self.mirror, self.git_store, resolve=self.resolve,
                            live_authority=self.live_authority, live_policy=self.policy, clock=self.clock,
                            revalidate=self.revalidate)
            server = SessionServer(path, broker, peer_uid=self.peer_uid)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.servers[identifier] = server, thread
        return len(self.servers)

    def revalidate(self, repository):
        with self.observation_lock:
            entry = self.observations.get(repository.repository_id)
            if entry is not None and self.clock() - entry[0] < 10:
                return entry[1]
            current = access_call(self.access.resolve, repository.canonical_name)
            if current.repository_id != repository.repository_id:
                raise MirrorError('repository_unavailable')
            self.observations[repository.repository_id] = self.clock(), current
            return current

    def close(self):
        # A process restart recreates still-owned endpoints from the same
        # durable session; shutdown itself grants no new execution authority.
        for server, thread in self.servers.values():
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            Path(server.server_address).unlink(missing_ok=True)
        self.servers.clear()


def health(root, store, *, timeout=3, clock=time.time):
    """Read-only bounded readiness: owned daemon lock and all current sockets."""
    root = Path(root)
    lock_path = root / 'state/factory/mirror/daemon.lock'
    until = time.monotonic() + timeout
    mirror = MirrorStore(store, clock=clock)
    while True:
        healthy = False
        count = 0
        try:
            info = lock_path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                return {'ok': False, 'reason': 'daemon_lock_invalid'}
            descriptor = os.open(lock_path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    healthy = True
                else:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
            if healthy:
                for session in mirror.active_sessions():
                    if not mirror.live_authority(session):
                        continue
                    path = session_directory(root, session['id']) / 'session.sock'
                    endpoint = path.lstat()
                    if not stat.S_ISSOCK(endpoint.st_mode) or endpoint.st_uid != os.geteuid() or stat.S_IMODE(endpoint.st_mode) != 0o600:
                        healthy = False
                        break
                    count += 1
        except (FileNotFoundError, OSError, MirrorError):
            healthy = False
        if healthy:
            return {'ok': True, 'active_sessions': count}
        if time.monotonic() >= until:
            return {'ok': False, 'reason': 'mirror_service_unavailable'}
        time.sleep(.1)


def main():
    parser = argparse.ArgumentParser(description='Serve Forge job-bound read-only Git mirrors')
    parser.add_argument('--root', required=True)
    parser.add_argument('--peer-uid', type=int)
    parser.add_argument('--check', action='store_true', help='Read-only bounded service readiness probe')
    args = parser.parse_args()
    root = Path(args.root)
    store = selected(root)
    if store is None:
        parser.error('Mirror service requires the active SQLite runtime')
    if args.check:
        result = health(root, store)
        print(json.dumps(result))
        return 0 if result['ok'] else 1
    directory = root / 'state/factory/mirror'
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = os.open(directory / 'daemon.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    stopped = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stopped.set())
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        service = MirrorDaemon(root, store, peer_uid=args.peer_uid)
        try:
            while not stopped.is_set():
                service.step()
                stopped.wait(.5)
        finally:
            service.close()
    finally:
        os.close(lock)


if __name__ == '__main__':
    raise SystemExit(main())
