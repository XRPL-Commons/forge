"""Image-owned Claude Code adapter. Managed sandbox policy is mandatory."""
import json
from collections import deque
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
import selectors
import runpy

RESULTS = runpy.run_path(str(Path(__file__).resolve().parent.parent / 'results.py'))


def error_message(text):
    text = text.lower()
    if any(s in text for s in ('rate_limit', 'rate limit', 'usage limit', 'credit balance', 'out of credits', 'hit your limit', '429')):
        return 'Provider usage limit reached'
    if any(s in text for s in ('authentication_error', 'invalid_api_key', 'not logged in', 'token expired', 'unauthorized', 'login required', '401')):
        return 'Provider authentication_error; reconnect the account'
    return 'Claude Code did not return a usable result'


FAILURE_REASONS = {'Provider authentication_error; reconnect the account': 'provider_login_expired',
                   'Provider usage limit reached': 'provider_usage_limit'}


def probe_failure_reason(answer, failure, proof, nonce):
    """Map a probe outcome to a fixed reason label, or None when the probe passed."""
    if answer is None:
        if failure == 'canary':
            return 'native_sandbox_failed'
        return FAILURE_REASONS.get(failure, 'provider_connection_failed')
    if answer != {'ready': True} or proof != nonce:
        return 'native_sandbox_failed'
    return None



def announcer():
    """Tool calls as small live inspection events; never fails a run."""
    try:
        return runpy.run_path(str(Path(__file__).resolve().parent.parent / 'activity.py'))['Announcer']()
    except Exception:
        return None


def collect(args, prompt, directory, env, *, max_bytes=2*1024*1024):
    """Drain an unlimited transcript into a bounded tail; keep its final result."""
    with tempfile.TemporaryFile(mode='w+', dir='/work/home') as source:
        source.write(prompt); source.seek(0)
        child = subprocess.Popen(args, stdin=source, cwd=directory, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
        chunks, size = deque(), 0
        announce = announcer()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    selector.select()
                    block = os.read(child.stdout.fileno(), 65536)
                    if not block:
                        break
                    size += len(block)
                    chunks.append(block)
                    if announce:
                        announce.feed(block)
                    # Tool-heavy work can emit much more than 2 MiB over its
                    # lifetime. Bound retained memory, not completed work. The
                    # CLI's terminal result is at the end; incomplete leading
                    # JSON lines are ignored by invoke(). The runner still
                    # enforces the execution deadline and role-specific result limit.
                    while size > max_bytes:
                        excess = size - max_bytes
                        first = chunks.popleft()
                        if len(first) > excess:
                            chunks.appendleft(first[excess:])
                            size -= excess
                        else:
                            size -= len(first)
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


def harness_environment(env):
    # Parent credentials never reach Bash/hook/MCP subprocesses. This switch
    # also requires a separate PID namespace for Bash on Linux.
    # CLAUDE_CODE_PROXY_RESOLVES_HOSTS is deliberately absent: the OAuth token
    # refresh client does not honour it and fails with "Invalid IP address:
    # undefined", so every expired access token became a login failure. The
    # API client reaches the egress proxy by CONNECT host name either way.
    return {**{k: v for k, v in env.items() if k != 'CLAUDE_CODE_PROXY_RESOLVES_HOSTS'},
            'CLAUDE_CODE_SUBPROCESS_ENV_SCRUB': '1', 'DISABLE_AUTOUPDATER': '1',
            'PATH': '/opt/forge/claude-tools:/usr/local/bin:/usr/bin:/bin',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1'}


def invoke(note, directory, env, result_schema, prompt, *, canary=None):
    env = harness_environment(env)
    args = ['claude', '-p', '--output-format', 'stream-json', '--verbose', '--json-schema', json.dumps(result_schema),
            '--setting-sources', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--tools', 'Bash,Read,Edit,Write,Glob,Grep', '--permission-mode', 'acceptEdits',
            '--no-session-persistence', '--allowedTools', 'Bash,Read,Edit,Write,Glob,Grep',
            '--add-dir', str(directory), '--add-dir', '/opt/forge/harnesses/claude']
    if note.get('model'):
        args += ['--model', note['model']]
    home = Path('/work/home'); home.mkdir(exist_ok=True)
    launch = Path('/opt/forge/claude-control')
    # Scrub mode protects the launch directory's manifests, lockfiles, scripts
    # and node_modules/.bin. Keep that directory image-owned: repository code
    # executes only in the sandbox, with the checkout as an added working dir.
    prompt = ('The assigned repository is ' + str(directory) + '. Run repository commands with '
              'an explicit cd to that directory. The launch directory is trusted runner code.\n' + prompt)
    code, raw = collect(args, prompt, launch, env,
                        max_bytes=max(2*1024*1024, 2*RESULTS['result_limit'](note.get('role'))+65536))
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
        message = error_message(raw)
        print(json.dumps({'type': 'error', 'message': message}), flush=True)
        return None, message
    if canary and not canary_completed(events, *canary):
        raise RuntimeError('Native sandbox canary execution was not verified')
    return response['structured_output'], None


def run(note, directory, env, result_schema):
    build = runpy.run_path('/opt/forge/build_environment.py')
    try:
        subprocess.run(['python3', '/opt/forge/build_environment.py', '--check-environment'],
                       cwd=directory, env=env, capture_output=True, check=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        build['blocked'](note, 'Forge toolchain readiness failed before work started. Repair the runner image; the candidate PR was not changed.')
        return 125
    prompt = json.dumps(note) + '\nReturn your result as the final structured response. The supervisor publishes it; do not write /work/result.json.'
    result, _ = invoke(note, directory, env, result_schema, prompt)
    if result is None:
        return 1
    RESULTS['write_result'](note, result)
    print(json.dumps({'type': 'claude.result', 'summary': str(result.get('summary', ''))[:4000]}), flush=True)
    return 0


def probe(note, directory):
    private = Path('/work/home/probe-private'); private.parent.mkdir(exist_ok=True); private.write_text('synthetic private canary')
    subprocess.run(['git', 'init', str(directory)], check=True, capture_output=True)
    nonce = uuid.uuid4().hex
    schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean', 'enum': [True]}},
              'required': ['ready'], 'additionalProperties': False}
    command = '/usr/local/bin/python3 /opt/forge/harnesses/claude/canary.py ' + nonce
    prompt = ('This is a sandbox and authentication check. Use Bash to run exactly ' + command +
              '. Do not create or edit any files yourself. If it succeeds, return ready true.')
    build_env = runpy.run_path('/opt/forge/build_environment.py')['environment'](directory)
    try:
        answer, failure = invoke(note, directory, {**os.environ, **build_env, 'FORGE_PROBE_SECRET': 'synthetic-private-env',
                                                  'GH_TOKEN': 'forge-synthetic-repository-token'}, schema, prompt, canary=(command, nonce))
    except RuntimeError as error:
        answer, failure = None, 'canary' if 'canary' in str(error) else str(error)
    proof = directory / 'claude-probe-proof'
    reason = probe_failure_reason(answer, failure, proof.read_text() if proof.is_file() else None, nonce)
    if reason:
        # Fixed labels only: the setup service turns them into operator guidance.
        Path('/work/result.json').write_text(json.dumps({'ready': False, 'attempt': note['id'], 'reason': reason}))
        raise RuntimeError(reason)
    Path('/work/result.json').write_text(json.dumps({'ready': True, 'attempt': note['id']}))
