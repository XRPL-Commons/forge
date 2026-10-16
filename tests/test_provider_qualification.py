import io
import json
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.provider_qualification import CAPABILITY, HOST_KEY, SHELL_CHECK, WORKER, handoff, qualify
from forge.factory.worker_boundary import BoundaryError
from forge_client.boundary_qualification import execute
from tests.test_worker_boundary import Engine


class ProviderQualificationTests(unittest.TestCase):
    def test_requires_native_linux_before_any_mutation(self):
        engine = Engine()
        with patch('forge.provider_qualification.platform.system', return_value='Darwin'):
            with self.assertRaisesRegex(BoundaryError, 'Linux'):
                qualify('sha256:'+'a'*64, docker=engine)
        self.assertEqual(engine.calls, [])

    def test_requires_supported_engine_before_mutation(self):
        engine = Engine(); engine.version = '27.0.0'
        with patch('forge.provider_qualification.platform.system', return_value='Linux'):
            with self.assertRaisesRegex(BoundaryError, 'Engine 28'):
                qualify('sha256:'+'a'*64, docker=engine)
        self.assertEqual(len(engine.calls), 1)
        self.assertFalse(any(engine.resources.values()))

    def test_supported_cli_selects_provider_canary_explicitly(self):
        args = SimpleNamespace(image='sha256:'+'a'*64, ssh='anvil', apply=True, provider_proxy=True)
        installation = Mock(); installation.journal.read.return_value = {}
        installation.host.rpc.return_value = {'result':dict(status='qualified', cleanup='confirmed', image=args.image)}
        execute(args, installation=installation)
        self.assertTrue(installation.host.rpc.call_args.args[1]['provider_proxy'])
        args.apply = False
        report = execute(args)
        self.assertIn('shell credential isolation', report['checks'])

    def test_real_envelope_handoff_is_private_and_has_no_host_key(self):
        docker = Mock()
        handoff(docker, 'fixture-worker')
        args, kwargs = docker.call_args
        self.assertEqual(args[0], ['cp','-a','-','fixture-worker:/work/'])
        with tarfile.open(fileobj=io.BytesIO(kwargs['input'])) as archive:
            member = archive.getmember('credentials.json')
            self.assertEqual((member.mode,member.uid,member.gid), (0o600,1000,1000))
            credentials = json.loads(archive.extractfile(member).read())
        self.assertEqual(credentials, {'ANTHROPIC_BASE_URL':'http://forge-provider:8082',
                                      'ANTHROPIC_AUTH_TOKEN':CAPABILITY})
        self.assertNotIn(HOST_KEY.encode(), kwargs['input'])

    def privacy(self, *, exposed_file=None, environment=b'PATH=/usr/bin\0', namespace='pid:[private]', process_namespace=None):
        files = {
            '/run/forge-input/assignment.json': json.dumps({'model':'claude-sonnet-4-6'}).encode(),
            '/work/repository/namespace.json': json.dumps({'pid_namespace':'pid:[outer]'}).encode(),
            '/proc/42/environ': environment,
        }
        if exposed_file: files[exposed_file] = b'private-data'
        class FixturePath:
            def __init__(self, value): self.value = str(value)
            def __str__(self): return self.value
            def __fspath__(self): return self.value
            @property
            def name(self): return self.value.rsplit('/',1)[-1]
            def __truediv__(self, child): return FixturePath(self.value+'/'+child)
            def read_bytes(self):
                if self.value not in files: raise PermissionError('unreadable')
                return files[self.value]
            def read_text(self): return self.read_bytes().decode()
            def iterdir(self): return iter([FixturePath('/proc/42')])
        with patch('pathlib.Path', FixturePath), patch.dict('os.environ', {}, clear=True), \
                patch('os.readlink', side_effect=lambda path: namespace if str(path)=='/proc/self/ns/pid' else process_namespace or namespace), patch('builtins.print'):
            exec(SHELL_CHECK, {})

    def test_bash_privacy_probe_checks_files_processes_and_alternate_roots(self):
        self.privacy()
        for path in ('/work/credentials.json','/work/home/proxy-private',
                     '/proc/42/root/work/home/proxy-private','/proc/42/root/work/credentials.json'):
            with self.subTest(path=path), self.assertRaisesRegex(AssertionError, 'file is readable'):
                self.privacy(exposed_file=path)
        with self.assertRaisesRegex(AssertionError, 'process environment'):
            self.privacy(environment=b'ANTHROPIC_AUTH_TOKEN=synthetic-leak\0')
        with self.assertRaisesRegex(AssertionError, 'parent processes'):
            self.privacy(namespace='pid:[outer]')
        with self.assertRaisesRegex(AssertionError, 'parent procfs'):
            self.privacy(process_namespace='pid:[outer]')

    def test_worker_program_compiles(self):
        compile(WORKER, '<provider-qualification-worker>', 'exec')
        compile(SHELL_CHECK, '<provider-qualification-shell>', 'exec')


if __name__ == '__main__': unittest.main()
