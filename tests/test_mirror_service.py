from contextlib import contextmanager
from dataclasses import replace
import hashlib
import http.client
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from forge.factory.source_inputs import create, digest
from forge.mirror.frontend import Frontend
from forge.mirror.git_store import GitStore, environment, run_git
from forge.mirror.policy import MirrorError, Repository, policy_digest
from forge.mirror.service import Broker, SessionServer, wants
from forge.store.db import Store
from forge.store.mirror import MirrorStore
from tests.test_mirror_policy import policy


@contextmanager
def no_credentials(repository):
    yield None


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Store.create(self.root / 'runtime')
        self.store = MirrorStore(self.db, clock=lambda: 100)
        self.policy = policy()
        self.policy['manifest_digest'] = digest({'job': 'fixture'})
        self.source = create(repository_id=1, canonical_repository='acme/project', base_oid='a' * 40,
                             input_oids=[], manifest={'job': 'fixture'}, dependency_policy_digest=policy_digest(self.policy), notes={})
        self.key = self.store.prepare(self.source, self.policy)
        self.session_id = self.store.bind_session(job_id='aux:triage:1', execution_fence='f1', network_generation='n1',
            deadline=1000, source_digest=digest(self.source), policy_digest=self.key)

    def test_immutable_source_policy_and_auxiliary_session_revocation(self):
        repeated = self.store.bind_session(job_id='aux:triage:1', execution_fence='f1', network_generation='n1',
            deadline=1000, source_digest=digest(self.source), policy_digest=self.key)
        self.assertEqual(repeated, self.session_id)
        with self.assertRaises(MirrorError):
            self.store.bind_session(job_id='aux:triage:1', execution_fence='f1', network_generation='n1',
                deadline=999, source_digest=digest(self.source), policy_digest=self.key)
        with self.assertRaises(sqlite3.IntegrityError), self.db.write() as db:
            db.execute("UPDATE mirror_policies SET policy_json='{}'")
        self.store.revoke(self.session_id)
        self.store.revoke(self.session_id)
        self.assertEqual(self.store.session(self.session_id)['revoked_at'], 100)

    def test_receipts_and_denials_are_job_facts_without_automatically_failing_result(self):
        self.store.append_receipt(self.session_id, {'outcome': 'denied', 'reason': 'project_denied', 'repository': 'acme/secret'})
        receipt = self.store.receipts('aux:triage:1')[0]
        self.assertEqual(receipt['details']['policy_digest'], self.key)
        self.assertEqual(self.store.claims('aux:triage:1')[0]['reason'], 'project_denied')
        with self.assertRaises(MirrorError):
            self.store.append_receipt(self.session_id, {'token': 'never-persist'})
        with self.db.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM unit_gates').fetchone()[0], 0)

    def broker(self, git_store, **kwargs):
        self.repository = Repository(1, 'acme/project', 10, 'Organization', 20, actual_access=True)
        return Broker(self.session_id, self.store, git_store,
            resolve=kwargs.get('resolve', lambda name: self.repository),
            live_authority=kwargs.get('live_authority', lambda session: True),
            live_policy=kwargs.get('live_policy', lambda session: {'organization_installations': self.policy['organization_installations'], 'denied_repositories': []}),
            clock=lambda: 100)

    def test_all_cached_fetches_reauthorize_and_record_denials(self):
        class Backend:
            count = 0
            def ensure(self, repository, **kwargs):
                self.count += 1
                return {'cache': True, 'host_fetch': False}
            def http(self, repository, kind, **kwargs):
                return {'content-type': 'application/x-git-upload-pack-advertisement'}, b'0000'
        backend = Backend()
        denied = []
        broker = self.broker(backend, live_policy=lambda session: {'organization_installations': self.policy['organization_installations'], 'denied_repositories': denied})
        request = dict(method='GET', path='/github/acme/project.git/info/refs?service=git-upload-pack', protocol='', content_type='', body_length=0)
        self.assertEqual(broker.handle(request, b'')[0], 200)
        denied.append({'canonical_name': 'acme/project', 'repository_id': 1})
        self.assertEqual(broker.handle(request, b'')[0], 403)
        self.assertEqual(backend.count, 1)
        self.assertEqual(self.store.claims('aux:triage:1')[0]['reason'], 'project_denied')
        self.store.revoke(self.session_id)
        self.assertEqual(broker.handle(request, b'')[0], 403)
        self.assertEqual(len(self.store.receipts('aux:triage:1')), 6)

    def test_revocation_during_backend_work_cancels_before_handing_back_bytes(self):
        store, identifier = self.store, self.session_id
        class Backend:
            def ensure(self, repository, *, alive):
                self.alive_before = alive()
                store.revoke(identifier)
                self.alive_after = alive()
                return {'cache': True, 'host_fetch': False}
            def http(self, *args, **kwargs):
                raise AssertionError('revoked request must not serve')
        backend = Backend()
        broker = self.broker(backend)
        request = dict(method='GET', path='/github/acme/project.git/info/refs?service=git-upload-pack', protocol='', content_type='', body_length=0)
        self.assertEqual(broker.handle(request, b'')[0], 403)
        self.assertTrue(backend.alive_before)
        self.assertFalse(backend.alive_after)

    def test_job_byte_budget_survives_store_restart(self):
        self.store.max_request_bytes = 10
        self.store.append_receipt(self.session_id, {'outcome': 'started', 'request_bytes': 8})
        restarted = MirrorStore(self.db, clock=lambda: 100, max_request_bytes=10)
        with self.assertRaisesRegex(MirrorError, 'mirror_capacity'):
            restarted.append_receipt(self.session_id, {'outcome': 'started', 'request_bytes': 3})

    def test_write_static_credential_and_traversal_paths_refused_without_lookup(self):
        broker = self.broker(None, resolve=lambda name: self.fail('invalid request reached metadata lookup'))
        for path in ('/github/acme/project.git/git-receive-pack', '/github/acme/project.git/HEAD',
                     '/github/acme/project.git/objects/ab/cd', '/github/acme/../project.git/info/refs?service=git-upload-pack',
                     '/github/acme/project.git/info/refs?service=git-receive-pack',
                     '/github/acme/project.git/info/refs?service=git-upload-pack&token=secret'):
            metadata = dict(method='GET', path=path, protocol='', content_type='', body_length=0)
            self.assertEqual(broker.handle(metadata, b'')[0], 403)
        self.assertEqual(len(self.store.receipts('aux:triage:1')), 6)
        self.assertNotIn('secret', json.dumps(self.store.receipts('aux:triage:1')))


