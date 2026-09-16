"""Image-owned Claude Code adapter. Managed sandbox policy is mandatory."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
import selectors


def error_message(text):
    text = text.lower()
    if any(s in text for s in ('rate_limit', 'rate limit', 'usage limit', 'credit balance', 'out of credits', 'hit your limit', '429')):
        return 'Provider usage limit reached'
    if any(s in text for s in ('authentication_error', 'invalid_api_key', 'not logged in', 'token expired', 'unauthorized', 'login required', '401')):
        return 'Provider authentication_error; reconnect the account'
    return 'Claude Code did not return a usable result'


def collect(args, prompt, directory, env):
    """Drain output with a hard bound; the outer runner owns the deadline."""
    with tempfile.TemporaryFile(mode='w+', dir='/work/home') as source:
        source.write(prompt); source.seek(0)
        child = subprocess.Popen(args, stdin=source, cwd=directory, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
        chunks, size = [], 0
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    selector.select()
                    block = os.read(child.stdout.fileno(), 65536)
                    if not block:
                        break
                    size += len(block)
                    if size > 2 * 1024 * 1024:
                        raise RuntimeError('Claude output exceeded its size limit')
                    chunks.append(block)
            return child.wait(), b''.join(chunks).decode(errors='replace')
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
            child.stdout.close()


def canary_completed(events, command, nonce):
    uses = {part['id'] for e in events if e.get('type') == 'assistant'
            for part in e.get('message', {}).get('content', [])
            if part.get('type') == 'tool_use' and part.get('name') == 'Bash'
            and part.get('input', {}).get('command') == command}
    return any(part.get('type') == 'tool_result' and part.get('tool_use_id') in uses
               and not part.get('is_error') and 'forge-canary:' + nonce in str(part.get('content', ''))
               for e in events if e.get('type') == 'user'
               for part in e.get('message', {}).get('content', []))


def invoke(note, directory, env, result_schema, prompt, *, canary=None):
    # Parent credentials never reach Bash/hook/MCP subprocesses. This switch
    # also requires a separate PID namespace for Bash on Linux.
    env = {**env, 'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB': '1', 'DISABLE_AUTOUPDATER': '1',
           'PATH': '/opt/forge/claude-tools:/usr/local/bin:/usr/bin:/bin',
           'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1', 'CLAUDE_CODE_PROXY_RESOLVES_HOSTS': '1'}
    args = ['claude', '-p', '--output-format', 'stream-json', '--verbose', '--json-schema', json.dumps(result_schema),
            '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--tools', 'Bash,Read,Edit,Write,Glob,Grep', '--permission-mode', 'acceptEdits',
            '--no-session-persistence', '--allowedTools', 'Bash,Read,Edit,Write,Glob,Grep',
            '--add-dir', '/opt/forge/harnesses/claude']
    if note.get('model'):
        args += ['--model', note['model']]
    home = Path('/work/home'); home.mkdir(exist_ok=True)
    code, raw = collect(args, prompt, directory, env)
    events = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
            if isinstance(event, dict):
                events.append(event)
        except ValueError:
            pass
    response = next((e for e in reversed(events) if e.get('type') == 'result'), {})
    if code or response.get('is_error') or not isinstance(response.get('structured_output'), dict):
        print(json.dumps({'type': 'error', 'message': error_message(raw)}), flush=True)
        return None
    if canary and not canary_completed(events, *canary):
        raise RuntimeError('Native sandbox canary execution was not verified')
    return response['structured_output']


def run(note, directory, env, result_schema):
    prompt = json.dumps(note) + '\nReturn your result as the final structured response. The supervisor publishes it; do not write /work/result.json.'
    result = invoke(note, directory, env, result_schema, prompt)
    if result is None:
        return 1
    encoded = json.dumps(result)
    if len(encoded.encode()) > 65536:
        raise RuntimeError('Result exceeded its size limit')
    temporary = Path('/work/final-result.json'); temporary.write_text(encoded); temporary.replace('/work/result.json')
    print(json.dumps({'type': 'claude.result', 'summary': str(result.get('summary', ''))[:4000]}), flush=True)
    return 0


def probe(note, directory):
    private = Path('/work/home/probe-private'); private.parent.mkdir(exist_ok=True); private.write_text('synthetic private canary')
    nonce = uuid.uuid4().hex
    schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean', 'enum': [True]}},
              'required': ['ready'], 'additionalProperties': False}
    command = '/usr/local/bin/python3 /opt/forge/harnesses/claude/canary.py ' + nonce
    prompt = ('This is a sandbox and authentication check. Use Bash to run exactly ' + command +
              '. Do not create or edit any files yourself. If it succeeds, return ready true.')
    answer = invoke(note, directory, {**os.environ, 'FORGE_PROBE_SECRET': 'synthetic-private-env'}, schema, prompt, canary=(command, nonce))
    proof = directory / 'claude-probe-proof'
    if answer != {'ready': True} or not proof.is_file() or proof.read_text() != nonce:
        raise RuntimeError('Claude authentication or native sandbox check failed')
    Path('/work/result.json').write_text(json.dumps({'ready': True, 'attempt': note['id']}))
