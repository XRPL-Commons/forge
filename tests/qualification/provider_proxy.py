"""Credential-free Claude wire qualification inside the pinned worker image."""
from contextlib import closing
import http.client, json, os, socket, subprocess, threading, time
from provider_proxy import Assignment, Broker, SessionServer
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

requests=[]
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_HEAD(self): self.send_response(200); self.end_headers()
    def do_GET(self):
        requests.append({'method':'GET','path':self.path})
        self.send_response(200); self.send_header('Content-Type','application/json'); self.end_headers()
        self.wfile.write(json.dumps({'object':'list','data':[{'id':'grok-test','object':'model','created':1,'owned_by':'xai'}]}).encode())
    def do_POST(self):
        raw=self.rfile.read(int(self.headers.get('Content-Length','0'))); body=json.loads(raw)
        tools=body.get('tools',[])
        names=[t.get('name',t.get('function',{}).get('name')) for t in tools]
        requests.append({'method':'POST','path':self.path,'model':body.get('model'),'tools':names,'body':body,'headers':dict(self.headers)})
        print('REQUEST',self.path,body.get('model'),names,flush=True)
        if '/messages' in self.path: self.claude(body,names)
        else: self.send_response(404); self.end_headers()
    def event(self,name,data): self.wfile.write(('event: '+name+'\ndata: '+json.dumps(data)+'\n\n').encode());self.wfile.flush()
    def claude(self,body,names):
        self.send_response(200);self.send_header('Content-Type','text/event-stream');self.end_headers()
        seen=any(isinstance(m.get('content'),list) and any(p.get('type')=='tool_result' for p in m['content']) for m in body.get('messages',[]))
        self.event('message_start',{'type':'message_start','message':{'id':'msg_fake','type':'message','role':'assistant','model':body['model'],'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':1,'output_tokens':0}}})
        if not seen:
            content={'type':'tool_use','id':'read_1','name':'Read','input':{}};arguments={'file_path':'/work/repository/proof.txt'}
        elif 'StructuredOutput' in names:
            content={'type':'tool_use','id':'final_1','name':'StructuredOutput','input':{}};arguments={'ready':True}
        else: content={'type':'text','text':''};arguments=None
        self.event('content_block_start',{'type':'content_block_start','index':0,'content_block':content})
        delta={'type':'input_json_delta','partial_json':json.dumps(arguments)} if arguments is not None else {'type':'text_delta','text':'{"ready":true}'}
        self.event('content_block_delta',{'type':'content_block_delta','index':0,'delta':delta})
        self.event('content_block_stop',{'type':'content_block_stop','index':0})
        self.event('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use' if arguments is not None else 'end_turn','stop_sequence':None},'usage':{'output_tokens':8}})
        self.event('message_stop',{'type':'message_stop'})
Path('/work/repository').mkdir(parents=True,exist_ok=True);Path('/work/repository/proof.txt').write_text('forge-proxy-proof')
server=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
base='http://127.0.0.1:'+str(server.server_port)
provider='claude'
version=subprocess.check_output([provider,'--version'],text=True).strip()
assert version.startswith('2.1.273 '), version
print('PINNED_VERSION',version,flush=True)
if provider == 'claude':
    socket_path='/work/provider.sock'
    broker=Broker(Assignment('qualification-job','synthetic-account','claude-sonnet-4-6',time.time()+60,'forge-test-capability'),api_key=lambda:'host-only-fixture-key',authorized=lambda assignment: True,connection=lambda: http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10))
    broker_server=SessionServer(socket_path,broker)
    threading.Thread(target=broker_server.serve_forever,daemon=True).start()
    class UnixConnection(http.client.HTTPConnection):
        def connect(self):
            self.sock=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);self.sock.settimeout(15);self.sock.connect(socket_path)
    class Frontend(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=self.rfile.read(int(self.headers['Content-Length']))
            with closing(UnixConnection('localhost')) as conn:
                conn.request('POST',self.path,body,dict(self.headers))
                response=conn.getresponse();self.send_response(response.status)
                for k,v in response.getheaders(): self.send_header(k,v)
                self.end_headers()
                while chunk:=response.read1(65536): self.wfile.write(chunk);self.wfile.flush()
    frontend=ThreadingHTTPServer(('127.0.0.1',0),Frontend)
    threading.Thread(target=frontend.serve_forever,daemon=True).start()
    base='http://127.0.0.1:'+str(frontend.server_port)
home=Path('/work/proxy-home');home.mkdir(exist_ok=True)
env={'PATH':'/opt/forge/claude-tools:'+os.environ['PATH'],'HOME':str(home),'DISABLE_AUTOUPDATER':'1','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','GROK_DISABLE_AUTOUPDATER':'1','GROK_TELEMETRY_ENABLED':'0','GROK_FEEDBACK_ENABLED':'0','GROK_WORKFLOWS':'0'}
schema=json.dumps({'type':'object','properties':{'ready':{'type':'boolean'}},'required':['ready'],'additionalProperties':False})
if provider=='claude':
    env.update(ANTHROPIC_BASE_URL=base,ANTHROPIC_AUTH_TOKEN='forge-test-capability')
    cmd=['claude','-p','Read /work/repository/proof.txt, then return ready true.','--output-format','stream-json','--verbose','--json-schema',schema,'--model','claude-sonnet-4-6','--tools','Read','--allowedTools','Read','--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--no-session-persistence','--add-dir','/work/repository']

try:
    run=subprocess.run(cmd,env=env,cwd='/work/repository',capture_output=True,text=True,timeout=50)
    events=[json.loads(line) for line in run.stdout.splitlines() if line.startswith('{')]
    result=next((event for event in reversed(events) if event.get('type')=='result'),{})
    print('RESULT',json.dumps({'exit_code':run.returncode,'subtype':result.get('subtype'),'is_error':result.get('is_error'),'structured_output':result.get('structured_output')}),flush=True)
    if run.returncode: print(run.stderr[-4000:],flush=True)
    assert run.returncode==0, 'native CLI failed'
    assert result.get('structured_output')=={'ready':True}, 'missing structured result'
except subprocess.TimeoutExpired: raise SystemExit('Native CLI timed out')
print('REQUEST_SUMMARY',json.dumps([{k:v for k,v in r.items() if k not in ('body','headers')} for r in requests]),flush=True)
assert any('forge-proxy-proof' in json.dumps(r.get('body')) for r in requests), 'tool result missing upstream'
if provider=='claude':
    assert all(r['headers'].get('x-api-key')=='host-only-fixture-key' for r in requests if r['method']=='POST')
    assert all(not any(k.lower()=='authorization' for k in r['headers']) for r in requests if r['method']=='POST')
print('QUALIFIED: native tool loop, schema result, host-only credential injection',flush=True)
server.shutdown()
