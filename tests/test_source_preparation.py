from contextlib import contextmanager
import json
from pathlib import Path
import tempfile
import shutil
import unittest

from forge.factory.source_preparation import SourcePreparation
from forge.mirror.git_store import GitStore, environment, run_git
from forge.mirror.policy import MirrorError, Repository
from forge.mirror_daemon import MirrorDaemon, health
from forge.store.auxiliary import save_in
from forge.store.db import Store
from forge.store.executions import Executions, ExecutionQueries, Launch
from forge.store.models import Command
from forge.store.transitions import project_unit
from tests.test_mirror_service import no_credentials


class Settings:
    def __init__(self):
        self.denied = []
    def load(self):
        return {'connections': {'controller': {'app_id': 30, 'installation_id': 20}},
                'repositories': [{'id': 1, 'name': 'acme/project', 'enabled': True,
                                  'mirror_denied_repositories': self.denied}]}


class Access:
    def __init__(self):
        self.calls = []
    def resolve(self, name):
        self.calls.append(name)
        return Repository({'acme/project': 1, 'vendor/private': 2, 'vendor/nested': 3}.get(name, 4), name, 10, 'Organization', 20, actual_access=True)
    def organizations(self):
        return [{'account_id': 10, 'installation_id': 20, 'app_id': 30}]
    credentials = staticmethod(no_credentials)


class SourcePreparationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(dir='/private/tmp' if Path('/private/tmp').is_dir() else None)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = Store.create(self.root / 'database')
        self.settings, self.access = Settings(), Access()
        self.cache = GitStore(self.root / 'cache', credentials=no_credentials)
        gitdir = self.cache.path(1)
        run_git(['init', '--bare', str(gitdir)])
        blob = run_git(['--git-dir', str(gitdir), 'hash-object', '-w', '--stdin'],
                       data=b'{"dependencies":{"private":"github:vendor/private"}}').decode().strip()
        tree = run_git(['--git-dir', str(gitdir), 'mktree'], data=f'100644 blob {blob}\tpackage.json\n'.encode()).decode().strip()
        env = {**environment(), 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        self.base = run_git(['--git-dir', str(gitdir), 'commit-tree', tree, '-m', 'fixture'], env=env).decode().strip()
        run_git(['--git-dir', str(gitdir), 'update-ref', 'refs/heads/main', self.base])
        run_git(['--git-dir', str(gitdir), 'symbolic-ref', 'HEAD', 'refs/heads/main'])
        (gitdir / 'forge-refreshed').touch()
        shutil.copytree(gitdir, self.cache.path(2))
        self.life = Executions(self.store, clock=lambda: 100)
        admitted = self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
            repository_id=1, repository_name='acme/project', issue_number=1, title='Issue', body='Body',
            requirements={'acceptance': ['works']}, observed_at=90)
        self.manifest = dict(requirements_revision_id=admitted['revision_id'], base_oid=self.base, policy_hash='policy')
        self.member = admitted['member_id']
        self.preparation = SourcePreparation(self.root, self.store, settings=self.settings, access=self.access,
                                             git_store=self.cache, clock=lambda: 100)

    def reserve(self):
        with self.store.read_snapshot() as db:
            version = project_unit(db, 'u')['version']
        job_id = self.life.reserve_job(Command('reserve', 'operator', 'u', version), 'build', slot='slot', execution_fence='fence',
            input_manifest=self.manifest, member_id=self.member, launch=Launch('forge/work', 'builder[bot]', 'codex', 1000, {}, {}))['job_id']
        return ExecutionQueries(self.store).job(job_id)

    def test_freezes_pinned_dependencies_and_notes_once_before_launch(self):
        job = self.reserve()
        prepared = self.preparation.prepare(job, {'role': 'build', 'purpose': 'Implement the issue'}, publication_authorization={'branch': 'forge/work'})
        assignment = json.loads((prepared.input_directory / 'assignment.json').read_text())
        self.assertEqual(assignment['source_input']['base_oid'], self.base)
        self.assertEqual(assignment['publication_authorization']['branch'], 'forge/work')
        self.assertEqual((prepared.input_directory / 'assignment.json').stat().st_mode & 0o777, 0o444)
        record = self.preparation.mirror.prepared(job['id'])
        self.assertEqual(record['policy']['declared_repositories'][0]['canonical_name'], 'vendor/private')
        self.assertEqual(record['policy']['declared_repositories'][0]['repository_id'], 2)
        receipts = self.preparation.mirror.receipts(job['id'])
        self.assertTrue(any(r['details'].get('phase') == 'preparation' and r['details']['outcome'] == 'prepared' for r in receipts))
        calls = list(self.access.calls)
        retry = self.preparation.prepare(job, {'changed': 'cannot replace frozen notes'})
        self.assertEqual(retry.source_input, prepared.source_input)
        self.assertEqual(calls, self.access.calls)
        self.assertEqual(assignment, json.loads((prepared.input_directory / 'assignment.json').read_text()))

    def test_stale_execution_cannot_prepare_or_keep_its_mirror(self):
        job = self.reserve()
        prepared = self.preparation.prepare(job, {})
        session = self.preparation.mirror.session(prepared.session_id)
        self.assertTrue(self.preparation.mirror.live_authority(session))
        with self.store.write() as db:
            db.execute('UPDATE jobs SET fenced=1 WHERE id=?', (job['id'],))
        self.assertFalse(self.preparation.mirror.live_authority(session))
        with self.assertRaisesRegex(MirrorError, 'session_revoked'):
            self.preparation.prepare(job, {})

    def test_per_project_deny_prevents_even_assigned_source_fetch(self):
        job = self.reserve()
        self.settings.denied = [{'canonical_name': 'acme/project', 'repository_id': 1}]
        with self.assertRaisesRegex(MirrorError, 'project_denied'):
            self.preparation.prepare(job, {})
        self.assertIsNone(self.preparation.mirror.prepared(job['id']))

    def test_auxiliary_preparation_uses_persisted_manifest_and_fence(self):
        job = dict(id='aux', repo='acme/project', repo_id=1, slot='triage', deadline=1000,
                   role='triage', head=self.base, runner_config={})
        with self.store.write() as db:
            save_in(db, 'triage', '1', job, 100)
        prepared = self.preparation.prepare({**job, 'head': 'b' * 40}, {'role': 'triage'})
        self.assertEqual(prepared.source_input['base_oid'], self.base)
        session = self.preparation.mirror.session(prepared.session_id)
        self.assertEqual(session['execution_fence'], 'aux')
        with self.store.write() as db:
            db.execute("UPDATE auxiliary_executions SET fenced=1 WHERE id='aux'")
        self.assertFalse(self.preparation.mirror.live_authority(session))

    def test_daemon_creates_and_revokes_only_current_job_socket(self):
        job = self.reserve()
        prepared = self.preparation.prepare(job, {})
        daemon = MirrorDaemon(self.root, self.store, settings=self.settings, access=self.access,
                              git_store=self.cache, clock=lambda: 100)
        self.addCleanup(daemon.close)
        self.assertEqual(daemon.step(), 1)
        self.assertTrue((prepared.socket_directory / 'session.sock').is_socket())
        with self.store.write() as db:
            db.execute('UPDATE jobs SET fenced=1 WHERE id=?', (job['id'],))
        self.assertEqual(daemon.step(), 0)
        self.assertFalse((prepared.socket_directory / 'session.sock').exists())
        self.assertEqual(self.preparation.mirror.session(prepared.session_id)['revoked_at'], 100)

    def test_changed_authority_during_metadata_read_cannot_admit_input(self):
        job = self.reserve()
        resolve = self.access.resolve
        def changed(name):
            result = resolve(name)
            if name == 'vendor/private':
                with self.store.write() as db:
                    db.execute('UPDATE jobs SET fenced=1 WHERE id=?', (job['id'],))
            return result
        self.access.resolve = changed
        with self.assertRaisesRegex(MirrorError, 'session_revoked'):
            self.preparation.prepare(job, {})
        self.assertIsNone(self.preparation.mirror.prepared(job['id']))

    def test_discussion_uses_the_current_conversation_lease(self):
        from forge.store.discussions import DiscussionStore
        discussions = DiscussionStore(self.store, clock=lambda: 100)
        discussions.send('1:1', 'Question', 'request', 'revision', actor='operator')
        thread = discussions.thread('1:1')
        job = dict(id='discussion', repo='acme/project', repo_id=1, slot='discussion', deadline=1000,
            role='discussion', head=self.base, runner_config={'provider': 'codex'}, created_at=100,
            context={'revision': 'revision'})
        discussions.reserve('1:1', job, expected_version=thread['version'], origin={})
        prepared = self.preparation.prepare(job, {'role': 'discussion'})
        session = self.preparation.mirror.session(prepared.session_id)
        self.assertTrue(self.preparation.mirror.live_authority(session))
        with self.store.write() as db:
            db.execute('UPDATE discussions SET cancel_requested=1')
        self.assertFalse(self.preparation.mirror.live_authority(session))

    def test_verified_child_bundle_becomes_readable_without_worker_paths(self):
        from forge.mirror.dependencies import extract
        child = self.root / 'child.git'
        run_git(['init', '--bare', str(child)])
        env = {**environment(), 'GIT_ALTERNATE_OBJECT_DIRECTORIES': str(self.cache.path(1) / 'objects'),
               'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        blob = run_git(['--git-dir', str(child), 'hash-object', '-w', '--stdin'],
                       data=b'{"dependencies":{"approved":"github:vendor/approved"}}', env=env).decode().strip()
        tree = run_git(['--git-dir', str(child), 'mktree'], data=f'100644 blob {blob}\tpackage.json\n'.encode(), env=env).decode().strip()
        head = run_git(['--git-dir', str(child), 'commit-tree', tree, '-p', self.base, '-m', 'approved child'], env=env).decode().strip()
        run_git(['--git-dir', str(child), 'update-ref', 'HEAD', head], env=env)
        bundle = self.root / 'child.bundle'
        run_git(['--git-dir', str(child), 'bundle', 'create', str(bundle), 'HEAD', '^' + self.base], env=env)
        repository = self.access.resolve('acme/project')
        self.cache.import_bundle(repository, bundle.read_bytes(), head, self.base)
        result = extract(self.cache.reader(1), 1, 'acme/project', [head])
        self.assertEqual(result.repositories[0]['canonical_name'], 'vendor/approved')
        with self.assertRaisesRegex(MirrorError, 'source_bundle_invalid'):
            self.cache.import_bundle(repository, bundle.read_bytes(), 'a' * 40, self.base)

    def test_transitive_package_manifest_is_pinned_before_worker_execution(self):
        dependency = self.cache.path(2)
        env = {**environment(), 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
               'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        blob = run_git(['--git-dir', str(dependency), 'hash-object', '-w', '--stdin'],
                       data=b'{"dependencies":{"nested":"github:vendor/nested"}}').decode().strip()
        tree = run_git(['--git-dir', str(dependency), 'mktree'], data=f'100644 blob {blob}\tpackage.json\n'.encode()).decode().strip()
        head = run_git(['--git-dir', str(dependency), 'commit-tree', tree, '-p', self.base, '-m', 'dependency'], env=env).decode().strip()
        run_git(['--git-dir', str(dependency), 'update-ref', 'refs/heads/main', head])
        shutil.copytree(self.cache.path(1), self.cache.path(3))
        job = self.reserve()
        self.preparation.prepare(job, {})
        policy = self.preparation.mirror.prepared(job['id'])['policy']
        child = next(item for item in policy['declared_repositories'] if item['canonical_name'] == 'vendor/nested')
        self.assertEqual(child['repository_id'], 3)
        self.assertEqual(child['source_evidence']['commit_oid'], head)
        self.assertEqual(child['source_evidence']['parent_declarations'][0]['commit_oid'], self.base)
        self.assertEqual(len(child['source_evidence']['resolved_commit_oid']), 40)

    def test_health_probe_is_read_only_and_requires_live_daemon_lock(self):
        import fcntl
        import os
        before = sorted(str(path) for path in self.root.rglob('*'))
        self.assertFalse(health(self.root, self.store, timeout=0, clock=lambda: 100)['ok'])
        self.assertEqual(before, sorted(str(path) for path in self.root.rglob('*')))
        job = self.reserve()
        prepared = self.preparation.prepare(job, {})
        lock_path = self.root / 'state/factory/mirror/daemon.lock'
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.assertFalse(health(self.root, self.store, timeout=0, clock=lambda: 100)['ok'])
        daemon = MirrorDaemon(self.root, self.store, settings=self.settings, access=self.access,
                              git_store=self.cache, clock=lambda: 100)
        self.addCleanup(daemon.close)
        daemon.step()
        self.assertEqual(health(self.root, self.store, timeout=0, clock=lambda: 100), {'ok': True, 'active_sessions': 1})
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        self.assertFalse(health(self.root, self.store, timeout=0, clock=lambda: 100)['ok'])
