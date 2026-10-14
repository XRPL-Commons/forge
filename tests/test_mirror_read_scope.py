"""Read handles survive a transfer; execution and policy snapshots never do."""
from contextlib import contextmanager, ExitStack
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from forge.factory.config import DEFAULT
from forge.factory.source_inputs import create, digest
from forge.mirror.git_store import CHUNK_BYTES, HttpBody
from forge.mirror.policy import MirrorError, policy_digest
from forge.mirror.service import Broker, SessionServer, send
from forge.mirror_daemon import MirrorDaemon
from forge.role_qualification import QualificationSettings
from forge.role_qualification_fixtures import Fixtures
from forge.settings import Settings
from forge.store.db import Store
from forge.store.fresh import initialize
from forge.worker_qualification import Fixture
from tests.test_settings import configured
from tests.test_source_preparation import Access


class ReadScopeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        git = Fixture(self.root)
        installation = self.root / 'installation'
        installation.mkdir(mode=0o700)
        initialize(installation)
        self.settings = Settings(installation, scope='active')
        self.settings.save_config(configured())
        access = Access()
        repository = access.resolve('acme/project')
        self.fixtures = Fixtures(self.root / 'authority', repository,
            dict(number=1, title='Synthetic issue', body='Synthetic issue'),
            'requirements', git.oid, dict(DEFAULT), 600)
        self.job = self.fixtures.reserve('build')
        self.daemon = MirrorDaemon(self.fixtures.store.root, self.fixtures.store,
            settings=QualificationSettings(self.settings, repository), access=access, git_store=git.cache)
        policy = {**git.policy, 'canonical_repository': 'acme/project',
                  'manifest_digest': digest(self.job['input_manifest'])}
        source = create(repository_id=1, canonical_repository='acme/project', base_oid=git.oid,
            input_oids=[], manifest=self.job['input_manifest'], dependency_policy_digest=policy_digest(policy), notes={})
        mirror = self.daemon.mirror
        mirror.prepare(source, policy)
        self.session = mirror.bind_session(job_id=self.job['id'], execution_fence=self.job['execution_fence'],
            network_generation='fixture', deadline=self.job['deadline'], source_digest=digest(source),
            policy_digest=policy_digest(policy))
        spool = tempfile.TemporaryFile()
        self.addCleanup(spool.close)
        spool.write(b'x' * (CHUNK_BYTES * 8))
        self.body = HttpBody(spool, 0, CHUNK_BYTES * 8)
        body = self.body
        class Backend:
            def ensure(self, *args, **kwargs):
                return dict(cache=True, host_fetch=False)
            def http(self, *args, alive, **kwargs):
                body.alive = alive
                return {}, body
        self.broker = Broker(self.session, mirror, Backend(), resolve=self.daemon.resolve,
            live_authority=self.daemon.live_authority, live_policy=self.daemon.policy, revalidate=self.daemon.revalidate)
        self.request = dict(method='POST', path='/github/acme/project.git/git-upload-pack', protocol='',
            content_type='application/x-git-upload-pack-request', body_length=0)

    def test_reused_connections_read_new_fence_and_policy_between_chunks(self):
        for change in ('fence', 'deny'):
            # Each case gets its own authority and installation, as the real
            # qualification does; reads must lease both underlying stores.
            with self.subTest(change=change):
                case = ReadScopeTests()
                case.setUp()
                try:
                    opened = {case.fixtures.store: [], case.settings.database: []}
                    with ExitStack() as patches:
                        for store, connections in opened.items():
                            original = store._connect_path
                            def connect(*args, original=original, connections=connections, **kwargs):
                                connection = original(*args, **kwargs)
                                if kwargs.get('read_only'):
                                    connections.append(connection)
                                return connection
                            patches.enter_context(patch.object(store, '_connect_path', side_effect=connect))
                        with case.daemon.read_scope():
                            status, _, body = case.broker.handle(case.request, b'')
                            self.assertEqual(status, 200)
                            chunks = body.chunks()
                            self.assertEqual(len(next(chunks)), CHUNK_BYTES)
                            self.assertEqual(len(next(chunks)), CHUNK_BYTES)
                            for store, connections in opened.items():
                                self.assertEqual(len(connections), 1)
                                self.assertFalse(connections[0].in_transaction)
                                self.assertIsNone(store._reader.get())
                            if change == 'fence':
                                with Store(case.fixtures.store.root).write() as db:
                                    db.execute('UPDATE jobs SET fenced=1 WHERE id=?', (case.job['id'],))
                            else:
                                config = case.settings.load()
                                config['repositories'] = [dict(id=1, name='acme/project', enabled=False,
                                    mirror_denied_repositories=[dict(repository_id=1, canonical_name='acme/project')])]
                                case.settings.save_config(config)
                            with self.assertRaisesRegex(MirrorError, 'session_revoked'):
                                next(chunks)
                        for store, connections in opened.items():
                            self.assertEqual(len(connections), 1)
                            self.assertIsNone(store._read_lease.get())
                            with self.assertRaises(sqlite3.ProgrammingError):
                                connections[0].execute('SELECT 1')
                finally:
                    case.doCleanups()

    def test_request_scope_closes_both_connections_on_client_disconnect(self):
        finished = threading.Event()
        observations = []
        main_thread = threading.get_ident()
        @contextmanager
        def scope():
            try:
                with self.daemon.read_scope():
                    observations.append(threading.get_ident() != main_thread)
                    yield
            finally:
                observations.append(all(store._read_lease.get() is None
                    for store in (self.fixtures.store, self.settings.database)))
                finished.set()
        path = self.root / 'session.sock'
        with SessionServer(path, self.broker, read_scope=scope) as server:
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                    client.settimeout(5)
                    client.connect(str(path))
                    send(client, self.request, b'')
                    self.assertEqual(len(client.recv(4)), 4)
                    client.shutdown(socket.SHUT_RDWR)
                self.assertTrue(finished.wait(5))
                self.assertEqual(observations, [True, True])
            finally:
                server.shutdown()
                thread.join()
