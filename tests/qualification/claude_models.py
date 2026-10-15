import json,os,subprocess,threading
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
seen=[]
class Handler(BaseHTTPRequestHandler):
 def log_message(self,*_):pass
 def do_POST(self):
  value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
  seen.append(value.get('model'))
  self.send_response(400);self.send_header('Content-Type','application/json');self.end_headers()
  self.wfile.write(b'{"type":"error","error":{"type":"invalid_request_error","message":"synthetic model trace complete"}}')
s=ThreadingHTTPServer(('127.0.0.1',0),Handler);threading.Thread(target=s.serve_forever,daemon=True).start()
version=subprocess.check_output(['claude','--version'],text=True).strip();assert version.startswith('2.1.273 ')
print(version,flush=True)
Path('/work/home').mkdir();Path('/work/repository').mkdir()
env={'PATH':'/opt/forge/claude-tools:'+os.environ['PATH'],'HOME':'/work/home','ANTHROPIC_BASE_URL':f'http://127.0.0.1:{s.server_port}','ANTHROPIC_API_KEY':'synthetic-key','DISABLE_AUTOUPDATER':'1','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}
for model in (None,'sonnet','opus','haiku'):
 seen.clear()
 cmd=['claude','-p','Return ok','--tools','','--setting-sources','','--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--no-session-persistence']
 if model:cmd+=['--model',model]
 p=subprocess.run(cmd,cwd='/work/repository',env=env,capture_output=True,timeout=40)
 assert seen,p.stderr.decode()[-1000:]
 expected={None:'claude-opus-5','sonnet':'claude-sonnet-5','opus':'claude-opus-5','haiku':'claude-haiku-4-5-20251001'}
 assert set(seen)=={expected[model]}, 'pinned alias mapping changed'
 print(json.dumps({'selected':model,'wire_models':sorted(set(seen)),'returncode':p.returncode}),flush=True)
s.shutdown()