class SmartHTTPTests(StoreTests):
    def setUp(self):
        super().setUp()
        self.cache = GitStore(self.root / 'cache', credentials=no_credentials, refresh_seconds=3600)
        self.gitdir = self.cache.path(1)
        run_git(['init', '--bare', str(self.gitdir)])
        blob = run_git(['--git-dir', str(self.gitdir), 'hash-object', '-w', '--stdin'], data=b'private dependency\n').decode().strip()
        tree = run_git(['--git-dir', str(self.gitdir), 'mktree'], data=f'100644 blob {blob}\tREADME.md\n'.encode()).decode().strip()
        env = {**environment(), 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        self.oid = run_git(['--git-dir', str(self.gitdir), 'commit-tree', tree, '-m', 'fixture'], env=env).decode().strip()
        run_git(['--git-dir', str(self.gitdir), 'update-ref', 'refs/heads/main', self.oid])
        run_git(['--git-dir', str(self.gitdir), 'symbolic-ref', 'HEAD', 'refs/heads/main'])
        for key, value in [('http.receivepack', 'false'), ('http.getanyfile', 'false'), ('http.uploadpack', 'true')]:
            run_git(['--git-dir', str(self.gitdir), 'config', key, value])
        (self.gitdir / 'git-daemon-export-ok').touch()
        (self.gitdir / 'forge-refreshed').touch()

    def test_real_smart_http_clone_fetch_no_write_and_live_revocation(self):
        broker = self.broker(self.cache)
        socketpath = self.root / 'session.sock'
        with SessionServer(socketpath, broker) as service:
            thread = threading.Thread(target=service.serve_forever, daemon=True)
            thread.start()
            try:
                with Frontend(('127.0.0.1', 0), str(socketpath)) as frontend:
                    front_thread = threading.Thread(target=frontend.serve_forever, daemon=True)
                    front_thread.start()
                    try:
                        url = f'http://127.0.0.1:{frontend.server_port}/github/acme/project.git'
                        cloned = self.root / 'checkout'
                        run_git(['clone', url, str(cloned)])
                        self.assertEqual((cloned / 'README.md').read_text(), 'private dependency\n')
                        self.assertEqual(run_git(['-C', str(cloned), 'rev-parse', 'HEAD']).decode().strip(), self.oid)
                        run_git(['-C', str(cloned), 'fetch', 'origin'])
                        with self.assertRaises(MirrorError):
                            run_git(['-C', str(cloned), 'push', 'origin', 'HEAD:refs/heads/forbidden'])
                        self.store.revoke(self.session_id)
                        with self.assertRaises(MirrorError):
                            run_git(['-C', str(cloned), 'fetch', 'origin'])
                    finally:
                        frontend.shutdown()
                        front_thread.join()
            finally:
                service.shutdown()
                thread.join()
        receipts = self.store.receipts('aux:triage:1')
        self.assertTrue(any(r['details'].get('request_kind') == 'upload' and r['details']['outcome'] == 'served' for r in receipts))
        self.assertTrue(any(r['details'].get('reason') == 'session_revoked' for r in receipts))
        self.assertTrue(all('source_digest' in r['details'] for r in receipts))

    def test_base_object_handle_pins_exact_commit_and_reader_never_checks_out(self):
        repository = Repository(1, 'acme/project', 10, 'Organization', 20, actual_access=True)
        with self.cache.base_objects(repository, [self.oid], digest(self.source)) as handle:
            self.assertEqual(handle.git_dir, self.gitdir)
            self.assertEqual(handle.pinned_oids, (self.oid,))
            self.assertEqual(handle.source_input_digest, digest(self.source))
            reader = self.cache.reader(1)
            entry = reader.list_tree(self.oid)[0]
            self.assertEqual(reader.read_blob(entry['oid']), b'private dependency\n')
        self.assertFalse((self.gitdir / 'README.md').exists())


    def test_uncached_host_fetch_creates_bare_mirror_without_storing_token(self):
        from forge.mirror import git_store as module
        @contextmanager
        def credentials(repository):
            self.assertEqual(repository.repository_id, 2)
            yield 'host-only-test-token'
        backend = GitStore(self.root / 'new-cache', credentials=credentials)
        real_run = module.run_git
        def local_upstream(args, **kwargs):
            if 'fetch' in args:
                args = list(args)
                index = args.index('https://github.com/acme/dependency.git')
                args[index] = str(self.gitdir)
                self.assertEqual(kwargs['env']['FORGE_MIRROR_TOKEN'], 'host-only-test-token')
                kwargs['env'] = {**kwargs['env'], 'GIT_CONFIG_COUNT': '6',
                                 'GIT_CONFIG_KEY_5': 'protocol.file.allow', 'GIT_CONFIG_VALUE_5': 'always'}
            return real_run(args, **kwargs)
        repo = Repository(2, 'acme/dependency', 10, 'Organization', 20, actual_access=True)
        with patch.object(module, 'run_git', side_effect=local_upstream):
            result = backend.ensure(repo, pinned_oids=[self.oid])
            cached = backend.ensure(repo, pinned_oids=[self.oid])
        self.assertEqual(result, {'cache': False, 'host_fetch': True})
        self.assertEqual(cached, {'cache': True, 'host_fetch': False})
        self.assertNotIn('host-only-test-token', (backend.path(2) / 'config').read_text())
        self.assertEqual(real_run(['--git-dir', str(backend.path(2)), 'rev-parse', 'HEAD']).decode().strip(), self.oid)


class ProtocolTests(unittest.TestCase):
    def test_oid_receipt_does_not_claim_wants_are_served_roots(self):
        oid = 'a' * 40
        data = ('0032want ' + oid + '\n0000').encode()
        self.assertEqual(wants(data)['requested_oids'], [oid])
        self.assertNotIn('served_oids', wants(data))
        self.assertEqual(wants(b'zzzzhostile')['requested_oids'], [])

    def test_host_credentials_are_context_only_and_not_git_configuration(self):
        @contextmanager
        def credentials(repository):
            yield 'private-canary-token'
        with tempfile.TemporaryDirectory() as directory:
            backend = GitStore(Path(directory) / 'cache', credentials=credentials)
            with backend._authenticated(Repository(1, 'acme/project', 10, 'Organization', 20)) as env:
                self.assertEqual(env['FORGE_MIRROR_TOKEN'], 'private-canary-token')
                script = Path(env['GIT_ASKPASS'])
                self.assertNotIn('private-canary-token', script.read_text())
                self.assertEqual(script.stat().st_mode & 0o777, 0o700)
            self.assertFalse(script.exists())
