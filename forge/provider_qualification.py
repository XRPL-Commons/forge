"""Native Claude API-key qualification using synthetic host credentials only.

The installed WorkerBoundary and gateway carry a real pinned CLI tool loop.
No provider API, login, repository or installation state is touched.
"""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import platform
from pathlib import Path
import subprocess
import tempfile
import textwrap
import threading
import time
import uuid

from .factory.runner import ContainerRunner, process_isolation
from .factory.worker_boundary import BoundaryError, WorkerBoundary
from .provider_proxy import Assignment, Broker, SessionServer, claude_environment

MODEL = 'claude-sonnet-4-6'
CAPABILITY = 'synthetic-job-capability-for-qualification'
HOST_KEY = 'synthetic-host-key-for-qualification'
DIAGNOSTIC_PREFIX = 'forge-provider-diagnostic:'
WORKER_PHASES = ('assignment', 'credentials', 'cli_version', 'route_denial',
                 'capability_denial', 'shell_fixture', 'tool_loop', 'tool_result', 'complete')
SHELL_FAILURES = {
    'shell environment exposes credential': 'shell_environment',
    'protected credential file is readable': 'private_file',
    'public assignment carries credential': 'public_assignment',
    'parent processes remain visible': 'pid_namespace',
    'parent procfs exposes the harness': 'parent_procfs',
    'process environment exposes credential': 'process_environment',
    'Permission denied': 'tool_permission_denied',
}
UPSTREAM_FAILURES = ('route', 'host_key', 'caller_authorization', 'model',
                     'shell_result_failed', 'shell_proof_missing', 'invalid_request', 'transport')


def failure_diagnostic(docker, worker, phase, code, upstream):
    """Only fixture enums and bounded counts survive cleanup, never raw CLI output."""
    details = dict(phase=phase, upstream_requests=min(len(upstream.requests), 1000),
                   worker_exit=int(code) if isinstance(code, str) and code.isdecimal() and len(code) <= 3 else None)
    for field, choices in (('failure', UPSTREAM_FAILURES), ('shell_failure', SHELL_FAILURES.values())):
        value = getattr(upstream, field, None)
        if value in choices:
            details['upstream_' + field] = value
    try:
        captured = docker(['logs', '--tail', '32', worker], check=False, timeout=10).stdout
        if isinstance(captured, bytes): captured = captured.decode(errors='replace')
        for line in captured[-16384:].splitlines():
            if not line.startswith(DIAGNOSTIC_PREFIX): continue
            try: value = json.loads(line[len(DIAGNOSTIC_PREFIX):])
            except ValueError: continue
            if not isinstance(value, dict): continue
            if value.get('phase') in WORKER_PHASES: details['worker_phase'] = value['phase']
            if value.get('error') in ('AssertionError', 'TimeoutExpired', 'FileNotFoundError',
                                      'PermissionError', 'ConnectionError', 'OSError', 'ValueError'):
                details['worker_error'] = value['error']
            for key in ('cli_exit', 'event_count'):
                if type(value.get(key)) is int and -255 <= value[key] <= 10000:
                    details[key] = value[key]
    except (OSError, subprocess.SubprocessError):
        details['worker_diagnostics'] = 'unavailable'
    return json.dumps(details, sort_keys=True, separators=(',', ':'))


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            assert self.path == '/v1/messages?beta=true', 'route'
            assert self.headers.get('x-api-key') == HOST_KEY, 'host_key'
            assert self.headers.get('Authorization') is None, 'caller_authorization'
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model'] == MODEL, 'model'
            self.server.requests.append(body)
            results = [part for message in body.get('messages', [])
                       if isinstance(message.get('content'), list) for part in message['content']
                       if part.get('type') == 'tool_result']
            if results:
                output = json.dumps(results)
                self.server.shell_failure = next((code for message, code in SHELL_FAILURES.items() if message in output), None)
                assert not any(result.get('is_error') for result in results), 'shell_result_failed'
                assert 'forge-shell-private' in output, 'shell_proof_missing'
            tool, arguments = ('StructuredOutput', {'ready': True}) if results else ('Bash', {
                'command': 'python3 /work/repository/proxy-privacy.py',
                'description':'Verify that shell tools cannot read inference credentials'})
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            self.event('message_start', {'type':'message_start','message':{'id':'fixture','type':'message',
                'role':'assistant','model':MODEL,'content':[],'stop_reason':None,'stop_sequence':None,
                'usage':{'input_tokens':1,'output_tokens':0}}})
            self.event('content_block_start', {'type':'content_block_start','index':0,
                'content_block':{'type':'tool_use','id':'tool_'+str(len(self.server.requests)),'name':tool,'input':{}}})
            self.event('content_block_delta', {'type':'content_block_delta','index':0,
                'delta':{'type':'input_json_delta','partial_json':json.dumps(arguments)}})
            self.event('content_block_stop', {'type':'content_block_stop','index':0})
            self.event('message_delta', {'type':'message_delta','delta':{'stop_reason':'tool_use','stop_sequence':None},
                'usage':{'output_tokens':8}})
            self.event('message_stop', {'type':'message_stop'})
        except (AssertionError, ValueError, KeyError, TypeError, OSError) as error:
            self.server.failed = True
            self.server.failure = (str(error) if isinstance(error, AssertionError) and str(error) in UPSTREAM_FAILURES
                                   else 'transport' if isinstance(error, OSError) else 'invalid_request')
            self.close_connection = True

    def event(self, name, value):
        self.wfile.write(('event: '+name+'\ndata: '+json.dumps(value)+'\n\n').encode()); self.wfile.flush()


