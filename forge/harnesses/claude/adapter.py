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
EXECUTION = runpy.run_path(str(Path(__file__).resolve().parent.parent / 'execution.py'))


def error_message(text):
    text = text.lower()
    if any(s in text for s in ('rate_limit', 'rate limit', 'usage limit', 'credit balance', 'out of credits', 'hit your limit', '429')):
        return 'Provider usage limit reached'
    if any(s in text for s in ('authentication_error', 'invalid_api_key', 'not logged in', 'token expired', 'unauthorized', 'login required', '401')):
        return 'Provider authentication_error; reconnect the account'
    return 'Claude Code did not return a usable result'


# Claude Code terminal result messages share the documented Agent SDK shape.
# Keep provider diagnostics separate from the authoritative Forge result file.
RESULT_SUBTYPES = frozenset(('success', 'error_during_execution', 'error_max_turns',
                             'error_max_budget_usd', 'error_max_structured_output_retries'))
SCHEMA_ERROR = 'Error: --json-schema is not a valid JSON Schema'
ERROR_BYTES = 1024
ERROR_COUNT = 3
API_ERRORS = frozenset(('authentication_failed', 'oauth_org_not_allowed', 'account_on_hold',
    'verification_required', 'billing_error', 'rate_limit', 'overloaded', 'invalid_request',
    'model_not_found', 'server_error', 'unknown', 'max_output_tokens', 'cloud_credential_error'))


def api_error_fields(event, status_key):
    category = event.get('error')
    status = event.get(status_key)
    return dict(error=category if isinstance(category, str) and category in API_ERRORS else None,
                **{status_key: status if type(status) is int and 100 <= status <= 599 else None})


def failure_diagnostic(code, response, raw, events=()):
    """Select complete provider errors; the host scrubs delivered secrets.

    Never truncate strings before that scrub: a cut opaque credential would no
    longer match its delivered value. No model result, tool output or transcript
    text belongs in this diagnostic.
    """
    startup = next((line for line in raw.splitlines() if line.startswith(SCHEMA_ERROR)), None)
    subtype = response.get('subtype')
    known_subtype = subtype if isinstance(subtype, str) and subtype in RESULT_SUBTYPES else None
    structured = isinstance(response.get('structured_output'), dict)
    if startup is not None:
        reason = 'startup_schema_invalid'
    elif known_subtype == 'error_max_structured_output_retries':
        reason = 'structured_output_retries_exhausted'
    elif not response:
        reason = 'terminal_result_missing'
    elif response.get('is_error') or known_subtype and known_subtype.startswith('error_'):
        reason = 'terminal_error'
    elif not structured:
        reason = 'structured_output_missing'
    else:
        reason = 'cli_exit_nonzero'
    errors = response.get('errors', [])
    errors = errors if isinstance(errors, list) else []
    if startup is not None:
        errors = [startup, *errors]
    selected = [item for item in errors if isinstance(item, str)
                and len(json.dumps(item).encode()) <= ERROR_BYTES][:ERROR_COUNT]
    diagnostic = dict(provider='claude', reason=reason, exit_code=code, terminal_result=bool(response),
                subtype=known_subtype, subtype_recognized=subtype in RESULT_SUBTYPES if isinstance(subtype, str) else False,
                is_error=response.get('is_error') if type(response.get('is_error')) is bool else None,
                structured_output=structured, error_count=len(errors), errors=selected,
                omitted_errors=len(errors)-len(selected))
    if 'api_error_status' in response:
        diagnostic['api_error_status'] = api_error_fields(response, 'api_error_status')['api_error_status']
    retry = next((event for event in reversed(events)
                  if event.get('type') == 'system' and event.get('subtype') == 'api_retry'), None)
    if retry is not None:
        diagnostic['last_api_retry'] = api_error_fields(retry, 'error_status')
        for key in ('attempt', 'max_retries', 'retry_delay_ms'):
            value = retry.get(key)
            if type(value) is int and 0 <= value <= 2**53-1:
                diagnostic['last_api_retry'][key] = value
    api_error = next((event for event in reversed(events) if event.get('type') == 'assistant'
                      and isinstance(event.get('error'), str) and event['error'] in API_ERRORS), None)
    if api_error is not None:
        diagnostic['last_api_error'] = api_error_fields(api_error, 'api_error_status')
    # These prior API observations are not evidence of the terminal cause.
    return diagnostic


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
        provider = EXECUTION['provider_error'](response)
        if provider is not None and canary is None:
            EXECUTION['write_receipt'](note, 'provider_error', provider_error=provider)
        message = error_message(raw)
        print(json.dumps({'type': 'error', 'message': message,
                          'provider_failure': failure_diagnostic(code, response, raw, events)}), flush=True)
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
    Path('/work/home/forge-exit.json').write_text('synthetic private envelope')
    nonce = uuid.uuid4().hex
    schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean', 'enum': [True]}},
              'required': ['ready'], 'additionalProperties': False}
    command = '/usr/local/bin/python3 /opt/forge/harnesses/claude/canary.py ' + nonce
    prompt = ('This is a sandbox and authentication check. Use Bash to run exactly ' + command +
              '. Do not create or edit any files yourself. If it succeeds, return ready true.')
    build_env = runpy.run_path('/opt/forge/build_environment.py')['environment'](directory)
    try:
        answer, failure = invoke(note, directory, {**os.environ, **build_env, 'FORGE_PROBE_SECRET': 'synthetic-private-env'}, schema, prompt, canary=(command, nonce))
    except RuntimeError as error:
        answer, failure = None, 'canary' if 'canary' in str(error) else str(error)
    proof = directory / 'claude-probe-proof'
    reason = probe_failure_reason(answer, failure, proof.read_text() if proof.is_file() else None, nonce)
    if reason:
        # Fixed labels only: the setup service turns them into operator guidance.
        Path('/work/result.json').write_text(json.dumps({'ready': False, 'attempt': note['id'], 'reason': reason}))
        raise RuntimeError(reason)
    Path('/work/result.json').write_text(json.dumps({'ready': True, 'attempt': note['id']}))
