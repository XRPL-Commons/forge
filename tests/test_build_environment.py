import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.factory import build_environment as build
from forge.factory.contracts import result
from forge.factory.harness import schema
from forge.factory.github import GitHubUnavailable, GitHubBudgetExhausted
from tests.test_factory_cycle import CycleTest


class EnvironmentLifecycleTests(CycleTest):
    def test_environment_block_preserves_review_stage_candidate_and_budget_across_restart(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        job = self.active()
        candidate = job['candidate']
        report = {'version': 1, 'attempt': job['id'], 'role': 'review', 'outcome': 'blocked_environment',
                  'blocker': 'resources', 'summary': 'Dependency storage unavailable', 'candidate': candidate['key'],
                  'headline': 'Stopped: the worker could not reach its dependencies',
                  'plain': 'The worker could not download what it needs. Nothing was changed.'}
        self.engine.finish(job, report)
        self.assertFalse(self.cycle()['errors'])
        for _ in range(3):
            self.assertFalse(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['state'], 'hold')
        self.assertEqual(item['environment_hold']['state'], 'built')
        self.assertEqual(item['candidate'], candidate)
        self.assertEqual(item['failures'], [])
        self.assertEqual(len(self.ledger.read()['jobs']), 2)
        self.assertFalse(self.ledger.read()['slots'])
        self.config['image'] = 'sha256:' + 'b' * 64
        self.assertFalse(self.cycle()['errors'])
        self.assertEqual(self.active()['role'], 'review')
        self.assertEqual(self.active()['candidate'], candidate)

    def test_ci_unavailable_blocks_before_container_launch_without_spending_attempts(self):
        self.github.ci_evidence = Mock(side_effect=GitHubUnavailable('Forbidden'))
        self.assertFalse(self.cycle()['errors'])
        self.cycle()
        self.assertFalse(self.engine.jobs)
        self.assertFalse(self.ledger.read()['slots'])
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['environment_hold']['blocker'], 'ci_evidence')
        self.assertEqual(item['failures'], [])

    def test_ci_budget_defers_same_launch_without_fencing_or_failure(self):
        self.github.ci_evidence = Mock(side_effect=GitHubBudgetExhausted('Cycle ended'))
        self.assertFalse(self.cycle()['errors'])
        job = self.active()
        self.assertEqual(job['phase'], 'reserved')
        self.assertFalse(job['fenced'])
        self.assertFalse(self.engine.jobs)
        self.assertNotIn('environment_hold', self.ledger.read()['issues'][self.key])
        self.github.ci_evidence = Mock(return_value=None)
        self.assertFalse(self.cycle()['errors'])
        self.assertEqual(self.active()['id'], job['id'])
        self.assertEqual(self.active()['phase'], 'running')

    def test_ci_hold_recovers_without_image_change_and_backs_off_while_unavailable(self):
        from forge.factory.supervisor import Supervisor
        from tests.test_factory_cycle import Settings
        self.github.ci_evidence = Mock(side_effect=GitHubUnavailable('Forbidden'))
        self.cycle()
        # Teardown first. A failed recheck sets a bounded retry time.
        self.cycle()
        held = self.ledger.read()['issues'][self.key]
        retry_at = held['environment_hold']['recheck_after']
        calls = self.github.ci_evidence.call_count
        self.cycle()
        self.assertEqual(self.github.ci_evidence.call_count, calls)
        self.github.ci_evidence = Mock(return_value=None)
        controller = Supervisor(self.ledger, self.engine, self.github, Settings(), self.config,
                                clock=lambda: retry_at + 1)
        self.assertFalse(controller.cycle()['errors'])
        self.assertEqual(self.active()['phase'], 'running')
        self.assertEqual(self.ledger.read()['issues'][self.key]['failures'], [])

    def test_budget_during_ci_hold_recheck_does_not_set_backoff_or_release_hold(self):
        self.github.ci_evidence = Mock(side_effect=GitHubUnavailable('Forbidden'))
        self.cycle()
        self.github.ci_evidence = Mock(side_effect=GitHubBudgetExhausted('Cycle ended'))
        self.assertFalse(self.cycle()['errors'])
        held = self.ledger.read()['issues'][self.key]['environment_hold']
        self.assertNotIn('recheck_after', held)
        self.assertFalse(self.engine.jobs)

    def test_code_rejection_still_consumes_budget(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review', 'reject'); self.cycle()
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(self.ledger.failure_count(item), 1)
        self.assertNotIn('environment_hold', item)

    def test_container_oom_is_resource_blocker_not_code_rejection(self):
        self.cycle(); job = self.active()
        self.engine.capture = lambda _: {'result': None, 'execution': {'oom_killed': True}, 'logs': '', 'missing': []}
        self.engine.finish(job)
        self.assertFalse(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['environment_hold']['blocker'], 'resources')
        self.assertEqual(item['failures'], [])

    def test_untyped_blocker_is_not_accepted(self):
        self.cycle(); job = self.active()
        report = {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'blocked_environment', 'summary': 'Missing',
                  'headline': 'Stopped: a tool is missing', 'plain': 'The worker cannot run without it.'}
        with self.assertRaises(ValueError):
            result(report, job)
        report['blocker'] = 'test_failure'
        with self.assertRaises(ValueError):
            result(report, job)
        report['blocker'] = 'toolchain'
        self.assertEqual(result(report, job), report)
        self.assertIn('blocker', schema(job)['required'])


class BuildToolsTests(unittest.TestCase):
    def test_native_tool_environment_is_explicit_and_excludes_provider_credentials(self):
        import tomllib
        from forge.harnesses.codex import adapter
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'repository/.forge/home').mkdir(parents=True)
            def path(value):
                return root / value.removeprefix('/work/')
            with patch.object(adapter, 'Path', side_effect=path), \
                    patch.object(adapter.runpy, 'run_path', return_value={'environment': lambda _: {'TMPDIR': str(root / 'tmp')}}):
                env = adapter.prepare_tool_environment(root / 'repository', {
                    'GH_TOKEN': 'job-scoped', 'OPENAI_API_KEY': 'private-provider-key',
                    'FORGE_PROBE_SECRET': 'private-parent-secret'})
            policy = tomllib.loads((root / 'home/.codex/config.toml').read_text())['shell_environment_policy']
            self.assertEqual(policy['inherit'], 'none')
            self.assertEqual(policy['set'], env)
            self.assertIn('/usr/local/bin', env['PATH'])
            self.assertEqual(env['GH_TOKEN'], 'job-scoped')
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('FORGE_PROBE_SECRET', env)

    def test_failed_fresh_install_canary_never_installs_services_or_success_marker(self):
        import io
        from forge import host_setup
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(host_setup.platform, 'system', return_value='Linux'), \
                patch.object(host_setup.os, 'getuid', return_value=1000), \
                patch.object(host_setup, 'apparmor_enabled', return_value=True), \
                patch.object(host_setup.shutil, 'which', return_value='/bin/tool'), \
                patch.object(host_setup, 'command', return_value=Mock(stdout='sha256:' + 'a'*64)), \
                patch.object(host_setup.urllib.request, 'urlopen', return_value=io.BytesIO(b'policy')), \
                patch.object(host_setup.shutil, 'copyfile'), \
                patch.object(host_setup, 'Onboarding') as onboarding, \
                patch.object(host_setup, 'install_services') as services, \
                patch.object(host_setup.runpy, 'run_path') as script:
            onboarding.return_value.runtime.return_value = {}
            validate = Mock(side_effect=RuntimeError('Browser unavailable'))
            script.side_effect = [{'generate': lambda *args: {}}, {'validate': validate}]
            with self.assertRaisesRegex(RuntimeError, 'Browser unavailable'):
                host_setup.install(Path(temporary))
            services.assert_not_called()
            self.assertFalse((Path(temporary) / 'host-runtime.json').exists())
            self.assertTrue(validate.called)

    def test_cache_and_temp_use_repository_disk(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(build.shutil, 'copytree'):
            env = build.environment(temporary)
            for key in ('TMPDIR', 'npm_config_cache', 'npm_config_store_dir', 'COREPACK_HOME'):
                self.assertTrue(env[key].startswith(temporary + '/'))
            self.assertEqual(env['PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH'], '/usr/bin/chromium')

    def test_safe_build_refuses_an_unbounded_host(self):
        with patch.object(Path, 'read_text', return_value='max'):
            with self.assertRaisesRegex(RuntimeError, 'finite container memory'):
                build.memory_limit()

    def test_timeout_kills_descendants_and_caps_heap(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(build.os.environ, {'TMPDIR': temporary, 'NODE_OPTIONS': '--max-old-space-size=99999'}), \
                patch.object(build, 'memory_limit', return_value=2*1024**3), patch.object(build.subprocess, 'Popen') as start, patch.object(build.os, 'killpg') as kill:
            child = start.return_value; child.pid = 123
            child.wait.side_effect = [subprocess.TimeoutExpired('build', 1), 0]
            self.assertEqual(build.safe_build(['node', 'build.js']), 124)
            self.assertEqual(start.call_args.kwargs['env']['NODE_OPTIONS'], '--max-old-space-size=1228')
            kill.assert_called_once_with(123, build.signal.SIGKILL)