SHELL_CHECK = r'''import json,os
from pathlib import Path
keys = {'ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_OAUTH_TOKEN'}
assert not any(os.environ.get(key) for key in keys), 'shell environment exposes credential'
def unreadable(path):
    try: Path(path).read_bytes()
    except OSError: return
    raise AssertionError('protected credential file is readable')
unreadable('/work/credentials.json')
unreadable('/work/home/proxy-private')
# The public assignment is never a credential envelope, even if readable.
try: assignment = json.loads(Path('/run/forge-input/assignment.json').read_text())
except OSError: pass
else: assert not ({'capability'} | keys).intersection(assignment), 'public assignment carries credential'
outer = json.loads(Path('/work/repository/namespace.json').read_text())
assert os.readlink('/proc/self/ns/pid') != outer['pid_namespace'], 'parent processes remain visible'
# A private PID namespace and its own procfs must hide the credential-bearing
# harness, including alternate /proc/<pid>/root and fd paths into its files.
for process in Path('/proc').iterdir():
    if not process.name.isdigit(): continue
    try: namespace = os.readlink(process / 'ns/pid')
    except OSError: namespace = None
    assert namespace != outer['pid_namespace'], 'parent procfs exposes the harness'
    try: values = (process / 'environ').read_bytes().split(b'\0')
    except OSError: values = []
    assert not any(value.split(b'=',1)[0].decode(errors='ignore') in keys for value in values), 'process environment exposes credential'
    unreadable(process / 'root/work/home/proxy-private')
    unreadable(process / 'root/work/credentials.json')
print('forge-shell-private')
'''


def handoff(docker, worker):
    """Use production's private tar envelope, with only a synthetic capability."""
    runner = ContainerRunner({}, environ={})
    runner.call = docker
    runner._send_credentials({'id': worker}, {'id': worker},
        claude_environment({}, 'http://forge-provider:8082', CAPABILITY, auth_mode='api_key'))


