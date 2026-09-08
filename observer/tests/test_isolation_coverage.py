import json
import os
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from observer import coverage, gitwatch, run
from observer.config import Repo
from observer.isolation import IsolationError, check_runtime
from observer.judge import Verdict
from observer.tests.test_run import FakeGit, C, deps_with


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.mirror = Path(self.temp.name, 'mirror.git')
        subprocess.run(['git', 'init', '--bare', '-q', str(self.mirror)], check=True)
        subprocess.run(['git', '-C', str(self.mirror), 'remote', 'add', 'origin', 'git@github.com:X/y.git'], check=True)
        for path in [self.mirror, *self.mirror.rglob('*')]:
            path.chmod(0o700 if path.is_dir() else 0o600)

    def test_minimal_independent_bare_mirror_is_accepted(self):
        gitwatch.verify_mirror(str(self.mirror), 'X/y')

    def test_wrong_remote_and_executable_git_config_are_rejected(self):
        for key, value in [('remote.origin.url', 'git@github.com:Other/repo.git'), ('core.sshCommand', 'untrusted-command')]:
            with self.subTest(key=key):
                subprocess.run(['git','-C',str(self.mirror),'config',key,value],check=True)
                with self.assertRaises(IsolationError): gitwatch.verify_mirror(str(self.mirror), 'X/y')
                subprocess.run(['git','-C',str(self.mirror),'config','--unset-all',key],check=True)
                subprocess.run(['git','-C',str(self.mirror),'config','remote.origin.url','git@github.com:X/y.git'],check=True)

    def test_group_writable_mirror_and_object_alternates_are_rejected(self):
        self.mirror.chmod(0o770)
        with self.assertRaises(IsolationError): gitwatch.verify_mirror(str(self.mirror), 'X/y')
        self.mirror.chmod(0o700)
        Path(self.mirror, 'objects/info/alternates').write_text('/worker/objects\n')
        with self.assertRaises(IsolationError): gitwatch.verify_mirror(str(self.mirror), 'X/y')

    def test_git_transport_ignores_inherited_config_and_ssh_agent(self):
        captured = {}
        def runner(cmd, **kwargs):
            captured.update(cmd=cmd, **kwargs)
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        with patch.dict(os.environ, {'GIT_CONFIG_COUNT':'1','GIT_CONFIG_KEY_0':'core.sshCommand', 'GIT_CONFIG_VALUE_0':'evil', 'SSH_AUTH_SOCK':'/worker/socket'}), patch('observer.gitwatch.subprocess.run', side_effect=runner):
            gitwatch.fetch(str(self.mirror))
        self.assertEqual(captured['cmd'][0], '/usr/bin/git')
        self.assertIn('+refs/heads/main:refs/remotes/origin/main', captured['cmd'])
        self.assertNotIn('GIT_CONFIG_COUNT', captured['env'])
        self.assertNotIn('SSH_AUTH_SOCK', captured['env'])
        self.assertEqual(captured['env']['GIT_CONFIG_GLOBAL'], '/dev/null')

    def test_shared_worker_identity_refuses_even_before_path_access(self):
        with patch.dict(os.environ, {'FORGE_OBSERVER_WORKER_UIDS':str(os.geteuid())}):
            with self.assertRaises(IsolationError): check_runtime('/nonexistent')

    def test_unknown_ancestry_is_error_not_force_push_evidence(self):
        with patch('observer.gitwatch._git', return_value=SimpleNamespace(returncode=128)):
            with self.assertRaises(RuntimeError): gitwatch.is_ancestor('/mirror', 'a'*40, 'b'*40)


class CoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = self.temp.name
        self.repo = Repo('r', 'X/y', '/build', True, '/trusted/mirror.git')
        self.old, self.new = 'a'*40, 'b'*40

    def test_checkpoint_requires_identity_bound_baseline_and_receipt(self):
        with self.assertRaises(ValueError): coverage.verify_checkpoint(self.repo, self.old, self.state)
        coverage.declare_baseline(self.repo, self.old, self.state, 'explicit offline fixture')
        coverage.verify_checkpoint(self.repo, self.old, self.state)
        with self.assertRaises(ValueError): coverage.verify_checkpoint(self.repo, self.new, self.state)
        coverage.record_scan(self.repo, C(self.new), '+content', Verdict('benign','safe','llm'), self.state)
        coverage.verify_checkpoint(self.repo, self.new, self.state)
        other = Repo('r', 'X/other', '/build', True, '/trusted/mirror.git')
        with self.assertRaises(ValueError): coverage.verify_checkpoint(other, self.old, self.state)
        moved = Repo('r', 'X/y', '/build', True, '/other/mirror.git')
        with self.assertRaises(ValueError): coverage.verify_checkpoint(moved, self.old, self.state)

    def test_incomplete_or_previous_baseline_receipt_cannot_advance(self):
        coverage.declare_baseline(self.repo, self.old, self.state, 'first baseline')
        coverage.record_scan(self.repo, C(self.new), 'diff', Verdict('unknown','model failed','none',False), self.state)
        with self.assertRaises(ValueError): coverage.verify_checkpoint(self.repo, self.new, self.state)
        coverage.record_scan(self.repo, C(self.new), 'diff', Verdict('benign','safe','llm'), self.state)
        coverage.declare_baseline(self.repo, self.old, self.state, 'deliberately new coverage epoch')
        with self.assertRaises(ValueError): coverage.verify_checkpoint(self.repo, self.new, self.state)

    def test_scan_receipt_exists_before_reporting_or_advancing(self):
        coverage.declare_baseline(self.repo, self.old, self.state, 'fixture baseline')
        git = FakeGit(self.new, commits=[C(self.new)], diffs={self.new:'+content'})
        git._pre = self.old
        deps, _ = deps_with(git)
        deps['coverage'] = coverage
        class Report:
            @staticmethod
            def report(*args):
                receipt = json.loads(coverage.receipt_path(self.repo,self.new,self.state).read_text())
                self.assertTrue(receipt['complete'])
                self.assertEqual(git.written,{})
        deps['report'] = Report
        result = run.scan_repo(self.repo,self.state,'/claude','/notify',deps=deps)
        self.assertEqual(result['status'],'scanned')
        self.assertEqual(git.written['r'],self.new)

    def test_dry_run_does_not_fetch_write_receipts_or_clear_failures(self):
        git = FakeGit(self.old); git._pre = self.old
        git.fetch = lambda *args: self.fail('dry run fetched')
        deps, _ = deps_with(git)
        failure = Path(self.state,'r.failcount'); failure.write_text('4')
        before = sorted(Path(self.state).iterdir())
        result = run.scan_repo(self.repo,self.state,'/claude','/notify',deps=deps,dry_run=True)
        self.assertEqual(result['status'],'up_to_date')
        self.assertEqual(failure.read_text(),'4')
        self.assertEqual(sorted(Path(self.state).iterdir()),before)
        self.assertEqual(git.written,{})

    def test_invalid_checkpoint_text_and_path_are_rejected(self):
        Path(self.state,'r.sha').write_text('--all\n')
        with self.assertRaises(ValueError): gitwatch.read_sha('r',self.state)
        with self.assertRaises(ValueError): gitwatch.write_sha('../escape',self.old,self.state)

    def test_degraded_cli_returns_nonzero_and_dry_run_creates_no_state(self):
        repo = self.repo
        before = sorted(Path(self.state).iterdir())
        summary = {'repo':'r','status':'checkpoint_unverified','scanned':0,'alerts':0,'note':'missing provenance'}
        import contextlib
        import io
        with patch('observer.run.check_runtime'), patch('observer.run.config.STATE_DIR',self.state), patch('observer.run.config.load_repos',return_value=[repo]), patch('observer.run.config.resolve_claude',return_value=None), patch('observer.run.scan_repo',return_value=summary), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(run.main(['--dry-run']),1)
        self.assertEqual(sorted(Path(self.state).iterdir()),before)

    def test_duplicate_repository_or_shared_mirror_declarations_are_rejected(self):
        from observer.config import load_repos
        directory = Path(self.state,'repos'); directory.mkdir()
        Path(directory,'one.repo').write_text('REPO="X/y"\nENABLED=true\nOBSERVER_MIRROR="/trusted/one"\n')
        second = Path(directory,'two.repo')
        second.write_text('REPO="X/y"\nENABLED=true\nOBSERVER_MIRROR="/trusted/two"\n')
        with self.assertRaises(ValueError): load_repos(directory)
        second.write_text('REPO="X/z"\nENABLED=true\nOBSERVER_MIRROR="/trusted/one"\n')
        with self.assertRaises(ValueError): load_repos(directory)
