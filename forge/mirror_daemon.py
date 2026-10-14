"""Host mirror service: bind/revoke sockets from durable execution authority."""
import argparse
from contextlib import contextmanager
import fcntl
import os
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


def main():
    parser = argparse.ArgumentParser(description='Serve Forge job-bound read-only Git mirrors')
    parser.add_argument('--root', required=True)
    parser.add_argument('--peer-uid', type=int)
    args = parser.parse_args()
    root = Path(args.root)
    store = selected(root)
    if store is None:
        parser.error('Mirror service requires the active SQLite runtime')
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
    main()
