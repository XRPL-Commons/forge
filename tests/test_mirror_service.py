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
        self.assertEqual(len(self.store.receipts('aux:triage:1')), 4)

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

    def test_same_policy_supports_distinct_immutable_job_sources(self):
        other = create(repository_id=1, canonical_repository='acme/project', base_oid='b' * 40,
            input_oids=[], manifest={'job': 'fixture'}, dependency_policy_digest=self.key, notes={'role':'review'})
        self.assertEqual(self.store.prepare(other, self.policy), self.key)
        other_id = self.store.bind_session(job_id='review:2', execution_fence='f2', network_generation='n2',
            deadline=1000, source_digest=digest(other), policy_digest=self.key)
        self.assertEqual(self.store.session(self.session_id)['source_input'], self.source)
        self.assertEqual(self.store.session(other_id)['source_input'], other)
        with self.db.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM mirror_policies').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM mirror_sources').fetchone()[0], 2)
        different = {**self.policy, 'revision':'different'}
        third = create(repository_id=1, canonical_repository='acme/project', base_oid='a'*40, input_oids=[],
            manifest={'job':'fixture'}, dependency_policy_digest=policy_digest(different), notes={})
        self.store.prepare(third, different)
        with self.assertRaisesRegex(MirrorError, 'session_invalid'):
            self.store.bind_session(job_id='bad', execution_fence='f3', network_generation='n3',
                deadline=1000, source_digest=digest(other), policy_digest=policy_digest(different))
        with self.assertRaises(sqlite3.IntegrityError), self.db.write() as db:
            db.execute('INSERT INTO mirror_sessions VALUES(?,?,?,?,?,?,?,?,NULL)',
                ('bad','bad','f3','n3',digest(other),policy_digest(different),1000,100))

    def test_admit_and_prepared_bind_each_jobs_notes_to_its_exact_source(self):
        sources = []
        for job_id, notes in [('review', {'role':'review'}), ('audit', {'role':'audit'})]:
            source = create(repository_id=1, canonical_repository='acme/project', base_oid='a'*40,
                input_oids=[], manifest={'job':'fixture'}, dependency_policy_digest=self.key, notes=notes)
            authority = dict(execution_fence=job_id, repo_id=1, manifest={'job':'fixture'}, deadline=1000)
            with patch.object(self.store, 'authority_in', return_value=authority):
                self.store.admit(job_id, source, self.policy, notes, {}, execution_fence=job_id,
                    network_generation=job_id, deadline=1000)
            prepared = self.store.prepared(job_id)
            self.assertEqual(prepared['notes'], notes)
            self.assertEqual(prepared['source_input'], source)
            sources.append(source)
        self.assertNotEqual(digest(sources[0]), digest(sources[1]))
        with self.db.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM mirror_policies').fetchone()[0], 1)

    def test_malformed_and_terminal_only_attempts_are_bounded_with_one_capacity_claim(self):
        self.store.max_requests = 2
        broker = self.broker(None, resolve=lambda _: self.fail('invalid request reached lookup'))
        request = dict(method='GET', path='/nope', protocol='', content_type='', body_length=0)
        self.assertEqual(broker.handle(request, b'')[0], 403)
        broker.malformed()
        self.assertEqual(broker.handle(request, b'')[0], 503)
        for _ in range(20):
            broker.malformed()
            self.assertEqual(broker.handle(request, b'')[0], 403)
        self.assertEqual(len(self.store.receipts('aux:triage:1')), 4)
        self.assertEqual([row['reason'] for row in self.store.claims('aux:triage:1')], ['mirror_capacity'])
        self.assertIsNotNone(self.store.session(self.session_id)['revoked_at'])

    def test_denial_flood_after_quota_does_not_extend_history(self):
        self.store.max_requests = 1
        start = self.store.append_receipt(self.session_id, {'outcome':'started'})
        with self.assertRaisesRegex(MirrorError, 'mirror_capacity'):
            self.store.append_receipt(self.session_id, {'outcome':'denied', 'reason':'mirror_capacity'})
        for _ in range(20):
            with self.assertRaisesRegex(MirrorError, 'session_revoked'):
                self.store.append_receipt(self.session_id, {'outcome':'denied', 'reason':'mirror_capacity'})
        terminal = self.store.append_receipt(self.session_id, {'outcome':'denied','reason':'session_revoked'}, request_id=start)
        self.assertEqual(self.store.append_receipt(self.session_id, {'outcome':'denied','reason':'session_revoked'}, request_id=start), terminal)
        self.assertEqual(len(self.store.receipts('aux:triage:1')), 3)
        self.assertEqual(len(self.store.claims('aux:triage:1')), 1)

    def test_completion_after_revocation_cannot_claim_served_or_expand_records(self):
        start = self.store.append_receipt(self.session_id, {'outcome':'started'})
        self.store.revoke(self.session_id)
        with self.assertRaisesRegex(MirrorError, 'session_revoked'):
            self.store.append_receipt(self.session_id, {'outcome':'served', 'response_bytes':4}, request_id=start)
        terminal = [r for r in self.store.receipts('aux:triage:1') if r['request_id'] == start]
        self.assertEqual(len(terminal), 1)
        self.assertEqual(terminal[0]['details']['reason'], 'session_revoked')
        for _ in range(10):
            self.store.append_receipt(self.session_id, {'outcome':'denied', 'reason':'session_revoked'}, request_id=start)
        self.assertEqual(len(self.store.receipts('aux:triage:1')), 2)
        with self.assertRaisesRegex(MirrorError, 'receipt_invalid'):
            self.store.append_receipt(self.session_id, {'outcome':'denied'}, request_id='different-session-request')

    def test_response_quota_and_revocation_finalize_once_without_serving(self):
        start = self.store.append_receipt(self.session_id, {'outcome':'started'})
        self.store.max_response_bytes = 1
        with self.assertRaisesRegex(MirrorError, 'mirror_capacity'):
            self.store.append_receipt(self.session_id, {'outcome':'served','response_bytes':2}, request_id=start)
        self.assertEqual(len(self.store.receipts('aux:triage:1')), 2)
        self.assertEqual(len(self.store.claims('aux:triage:1')), 1)
        self.assertEqual(self.store.claims('aux:triage:1')[0]['details']['response_bytes'], 0)

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
        self.assertEqual(len(self.store.receipts('aux:triage:1')), 12)
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
                        receipt_count = len(self.store.receipts('aux:triage:1'))
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
        self.assertEqual(len(receipts), receipt_count)
        self.assertIsNotNone(self.store.session(self.session_id)['revoked_at'])
        self.assertTrue(all('source_digest' in r['details'] for r in receipts))

    def test_clone_with_gzip_encoded_upload_request_is_served(self):
        # Git compresses an upload-pack request above 1 KiB. A repository with
        # many refs therefore sends a gzip body on its first clone negotiation.
        env = {**environment(), 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        tree = run_git(['--git-dir', str(self.gitdir), 'rev-parse', self.oid + '^{tree}']).decode().strip()
        branches = {}
        for index in range(60):
            commit = run_git(['--git-dir', str(self.gitdir), 'commit-tree', tree, '-p', self.oid, '-m', f'branch {index}'], env=env).decode().strip()
            run_git(['--git-dir', str(self.gitdir), 'update-ref', f'refs/heads/topic-{index}', commit])
            branches[f'topic-{index}'] = commit
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
                        run_git(['clone', '--no-checkout', url, str(cloned)])
                        self.assertEqual(run_git(['-C', str(cloned), 'rev-parse', 'origin/topic-59']).decode().strip(),
                                         branches['topic-59'])
                    finally:
                        frontend.shutdown()
                        front_thread.join()
            finally:
                service.shutdown()
                thread.join()
        uploads = [r['details'] for r in self.store.receipts('aux:triage:1')
                   if r['details'].get('request_kind') == 'upload' and r['details']['outcome'] != 'started']
        self.assertTrue(uploads)
        self.assertTrue(all(r['outcome'] == 'served' for r in uploads), uploads)
        # The receipt records the decoded wants, not an opaque compressed body.
        self.assertTrue(set(branches.values()) <= {oid for r in uploads for oid in r['requested_oids']})

    def test_gzip_upload_request_is_bounded_and_must_be_complete(self):
        import gzip
        from forge.mirror.service import decode_upload
        body = ('0032want ' + 'a' * 40 + '\n0000').encode()
        self.assertEqual(decode_upload(gzip.compress(body)), body)
        self.assertEqual(decode_upload(body), body)
        for hostile in (gzip.compress(body)[:-3], gzip.compress(body) + b'trailing', b'\x1f\x8bnot-gzip',
                        gzip.compress(b'0' * (8 * 1024 * 1024 + 1))):
            with self.assertRaises(MirrorError) as raised:
                decode_upload(hostile)
            self.assertEqual(raised.exception.reason, 'request_invalid')

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
