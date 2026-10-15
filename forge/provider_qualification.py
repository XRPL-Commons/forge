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
import threading
import time
import uuid

from .factory.runner import process_isolation
from .factory.worker_boundary import BoundaryError, WorkerBoundary
from .provider_proxy import Assignment, Broker, SessionServer

MODEL = 'claude-sonnet-4-6'
CAPABILITY = 'synthetic-job-capability-for-qualification'
HOST_KEY = 'synthetic-host-key-for-qualification'


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            assert self.path == '/v1/messages?beta=true'
            assert self.headers.get('x-api-key') == HOST_KEY
            assert self.headers.get('Authorization') is None
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            assert body['model'] == MODEL
            self.server.requests.append(body)
            results = [part for message in body.get('messages', [])
                       if isinstance(message.get('content'), list) for part in message['content']
                       if part.get('type') == 'tool_result']
            if results:
                assert any('forge-shell-private' in json.dumps(result) for result in results)
                assert not any(result.get('is_error') for result in results)
            tool, arguments = ('StructuredOutput', {'ready': True}) if results else ('Bash', {
                'command': "python3 -c \"import os; assert not any(os.environ.get(k) for k in ['ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN','CLAUDE_CODE_OAUTH_TOKEN']); print('forge-shell-private')\"",
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
        except (AssertionError, ValueError, OSError):
            self.server.failed = True
            self.close_connection = True

    def event(self, name, value):
        self.wfile.write(('event: '+name+'\ndata: '+json.dumps(value)+'\n\n').encode()); self.wfile.flush()


WORKER = r'''
import http.client,json,os,subprocess
from pathlib import Path
config=json.loads(Path('/run/forge-input/assignment.json').read_text())
assert subprocess.check_output(['claude','--version'],text=True).startswith('2.1.273 ')
assert not Path('/provider').exists()
for host in ('api.anthropic.com','api.openai.com','claude.ai'):
    c=http.client.HTTPConnection('forge-egress',3128,timeout=10)
    c.request('CONNECT',host+':443');r=c.getresponse();assert r.status==403;r.read();c.close()
# A capability for another execution cannot borrow this gateway's account.
c=http.client.HTTPConnection('forge-provider',8082,timeout=10)
c.request('POST','/v1/messages',json.dumps({'model':config['model']}),{'Authorization':'Bearer wrong-job'})
r=c.getresponse();assert r.status==403;r.read();c.close()
Path('/work/repository').mkdir();Path('/work/home').mkdir()
env={**os.environ,'HOME':'/work/home','PATH':'/opt/forge/claude-tools:'+os.environ['PATH'],
 'ANTHROPIC_BASE_URL':'http://forge-provider:8082','ANTHROPIC_AUTH_TOKEN':config['capability'],
 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB':'1','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_AUTOUPDATER':'1'}
schema=json.dumps({'type':'object','properties':{'ready':{'type':'boolean'}},'required':['ready'],'additionalProperties':False})
args=['claude','-p','Run the requested shell privacy check, then return ready true.',
 '--output-format','stream-json','--verbose','--json-schema',schema,'--model',config['model'],
 '--tools','Bash','--allowedTools','Bash','--setting-sources','','--strict-mcp-config','--mcp-config',
 '{"mcpServers":{}}','--no-session-persistence','--add-dir','/work/repository']
run=subprocess.run(args,env=env,cwd='/work/repository',capture_output=True,text=True,timeout=120)
events=[json.loads(line) for line in run.stdout.splitlines() if line.startswith('{')]
result=next((event for event in reversed(events) if event.get('type')=='result'),{})
assert run.returncode==0 and result.get('structured_output')=={'ready':True}, 'native tool loop failed'
print(json.dumps(dict(pinned_claude_tool_loop=True,shell_credentials_absent=True,
                     direct_provider_routes_denied=True,other_job_capability_denied=True)))
'''


def qualify(image, *, docker=None):
    if platform.system() != 'Linux':
        raise BoundaryError('Native provider qualification requires the Linux Docker host')
    def call(args, **kwargs):
        return subprocess.run(['docker','--host','unix:///var/run/docker.sock',*args],capture_output=True,text=True,
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
        target = inputs/'assignment.json'; target.write_text(json.dumps(dict(model=MODEL,capability=CAPABILITY))); target.chmod(0o444)
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
        try:
            docker(['network','create','--driver','bridge',*boundary.labels(labels),boundary.uplink])
            spec = boundary.prepare(job,input_directory=inputs,broker_socket_directory=None)
            docker(['exec',spec.gateway,'/usr/local/bin/python3','-I','-c',
                'import socket,time\nfor attempt in range(50):\n try: socket.create_connection(("forge-provider",8082),1).close(); break\n except OSError:\n  if attempt==49: raise\n  time.sleep(.1)'])
            docker(['create','--pull=never','--name',worker,*boundary.labels(WorkerBoundary.identity(job)[1]),
                '--read-only','--user','1000:1000','--cap-drop','ALL','--security-opt','no-new-privileges',
                *process_isolation(dict(provider='claude',apparmor_profile='forge-runner-private-proc')),
                '--pids-limit','256','--memory','1g','--memory-swap','1g','--cpus','1',
                '--tmpfs','/tmp:rw,nosuid,nodev,size=128m',*spec.worker_args,
                *[item for key,value in spec.environment.items() for item in ('--env',key+'='+value)],
                '--entrypoint','/usr/local/bin/python3',image,'-I','-c',WORKER])
            docker(['start',worker]); code=docker(['wait',worker],timeout=150).stdout.strip()
            if code != '0' or upstream.failed or len(upstream.requests) < 2:
                raise BoundaryError('Native Claude proxy/tool privacy qualification failed')
            result['checks'] = json.loads(docker(['logs',worker]).stdout.strip().splitlines()[-1])
            result['checks']['host_only_key_injection'] = True
            alive.clear()
            docker(['exec',spec.gateway,'/usr/local/bin/python3','-I','-c',
                'import http.client,json; c=http.client.HTTPConnection("forge-provider",8082,timeout=5); '
                'c.request("POST","/v1/messages",json.dumps({"model":'+repr(MODEL)+'}),'
                '{"Authorization":"Bearer "+'+repr(CAPABILITY)+'}); assert c.getresponse().status==403'])
            result['checks']['revoked_execution_denied'] = True
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
