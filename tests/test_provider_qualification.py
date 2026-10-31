import io
import json
from pathlib import Path
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.provider_qualification import (CAPABILITY, HOST_KEY, MODEL, SHELL_CHECK, WORKER,
    DIAGNOSTIC_PREFIX, SHELL_PREFIX, Upstream, failure_diagnostic, handoff, qualify, tool_diagnostic)
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

    def test_missing_or_relative_seccomp_is_refused_before_resource_creation(self):
        image = 'sha256:'+'a'*64
        def docker(args, **kwargs):
            return SimpleNamespace(stdout=json.dumps([{'Id':image,
                'Config':{'Labels':{'org.forge.worker-boundary':'2'}}}]))
        for policy in (None, 'relative.json', '/does-not-exist/runner-seccomp.json'):
            engine = Mock(side_effect=docker)
            with self.subTest(policy=policy), \
                    patch('forge.provider_qualification.platform.system', return_value='Linux'), \
                    patch('forge.provider_qualification.WorkerBoundary'):
                with self.assertRaisesRegex(BoundaryError, 'installed runner seccomp'):
                    qualify(image, docker=engine, seccomp=policy)
            self.assertEqual([call.args[0] for call in engine.call_args_list], [['image','inspect',image]])

    def test_native_entrypoint_forwards_exact_policy(self):
        from forge.worker_qualification import main
        with patch('sys.argv', ['qualification','--image','sha256:'+'a'*64,
                '--provider-proxy','--seccomp','/installed/runner-seccomp.json']), \
                patch('forge.provider_qualification.qualify', return_value={}) as probe, \
                patch('builtins.print'):
            self.assertEqual(main(), 0)
        probe.assert_called_once_with('sha256:'+'a'*64, seccomp='/installed/runner-seccomp.json')

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

    def privacy(self, *, exposed_file=None, environment=b'PATH=/usr/bin\0', namespace='pid:[private]', process_namespace=None,
                missing_file=None, diagnostics=None):
        files = {
            '/run/forge-input/assignment.json': json.dumps({'model':'claude-sonnet-4-6'}).encode(),
            '/work/repository/namespace.json': json.dumps({'pid_namespace':'pid:[outer]'}).encode(),
            '/proc/42/environ': environment,
        }
        if missing_file: files.pop(missing_file, None)
        if exposed_file: files[exposed_file] = b'private-data'
        class FixturePath:
            def __init__(self, value): self.value = str(value)
            def __str__(self): return self.value
            def __fspath__(self): return self.value
            @property
            def name(self): return self.value.rsplit('/',1)[-1]
            def __truediv__(self, child): return FixturePath(self.value+'/'+child)
            def read_bytes(self):
                if self.value not in files: raise PermissionError(13, 'unreadable', self.value)
                return files[self.value]
            def read_text(self): return self.read_bytes().decode()
            def iterdir(self): return iter([FixturePath('/proc/42')])
        with patch('pathlib.Path', FixturePath), patch.dict('os.environ', {}, clear=True), \
                patch('os.readlink', side_effect=lambda path: namespace if str(path)=='/proc/self/ns/pid' else process_namespace or namespace), \
                patch('builtins.print', side_effect=lambda *values, **kwargs: diagnostics.extend(values) if diagnostics is not None else None):
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

    def test_failure_report_retains_only_bounded_fixture_fields(self):
        secret = 'never-retain-this-provider-output'
        rows = [secret, DIAGNOSTIC_PREFIX + '{bad json', DIAGNOSTIC_PREFIX + json.dumps({
            'phase':'tool_result', 'cli_exit':1, 'event_count':2, 'error':'AssertionError',
            'stderr':secret, 'capability':CAPABILITY}),
            DIAGNOSTIC_PREFIX + json.dumps({'phase':secret, 'error':secret, 'event_count':1000000})]
        docker = Mock(return_value=SimpleNamespace(stdout='\n'.join(rows)))
        upstream = SimpleNamespace(requests=[{}, {}], failure='shell_result_failed', shell_failure='private_file')
        report = failure_diagnostic(docker, 'worker', 'worker_execution', '1', upstream)
        self.assertEqual(json.loads(report), dict(phase='worker_execution', worker_exit=1,
            upstream_requests=2, upstream_failure='shell_result_failed', upstream_shell_failure='private_file',
            worker_phase='tool_result', cli_exit=1, event_count=2, worker_error='AssertionError'))
        self.assertNotIn(secret, report)
        self.assertNotIn(CAPABILITY, report)
        self.assertEqual(docker.call_args.args[0], ['logs', '--tail', '32', 'worker'])
        self.assertEqual(docker.call_args.kwargs['timeout'], 10)

    def test_missing_worker_logs_still_report_phase_and_upstream_count(self):
        docker = Mock(side_effect=OSError('private diagnostic must not escape'))
        value = json.loads(failure_diagnostic(docker, 'worker', 'gateway_readiness', None,
            SimpleNamespace(requests=[], failure='arbitrary private data')))
        self.assertEqual(value, dict(phase='gateway_readiness', worker_exit=None,
            upstream_requests=0, worker_diagnostics='unavailable'))

    def upstream(self, results):
        value = object.__new__(Upstream)
        body = json.dumps({'model':MODEL, 'messages':[{'content':results}]}).encode()
        value.path = '/v1/messages?beta=true'
        value.headers = {'x-api-key':HOST_KEY, 'Content-Length':str(len(body))}
        value.rfile = io.BytesIO(body)
        value.wfile = io.BytesIO()
        value.server = SimpleNamespace(requests=[], failed=False)
        for name in ('send_response', 'send_header', 'end_headers', 'event'):
            setattr(value, name, Mock())
        value.do_POST()
        return value

    def test_failed_privacy_assertion_preserves_static_reason_without_tool_output(self):
        value = self.upstream([dict(type='tool_result', is_error=True,
            content='Traceback private-data: AssertionError: protected credential file is readable')])
        self.assertTrue(value.server.failed)
        self.assertEqual(value.server.failure, 'shell_result_failed')
        self.assertEqual(value.server.shell_failure, 'private_file')
        value.event.assert_not_called()
        value.send_response.assert_called_once_with(400)
        value.send_header.assert_any_call('x-should-retry', 'false')
        self.assertEqual(json.loads(value.wfile.getvalue())['error']['type'], 'invalid_request_error')
        value = self.upstream([dict(type='tool_result', content='unrecognized private output')])
        self.assertEqual(value.server.failure, 'shell_proof_missing')
        self.assertIsNone(value.server.shell_failure)

    def test_shell_os_failure_reports_phase_errno_and_script_location(self):
        output = []
        with self.assertRaises(PermissionError):
            self.privacy(missing_file='/work/repository/namespace.json', diagnostics=output)
        record = json.loads(output[-1].removeprefix(SHELL_PREFIX))
        self.assertEqual(record['phase'], 'namespace_fixture')
        self.assertEqual(record['error'], 'PermissionError')
        self.assertEqual(record['errno'], 13)
        self.assertEqual(record['function'], '<module>')
        self.assertIsInstance(record['line'], int)
        self.assertNotIn('namespace.json', output[-1])

    def test_synthetic_tool_diagnostics_redact_credentials_and_bound_unknown_launch_errors(self):
        token = 'sk-'+'a'*24
        text = ('x'*3000 + '\nError: Exit code 1\nbwrap: No such file or directory\n'
                + CAPABILITY+' '+HOST_KEY+' '+token+'\x1b[31m')
        report = tool_diagnostic([dict(type='tool_result', is_error=True, content=[{'type':'text', 'text':text}])])
        self.assertLessEqual(len(report['excerpt']), 2048)
        self.assertIn('bwrap: No such file or directory', report['excerpt'])
        for value in (CAPABILITY, HOST_KEY, token, '\x1b'):
            self.assertNotIn(value, json.dumps(report))
        marker = dict(phase='private_files', error='AssertionError', line=12, function='unreadable')
        report = tool_diagnostic([dict(content=SHELL_PREFIX+json.dumps(marker))])
        self.assertEqual({key:report[key] for key in marker}, marker)

    def test_worker_launch_matches_production_and_reports_failure_without_raw_cli_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = root/'run/forge-input'; inputs.mkdir(parents=True)
            (inputs/'assignment.json').write_text(json.dumps({'model':MODEL}))
            (inputs/'proxy-privacy.py').write_text(SHELL_CHECK)
            (root/'work').mkdir()
            (root/'work/credentials.json').write_text(json.dumps({
                'ANTHROPIC_BASE_URL':'http://forge-provider:8082', 'ANTHROPIC_AUTH_TOKEN':CAPABILITY}))
            output = io.StringIO()
            run = Mock(return_value=SimpleNamespace(returncode=1,
                stdout=json.dumps({'type':'result', 'message':'private-cli-stdout'})+'\n', stderr='private-cli-stderr'))
            connection = Mock(); connection.getresponse.return_value.status = 403
            with patch('pathlib.Path', side_effect=lambda value: root/str(value).lstrip('/')), \
                    patch('os.readlink', return_value='pid:[outer]'), patch.dict('os.environ', {'PATH':'/usr/bin'}, clear=True), \
                    patch('http.client.HTTPConnection', return_value=connection), \
                    patch('subprocess.check_output', return_value='2.1.273 (Claude Code)'), \
                    patch('subprocess.run', run), patch('sys.stdout', output), self.assertRaises(SystemExit):
                exec(WORKER, {})
            self.assertEqual(run.call_args.kwargs['cwd'], '/opt/forge/claude-control')
            self.assertFalse((root/'work/credentials.json').exists())
            value = output.getvalue()
            self.assertIn('"phase": "tool_result"', value)
            self.assertIn('"cli_exit": 1', value)
            self.assertIn('"error": "AssertionError"', value)
            for private in ('private-cli-stdout', 'private-cli-stderr', CAPABILITY, HOST_KEY):
                self.assertNotIn(private, value)

    def test_failed_native_worker_reports_diagnostics_before_confirmed_cleanup(self):
        image = 'sha256:'+'a'*64
        upstream, session = Mock(), Mock()
        upstream.server_port = 12345
        boundary = Mock()
        boundary.labels.return_value = []
        boundary.prepare.return_value = SimpleNamespace(gateway='gateway', worker_args=[], environment={})
        boundary.inspect.return_value = None
        actions, launches = [], []
        boundary.cleanup.side_effect = lambda job: actions.append('cleanup')
        def docker(args, **kwargs):
            if args[0] == 'create': launches.append(args)
            if args[:2] == ['image', 'inspect']:
                value = json.dumps([{'Id':image, 'Config':{'Labels':{'org.forge.worker-boundary':'2'}}}])
            elif args[0] == 'version': value = '29.0.0'
            elif args[0] == 'wait':
                upstream.requests = [{}, {}]
                upstream.failed, upstream.failure = True, 'shell_result_failed'
                upstream.shell_failure = 'pid_namespace'
                value = '1'
            elif args[0] == 'logs':
                actions.append('logs')
                value = DIAGNOSTIC_PREFIX + json.dumps({'phase':'tool_result','cli_exit':1,'event_count':3})
            else: value = ''
            return SimpleNamespace(stdout=value)
        with patch('forge.provider_qualification.platform.system', return_value='Linux'), \
                patch('forge.provider_qualification.WorkerBoundary', return_value=boundary) as cls, \
                patch('forge.provider_qualification.ThreadingHTTPServer', return_value=upstream), \
                patch('forge.provider_qualification.SessionServer', return_value=session), \
                tempfile.TemporaryDirectory() as directory:
            policy = Path(directory)/'runner-seccomp.json'; policy.write_text('{}')
            cls.identity.return_value = ('owned', {})
            with self.assertRaises(BoundaryError) as raised:
                qualify(image, docker=docker, seccomp=policy)
            self.assertEqual(launches[0].count('seccomp='+str(policy)), 1)
            self.assertNotIn('seccomp=unconfined', launches[0])
            self.assertIn('apparmor=forge-runner-private-proc', launches[0])
        report = json.loads(str(raised.exception).split(': ', 1)[1])
        self.assertEqual(report['worker_phase'], 'tool_result')
        self.assertEqual(report['upstream_requests'], 2)
        self.assertEqual(report['upstream_shell_failure'], 'pid_namespace')
        self.assertEqual(report['worker_exit'], 1)
        self.assertEqual(actions, ['logs','cleanup'])
        session.shutdown.assert_called_once()
        upstream.shutdown.assert_called_once()


if __name__ == '__main__': unittest.main()