_WORKER = r'''
import http.client,json,os,subprocess
from pathlib import Path
config=json.loads(Path('/run/forge-input/assignment.json').read_text())
assert 'capability' not in config
phase='credentials'
credentials=json.loads(Path('/work/credentials.json').read_text())
assert set(credentials)=={'ANTHROPIC_BASE_URL','ANTHROPIC_AUTH_TOKEN'}
Path('/work/credentials.json').unlink()
assert not Path('/work/credentials.json').exists()
phase='cli_version'
assert subprocess.check_output(['claude','--version'],text=True).startswith('2.1.273 ')
assert not Path('/provider').exists()
phase='route_denial'
for host in ('api.anthropic.com','api.openai.com','claude.ai'):
    c=http.client.HTTPConnection('forge-egress',3128,timeout=10)
    c.request('CONNECT',host+':443');r=c.getresponse();assert r.status==403;r.read();c.close()
# A capability for another execution cannot borrow this gateway's account.
phase='capability_denial'
c=http.client.HTTPConnection('forge-provider',8082,timeout=10)
c.request('POST','/v1/messages',json.dumps({'model':config['model']}),{'Authorization':'Bearer wrong-job'})
r=c.getresponse();assert r.status==403;r.read();c.close()
phase='shell_fixture'
Path('/work/repository').mkdir();Path('/work/home').mkdir()
Path('/work/repository/proxy-privacy.py').write_text(Path('/run/forge-input/proxy-privacy.py').read_text())
Path('/work/repository/namespace.json').write_text(json.dumps({'pid_namespace':os.readlink('/proc/self/ns/pid')}))
# Positive control: the harness can read this private fixture before Bash's
# sandbox masks the home. The shell check must fail if that file remains visible.
private=Path('/work/home/proxy-private');private.write_text(credentials['ANTHROPIC_AUTH_TOKEN'])
assert private.read_text()==credentials['ANTHROPIC_AUTH_TOKEN']
env={**os.environ,'HOME':'/work/home','PATH':'/opt/forge/claude-tools:'+os.environ['PATH'],
 **credentials,
 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB':'1','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_AUTOUPDATER':'1'}
schema=json.dumps({'type':'object','properties':{'ready':{'type':'boolean'}},'required':['ready'],'additionalProperties':False})
args=['claude','-p','Run the requested shell privacy check, then return ready true.',
 '--output-format','stream-json','--verbose','--json-schema',schema,'--model',config['model'],
 '--tools','Bash','--allowedTools','Bash','--permission-mode','acceptEdits','--setting-sources','','--strict-mcp-config','--mcp-config',
 '{"mcpServers":{}}','--no-session-persistence','--add-dir','/work/repository']
phase='tool_loop'
diagnostic()
# Match the production adapter: scrub mode protects scripts in its launch
# directory. The repository remains an added working directory for Bash.
run=subprocess.run(args,env=env,cwd='/opt/forge/claude-control',capture_output=True,text=True,timeout=120)
phase='tool_result'
events=[json.loads(line) for line in run.stdout.splitlines() if line.startswith('{')]
result=next((event for event in reversed(events) if event.get('type')=='result'),{})
diagnostic(cli_exit=run.returncode,event_count=len(events))
assert run.returncode==0 and result.get('structured_output')=={'ready':True}, 'native tool loop failed'
phase='complete'
print(json.dumps(dict(pinned_claude_tool_loop=True,shell_credentials_absent=True,
                     private_files_inaccessible=True,parent_processes_inaccessible=True,consumed_credential_envelope=True,
                     direct_provider_routes_denied=True,other_job_capability_denied=True)))
'''
WORKER = '''import json
phase='assignment'
def diagnostic(**values):
    print('forge-provider-diagnostic:'+json.dumps(dict(phase=phase,**values)),flush=True)
try:
''' + textwrap.indent(_WORKER, '    ') + '''
except Exception as error:
    diagnostic(error=type(error).__name__)
    raise SystemExit(1)
'''


