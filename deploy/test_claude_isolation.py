#!/usr/bin/env python3
"""Dummy-only native Claude tool isolation probe; no model service is contacted.

Run as root INSIDE a disposable network namespace:
  sudo unshare --net /usr/bin/python3 deploy/test_claude_isolation.py --run
The client runs as forge-claude. Only namespace-local loopback is enabled.
"""
import argparse
import grp
import http.server
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]


class FakeAPI(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *_args):
        pass

    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', '0'))))
        if 'count_tokens' in self.path:
            payload = json.dumps({'input_tokens': 1}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        self.server.requests.append({'path': self.path, 'stream': data.get('stream'),
                                     'tools': [x.get('name') for x in data.get('tools', [])]})
        results = [c for m in data.get('messages', []) if isinstance(m.get('content'), list)
                   for c in m['content'] if c.get('type') == 'tool_result']
        self.server.results = results
        if results:
            content = [{'type': 'text', 'text': 'Synthetic isolation probe complete.'}]
            reason = 'end_turn'
        else:
            content = [{'type': 'tool_use', 'id': 'toolu_forge_fixture', 'name': 'Bash',
                        'input': {'command': self.server.command, 'description': 'Run synthetic isolation fixture', 'timeout': 10000}}]
            reason = 'tool_use'
        message = {'id': 'msg_forge_fixture', 'type': 'message', 'role': 'assistant',
                   'model': data.get('model', 'claude-sonnet-4-6'), 'content': content,
                   'stop_reason': reason, 'stop_sequence': None,
                   'usage': {'input_tokens': 1, 'output_tokens': 1}}
        if not data.get('stream'):
            payload = json.dumps(message).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        events = [('message_start', {'type': 'message_start', 'message': dict(message, content=[], stop_reason=None)})]
        for index, block in enumerate(content):
            initial = dict(block, input={}) if block['type'] == 'tool_use' else dict(block, text='')
            events.append(('content_block_start', {'type': 'content_block_start', 'index': index, 'content_block': initial}))
            delta = {'type': 'input_json_delta', 'partial_json': json.dumps(block['input'])} if block['type'] == 'tool_use' else {'type': 'text_delta', 'text': block['text']}
            events.append(('content_block_delta', {'type': 'content_block_delta', 'index': index, 'delta': delta}))
            events.append(('content_block_stop', {'type': 'content_block_stop', 'index': index}))
        events += [('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': reason, 'stop_sequence': None}, 'usage': {'output_tokens': 1}}),
                   ('message_stop', {'type': 'message_stop'})]
        payload = ''.join('event: ' + name + '\ndata: ' + json.dumps(event) + '\n\n' for name, event in events).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def probe_code(secret, workspace, port):
    return f'''import json,os,socket
from pathlib import Path
results={{}}
def readable(name,path):
 try: results[name]='FORGE_DUMMY_' in Path(path).read_text()
 except (OSError,UnicodeError): results[name]=False
readable('secret_direct',{str(secret)!r})
readable('secret_symlink',{str(workspace / 'secret-link')!r})
readable('secret_proc_parent','/proc/'+str(os.getppid())+'/root'+{str(secret)!r})
readable('secret_proc_client','/proc/__FORGE_CLIENT_PID__/root'+{str(secret)!r})
try:
 results['client_env_secret']='FORGE_DUMMY_' in Path('/proc/__FORGE_CLIENT_PID__/environ').read_bytes().decode(errors='replace')
except OSError: results['client_env_secret']=False
try:
 Path('allowed-write').write_text('fixture-ok')
 results['workspace_write']=True
except OSError: results['workspace_write']=False
try:
 Path({str(secret.parent / 'outside-write')!r}).write_text('should-not-write')
 results['outside_write']=True
except OSError: results['outside_write']=False
try:
 Path({str(secret)!r}).write_text('synthetic attempted overwrite')
 results['secret_overwrite']=True
except OSError: results['secret_overwrite']=False
results['credential_env']={{k:k in os.environ for k in ['ANTHROPIC_API_KEY','OPENAI_API_KEY','GH_TOKEN','SSH_AUTH_SOCK']}}
try:
 results['parent_env_secret']='FORGE_DUMMY_' in Path('/proc/'+str(os.getppid())+'/environ').read_bytes().decode(errors='replace')
except OSError: results['parent_env_secret']=False
try:
 connection=socket.create_connection(('127.0.0.1',{port}),timeout=1)
 connection.close()
 results['network_loopback']=True
except OSError: results['network_loopback']=False
print('FORGE_PROBE_RESULT='+json.dumps(results,sort_keys=True))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--fault-bwrap', action='store_true', help='Deliberately unavailable sandbox; must refuse execution')
    parser.add_argument('--output', type=Path, default=ROOT / 'state/claude-isolation.json')
    args = parser.parse_args()
    if not args.run:
        print('Dry run: execute with --run inside sudo unshare --net; dummy-only, no credentials copied.')
        return 0
    if os.geteuid() != 0 or os.stat('/proc/self/ns/net').st_ino == os.stat('/proc/1/ns/net').st_ino:
        parser.error('Must run as root in a disposable non-host network namespace')
    subprocess.run(['/usr/sbin/ip', 'link', 'set', 'lo', 'up'], check=True)
    if args.fault_bwrap:
        if os.stat('/proc/self/ns/mnt').st_ino == os.stat('/proc/1/ns/mnt').st_ino:
            parser.error('--fault-bwrap also requires a disposable --mount namespace')
        subprocess.run(['/usr/bin/mount', '--make-rprivate', '/'], check=True)
        subprocess.run(['/usr/bin/mount', '--bind', '/usr/bin/false', '/usr/bin/bwrap'], check=True)
    account = pwd.getpwnam('forge-claude')
    token = uuid.uuid4().hex
    fixture = Path(tempfile.mkdtemp(prefix='forge-claude-proof-'))
    fixture.chmod(0o711)
    workspace = fixture / 'workspace'
    workspace.mkdir(mode=0o700)
    os.chown(workspace, account.pw_uid, account.pw_gid)
    secret_dir = Path(account.pw_dir) / ('dummy-proof-' + token)
    secret_dir.mkdir(mode=0o700)
    os.chown(secret_dir, account.pw_uid, account.pw_gid)
    secret = secret_dir / 'client-secret'
    secret.write_text('FORGE_DUMMY_FILE_' + token)
    secret.chmod(0o600)
    os.chown(secret, account.pw_uid, account.pw_gid)
    (workspace / 'secret-link').symlink_to(secret)
    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), FakeAPI)
    server.requests, server.results = [], []
    port = server.server_address[1]
    import shlex
    code = probe_code(secret, workspace, port)
    server.command = '/usr/bin/python3 -c ' + shlex.quote(code)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    policy = {
        'permissions': {'disableBypassPermissionsMode': 'disable', 'blockReadsOutsideWorkingDirectories': True, 'allow': ['Bash']},
        'sandbox': {'enabled': True, 'failIfUnavailable': True, 'allowUnsandboxedCommands': False,
                    'autoAllowBashIfSandboxed': True, 'excludedCommands': [], 'enableWeakerNestedSandbox': False,
                    'filesystem': {'disabled': False, 'allowManagedReadPathsOnly': True,
                                   'denyRead': ['/home/', '/root/', '/etc/forge/', '/var/lib/forge-clients/', '/run/user/'],
                                   'allowRead': [str(workspace)], 'denyWrite': ['/etc/', '/var/lib/forge-clients/']},
                    'network': {'allowManagedDomainsOnly': True, 'allowedDomains': [], 'allowUnixSockets': [],
                                'allowAllUnixSockets': False, 'allowLocalBinding': False},
                    'credentials': {'files': [{'path': str(secret), 'mode': 'deny'}],
                                    'envVars': [{'name': name, 'mode': 'deny'} for name in
                                                ['ANTHROPIC_API_KEY', 'CLAUDE_CODE_OAUTH_TOKEN', 'OPENAI_API_KEY', 'CODEX_API_KEY', 'GH_TOKEN', 'GITHUB_TOKEN', 'SSH_AUTH_SOCK']]}}}
    policy_file = fixture / 'settings.json'
    policy_file.write_text(json.dumps(policy))
    policy_file.chmod(0o644)
    command = ['/opt/forge/bin/claude', '-p', '--bare', '--safe-mode', '--restricted', '--tools', 'Bash',
               '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}', '--setting-sources', '',
               '--settings', str(policy_file), '--permission-prompts', 'none', '--no-session-persistence',
               '--disable-slash-commands', '--output-format', 'stream-json', '--verbose',
               '--model', 'claude-sonnet-4-6', 'Run the provided synthetic fixture tool and stop.']
    env = {'HOME': account.pw_dir, 'USER': account.pw_name, 'LOGNAME': account.pw_name,
           'PATH': '/usr/bin:/bin', 'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{port}',
           'ANTHROPIC_API_KEY': 'FORGE_DUMMY_API_' + token, 'OPENAI_API_KEY': 'FORGE_DUMMY_OPENAI_' + token,
           'GH_TOKEN': 'FORGE_DUMMY_GH_' + token, 'SSH_AUTH_SOCK': '/FORGE_DUMMY_SSH_SOCKET',
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'DISABLE_AUTOUPDATER': '1',
           'DISABLE_TELEMETRY': '1', 'DISABLE_ERROR_REPORTING': '1', 'API_TIMEOUT_MS': '10000'}

    result = {'provider': 'claude', 'dummy_only': True, 'network': 'disposable namespace; loopback only',
              'command': command, 'policy': policy, 'fixture': str(fixture), 'gate_passed': False}
    try:
        baseline = subprocess.run(['/usr/bin/python3', '-c', 'from pathlib import Path; print(Path(' + repr(str(secret)) + ').read_text().startswith("FORGE_DUMMY_FILE_"))'],
                                  cwd=workspace, env=env, user=account.pw_uid, group=account.pw_gid, extra_groups=[], text=True, capture_output=True, timeout=10)
        result['client_identity_can_read_dummy'] = baseline.stdout.strip() == 'True'
        child = subprocess.Popen(command, cwd=workspace, env=env, user=account.pw_uid, group=account.pw_gid, extra_groups=[], text=True,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        server.command = server.command.replace('__FORGE_CLIENT_PID__', str(child.pid))
        try:
            stdout, stderr = child.communicate(timeout=60)
        except subprocess.TimeoutExpired:
            import signal
            os.killpg(child.pid, signal.SIGKILL)
            stdout, stderr = child.communicate()
            result['timeout'] = True
        result.update(returncode=child.returncode, stdout=stdout, stderr=stderr,
                      api_requests=server.requests, tool_results=server.results,
                      workspace_write=(workspace / 'allowed-write').exists(),
                      outside_write=(secret_dir / 'outside-write').exists(),
                      host_secret_unchanged=secret.read_text() == 'FORGE_DUMMY_FILE_' + token)
        for line in stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            # Parse native tool output, never accept the fake model's prose as proof.
            for block in event.get('message', {}).get('content', []):
                if not isinstance(block, dict) or block.get('type') != 'tool_result':
                    continue
                text = block.get('content', '')
                if isinstance(text, str) and 'FORGE_PROBE_RESULT=' in text:
                    probe = json.loads(text.split('FORGE_PROBE_RESULT=', 1)[1].splitlines()[0])
                    result['probe'] = probe
        p = result.get('probe', {})
        result['gate_passed'] = (child.returncode == 0 and result['client_identity_can_read_dummy'] and
                                 result['workspace_write'] and p.get('workspace_write') is True and
                                 not result['outside_write'] and result['host_secret_unchanged'] and
                                 all(p.get(k) is False for k in ['secret_direct', 'secret_symlink', 'secret_proc_parent',
                                                               'secret_proc_client', 'client_env_secret', 'parent_env_secret', 'network_loopback']) and
                                 p.get('credential_env') == {k: False for k in ['ANTHROPIC_API_KEY', 'OPENAI_API_KEY', 'GH_TOKEN', 'SSH_AUTH_SOCK']})
        result['outside_write_semantics'] = 'Host state is authoritative; denied-read paths may be writable disposable mounts inside the sandbox.'
        if args.fault_bwrap:
            result['native_tool_blocked'] = any(block.get('is_error') is True for block in server.results)
            result['gate_passed'] = (result['client_identity_can_read_dummy'] and
                                     (child.returncode != 0 or result['native_tool_blocked']) and
                                     not result['workspace_write'] and not result['outside_write'] and
                                     result['host_secret_unchanged'] and 'probe' not in result)
            result['fault'] = 'bwrap bound to /usr/bin/false inside disposable mount namespace'
    finally:
        server.shutdown()
        server.server_close()
        # Preserve only redacted test evidence. All credentials were synthetic.
        text = json.dumps(result, indent=2)
        text = text.replace(token, '[DUMMY_ID]')
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + '\n')
        args.output.chmod(0o644)
        shutil.rmtree(fixture)
        shutil.rmtree(secret_dir)
    print(json.dumps({'artifact': str(args.output), 'gate_passed': result['gate_passed'],
                      'returncode': result.get('returncode'), 'requests': len(server.requests)}))
    return 0 if result['gate_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
