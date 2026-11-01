import importlib.util
import io
import json
from pathlib import Path
import signal
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from tests.test_release_artifacts import BASE, FULL, manifest

spec = importlib.util.spec_from_file_location('release_roundtrip',
    Path(__file__).resolve().parents[1] / 'tools/qualify_release_roundtrip.py')
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class RoundtripTests(unittest.TestCase):
    def test_daemons_never_share_builder_or_each_others_state(self):
        classic = tool.daemon_config(Path('/tmp/classic'), False)
        containerd = tool.daemon_config(Path('/tmp/containerd'), True)
        for key in ('hosts', 'data-root', 'exec-root', 'pidfile',
                    'containerd-namespace', 'containerd-plugins-namespace'):
            self.assertNotEqual(classic[key], containerd[key])
        self.assertFalse(classic['features']['containerd-snapshotter'])
        self.assertTrue(containerd['features']['containerd-snapshotter'])
        self.assertEqual(classic['storage-driver'], 'overlay2')
        self.assertNotIn('storage-driver', containerd)
        for config in (classic, containerd):
            self.assertEqual(config['bridge'], 'none')
            self.assertFalse(config['iptables'])
            self.assertFalse(config['ip6tables'])
            self.assertNotIn('containerd', config)  # Let this daemon own its child.

    def test_teardown_kills_daemon_group_even_when_graceful_exit_times_out(self):
        process = Mock(pid=1234)
        process.wait.side_effect = [subprocess.TimeoutExpired('dockerd', 45), 0]
        with patch.object(tool.os, 'killpg') as kill:
            tool.stop(process)
        self.assertEqual([item.args for item in kill.call_args_list],
                         [(1234, signal.SIGTERM), (1234, signal.SIGKILL)])
        self.assertEqual(process.wait.call_count, 2)

    def test_failed_qualification_still_tears_down_its_daemon(self):
        process = Mock()
        process.poll.return_value = None
        info = subprocess.CompletedProcess([], 0, json.dumps({'DriverStatus': [
            ['driver-type', 'io.containerd.snapshotter.v1']]}), '')
        with patch.object(tool.shutil, 'which', return_value='/usr/bin/dockerd'), \
                patch.object(tool.subprocess, 'Popen', return_value=process), \
                patch.object(tool.subprocess, 'run', return_value=info) as command, \
                patch.object(tool, 'stop') as stop, patch('sys.stderr', io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, 'image load failed'):
                with tool.isolated_daemon(True):
                    raise RuntimeError('image load failed')
        stop.assert_called_once_with(process)
        argv = command.call_args.args[0]
        self.assertEqual(argv[:2], ['docker', '--host'])
        self.assertTrue(argv[2].startswith('unix:///tmp/forge-rt-'))

    def test_exact_exports_load_through_consumer_then_run_without_network_or_pulls(self):
        value = manifest()
        calls, loaded = [], set()
        def docker(argv, **kwargs):
            calls.append(argv)
            if argv[1:3] == ['image', 'inspect']:
                image = argv[3]
                return subprocess.CompletedProcess(argv, 0 if image in loaded else 1,
                                                   image + ' linux/amd64\n' if image in loaded else '', '')
            if argv[1:3] == ['image', 'load']:
                self.assertEqual(Path(argv[-1]).read_bytes(), b'archive')
                loaded.add(BASE if BASE not in loaded else FULL)
            return subprocess.CompletedProcess(argv, 0, '', '')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for kind in ('runner', 'full'):
                (root / value['artifacts'][kind]['filename']).write_bytes(b'archive')
            result = tool.qualify_store(root, value, docker)
        self.assertEqual(result, {'runner': BASE, 'full': FULL})
        self.assertEqual(sum(argv[1:3] == ['image', 'load'] for argv in calls), 2)
        runs = [argv for argv in calls if argv[1] == 'run']
        self.assertEqual(len(runs), 2)
        for argv in runs:
            self.assertIn('--pull=never', argv)
            self.assertIn('--network=none', argv)
            self.assertIn('--read-only', argv)

    def test_corrupt_export_stops_before_load_or_execution(self):
        value = manifest()
        docker = Mock(return_value=subprocess.CompletedProcess([], 1, '', ''))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / value['artifacts']['runner']['filename']).write_bytes(b'corrupt')
            with self.assertRaisesRegex(ValueError, 'does not match'):
                tool.qualify_store(root, value, docker)
        self.assertFalse(any(item.args[0][1] in ('run', 'load') or item.args[0][1:3] == ['image', 'load']
                             for item in docker.call_args_list))

    def test_workflow_requires_roundtrip_before_publication(self):
        workflow = (Path(__file__).resolve().parents[1] / '.github/workflows/prepared-release.yml').read_text()
        self.assertLess(workflow.index('tools/build_release_artifacts.py'),
                        workflow.index('tools/qualify_release_roundtrip.py'))
        self.assertLess(workflow.index('tools/qualify_release_roundtrip.py'), workflow.index('gh release create'))


if __name__ == '__main__':
    unittest.main()