def qualify(image, *, docker=None):
    if platform.system() != 'Linux':
        raise BoundaryError('Native provider qualification requires the Linux Docker host')
    def call(args, **kwargs):
        return subprocess.run(['docker','--host','unix:///var/run/docker.sock',*args],capture_output=True,text=not isinstance(kwargs.get('input'), bytes),
            check=kwargs.pop('check',True),timeout=kwargs.pop('timeout',90),**kwargs)
    docker = docker or call
    boundary = WorkerBoundary(docker, image=image, work_bytes=256*1024**2)
    boundary.qualify()
    observed = json.loads(docker(['image','inspect',image]).stdout)[0]
    if observed['Id'] != image or observed.get('Config',{}).get('Labels',{}).get('org.forge.worker-boundary') != '2':
        raise BoundaryError('Qualification requires the exact worker-boundary v2 image ID')
    prefix = 'forge-provider-qualification-' + uuid.uuid4().hex
    job = dict(id=prefix, execution_fence=prefix)
    labels = {'forge.qualification':prefix}
    boundary.uplink = prefix+'-uplink'
    worker = prefix+'-worker'
    result = dict(image=image,provider='claude',auth_mode='api_key',
                  engine=docker(['version','--format','{{.Server.Version}}']).stdout.strip())
    with tempfile.TemporaryDirectory(prefix='fp-',dir='/tmp') as temporary:
        root = Path(temporary); inputs = root/'inputs'; inputs.mkdir(mode=0o755)
        target = inputs/'assignment.json'; target.write_text(json.dumps(dict(model=MODEL))); target.chmod(0o444)
        check = inputs/'proxy-privacy.py'; check.write_text(SHELL_CHECK); check.chmod(0o444)
        upstream = ThreadingHTTPServer(('127.0.0.1',0),Upstream)
        upstream.requests, upstream.failed = [], False
        alive = threading.Event(); alive.set()
        broker = Broker(Assignment(job['id'],'synthetic',MODEL,time.time()+180,CAPABILITY),
            api_key=lambda:HOST_KEY,authorized=lambda _:alive.is_set(),
            connection=lambda:http.client.HTTPConnection('127.0.0.1',upstream.server_port,timeout=30))
        server = SessionServer(root/'provider/session.sock',broker)
        threads = [threading.Thread(target=item.serve_forever,daemon=True) for item in (upstream,server)]
        for thread in threads: thread.start()
        boundary.provider_directory = root/'provider'
        phase, code = 'boundary', None
        try:
            docker(['network','create','--driver','bridge',*boundary.labels(labels),boundary.uplink])
            spec = boundary.prepare(job,input_directory=inputs,broker_socket_directory=None)
            phase = 'gateway_readiness'
            docker(['exec',spec.gateway,'/usr/local/bin/python3','-I','-c',
                'import socket,time\nfor attempt in range(50):\n try: socket.create_connection(("forge-provider",8082),1).close(); break\n except OSError:\n  if attempt==49: raise\n  time.sleep(.1)'])
            phase = 'worker_creation'
            docker(['create','--pull=never','--name',worker,*boundary.labels(WorkerBoundary.identity(job)[1]),
                '--read-only','--user','1000:1000','--cap-drop','ALL','--security-opt','no-new-privileges',
                *process_isolation(dict(provider='claude',apparmor_profile='forge-runner-private-proc')),
                '--pids-limit','256','--memory','1g','--memory-swap','1g','--cpus','1',
                '--tmpfs','/tmp:rw,nosuid,nodev,size=128m',*spec.worker_args,
                *[item for key,value in spec.environment.items() for item in ('--env',key+'='+value)],
                '--entrypoint','/usr/local/bin/python3',image,'-I','-c',WORKER])
            phase = 'credential_handoff'
            handoff(docker, worker)
            phase = 'worker_execution'
            docker(['start',worker]); code=docker(['wait',worker],timeout=150).stdout.strip()
            if code != '0' or upstream.failed or len(upstream.requests) < 2:
                raise BoundaryError('Native Claude proxy/tool privacy qualification failed')
            result['checks'] = json.loads(docker(['logs',worker]).stdout.strip().splitlines()[-1])
            result['checks']['host_only_key_injection'] = True
            phase = 'revocation'
            alive.clear()
            docker(['exec',spec.gateway,'/usr/local/bin/python3','-I','-c',
                'import http.client,json; c=http.client.HTTPConnection("forge-provider",8082,timeout=5); '
                'c.request("POST","/v1/messages",json.dumps({"model":'+repr(MODEL)+'}),'
                '{"Authorization":"Bearer "+'+repr(CAPABILITY)+'}); assert c.getresponse().status==403'])
            result['checks']['revoked_execution_denied'] = True
        except (BoundaryError, OSError, subprocess.SubprocessError, ValueError):
            reason = failure_diagnostic(docker, worker, phase, code, upstream)
            raise BoundaryError('Native Claude proxy/tool privacy qualification failed: ' + reason) from None
        finally:
            alive.clear()
            value=boundary.inspect('container',worker)
            if value:
                boundary.owned(value,WorkerBoundary.identity(job)[1],container=True)
                docker(['container','rm','--force',worker])
            boundary.cleanup(job)
            value=boundary.inspect('network',boundary.uplink)
            if value:
                boundary.owned(value,labels);docker(['network','rm',boundary.uplink])
            for item in (server,upstream): item.shutdown();item.server_close()
            for thread in threads: thread.join(timeout=2)
            if boundary.inspect('container',worker) or boundary.inspect('network',boundary.uplink):
                raise BoundaryError('Qualification cleanup is unconfirmed')
    result.update(status='qualified',cleanup='confirmed',observed_at=time.time())
    return result
