"""Image-owned Grok Build adapter. Managed sandbox policy is mandatory.

Grok Build 1.0.34 speaks the same streaming event wire format as Claude Code
(`system/init`, `assistant`, `user`, terminal `result` with `is_error` and
`structured_output`), so the parsing and the canary proof follow the Claude
adapter. Everything unverified fails closed: no structured result, no canary
tool result, or a readable private file makes the attempt or probe fail.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid
import selectors
import runpy


def error_message(text):
    text = text.lower()
    if any(s in text for s in ('rate_limit', 'rate limit', 'usage limit', 'insufficient_quota', 'quota', 'credit', '429')):
        return 'Provider usage limit reached'
    if any(s in text for s in ('not signed in', 'sign in', 'authentication_error', 'invalid api key', 'invalid_api_key',
                               'unauthorized', 'login required', 'token expired', '401')):
        return 'Provider authentication_error; reconnect the account'
    return 'Grok Build did not return a usable result'


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


def collect(args, directory, env):
    """Drain output with a hard bound; the outer runner owns the deadline."""
    child = subprocess.Popen(args, stdin=subprocess.DEVNULL, cwd=directory, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True)
    chunks, size = [], 0
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
                if size > 2 * 1024 * 1024:
                    raise RuntimeError('Grok output exceeded its size limit')
                chunks.append(block)
                if announce:
                    announce.feed(block)
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
    """Only the harness process sees provider credentials; repository tools do not."""
    home = Path('/work/home')
    return {**env, 'HOME': str(home), 'GROK_HOME': str(home / '.grok'),
            'PATH': '/usr/local/bin:/usr/bin:/bin',
            'GROK_DISABLE_AUTOUPDATER': '1', 'GROK_TELEMETRY_ENABLED': '0', 'GROK_FEEDBACK_ENABLED': '0',
            'GROK_WORKFLOWS': '0'}


def invoke(note, directory, env, result_schema, prompt, *, canary=None):
    env = harness_environment(env)
    Path(env['HOME']).mkdir(mode=0o700, exist_ok=True)
    with tempfile.NamedTemporaryFile('w', dir='/work/home', prefix='prompt-', suffix='.txt', delete=False) as source:
        source.write(prompt)
        os.chmod(source.name, 0o600)
    args = ['grok', '--prompt-file', source.name, '--cwd', str(directory),
            '--permission-mode', 'acceptEdits', '--no-subagents', '--disable-web-search', '--no-auto-update',
            '--output-format', 'streaming-messages-json', '--json-schema', json.dumps(result_schema)]
    if note.get('model'):
        args += ['-m', note['model']]
    try:
        code, raw = collect(args, directory, env)
    finally:
        Path(source.name).unlink(missing_ok=True)
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
    encoded = json.dumps(result)
    if len(encoded.encode()) > 65536:
        raise RuntimeError('Result exceeded its size limit')
    temporary = Path('/work/final-result.json'); temporary.write_text(encoded); temporary.replace('/work/result.json')
    print(json.dumps({'type': 'grok.result', 'summary': str(result.get('summary', ''))[:4000]}), flush=True)
    return 0


def probe(note, directory):
    private = Path('/work/home/probe-private'); private.parent.mkdir(exist_ok=True); private.write_text('synthetic private canary')
    nonce = uuid.uuid4().hex
    schema = {'type': 'object', 'properties': {'ready': {'type': 'boolean', 'enum': [True]}},
              'required': ['ready'], 'additionalProperties': False}
    command = '/usr/local/bin/python3 /opt/forge/harnesses/grok/canary.py ' + nonce
    prompt = ('This is a sandbox and authentication check. Use Bash to run exactly ' + command +
              '. Do not create or edit any files yourself. If it succeeds, return ready true.')
    build_env = runpy.run_path('/opt/forge/build_environment.py')['environment'](directory)
    try:
        answer, failure = invoke(note, directory, {**os.environ, **build_env, 'FORGE_PROBE_SECRET': 'synthetic-private-env',
                                                  'GH_TOKEN': 'forge-synthetic-repository-token'}, schema, prompt, canary=(command, nonce))
    except RuntimeError as error:
        answer, failure = None, 'canary' if 'canary' in str(error) else str(error)
    proof = directory / 'grok-probe-proof'
    reason = probe_failure_reason(answer, failure, proof.read_text() if proof.is_file() else None, nonce)
    if reason:
        # Fixed labels only: the setup service turns them into operator guidance.
        Path('/work/result.json').write_text(json.dumps({'ready': False, 'attempt': note['id'], 'reason': reason}))
        raise RuntimeError(reason)
    Path('/work/result.json').write_text(json.dumps({'ready': True, 'attempt': note['id']}))
