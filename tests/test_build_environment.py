from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forge.factory import build_environment as build
from forge.factory.contracts import result
from forge.factory.harness import schema


class BlockerContractTests(unittest.TestCase):
    def test_untyped_blocker_is_not_accepted(self):
        job = {'id': 'attempt-1', 'role': 'build'}
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
                    patch.object(adapter.runpy, 'run_path', return_value={'environment': lambda _, services: {'TMPDIR': str(root / 'tmp'), **build.service_environment(services)}}):
                env = adapter.prepare_tool_environment(root / 'repository', {
                    'GH_TOKEN': 'job-scoped', 'OPENAI_API_KEY': 'private-provider-key',
                    'FORGE_PROBE_SECRET': 'private-parent-secret', 'FORGE_REQUIRED_SERVICES': 'mongodb'})
            policy = tomllib.loads((root / 'home/.codex/config.toml').read_text())['shell_environment_policy']
            self.assertEqual(policy['inherit'], 'none')
            self.assertEqual(policy['set'], env)
            self.assertIn('/usr/local/bin', env['PATH'])
            self.assertEqual(env['GH_TOKEN'], 'job-scoped')
            self.assertNotIn('OPENAI_API_KEY', env)
            self.assertNotIn('FORGE_PROBE_SECRET', env)
            self.assertEqual(env['MONGOMS_SYSTEM_BINARY'], '/usr/bin/mongod')

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
