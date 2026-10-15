"""Private image-owned execution receipts; never parse worker prose as authority.

The native sandbox denies /work/home and parent process descriptors. Adapters
write here only from their own terminal protocol, not from model result objects.
"""
import hashlib
import json
import math
from pathlib import Path
import traceback

RECEIPT = Path('/work/home/forge-harness-exit.json')
ENVELOPE = Path('/work/home/forge-exit.json')
CODES = frozenset(('quota', 'auth', 'rate_limit', 'overloaded', 'unknown'))

# Diagnostic observations do not select execution/retry policy. Only fixed
# protocol vocabulary leaves the private provider event stream.
DIAGNOSTIC_EVENTS = frozenset(('thread.started', 'turn.started', 'error',
                             'command_execution', 'agent_message', 'reasoning', 'file_change', 'mcp_tool_call'))
DIAGNOSTIC_FIELDS = frozenset(('message', 'code', 'type', 'status', 'param'))
JSON_TYPES = frozenset(('null', 'boolean', 'number', 'string', 'array', 'object'))
DIAGNOSTIC_CODES = frozenset(('invalid_json_schema', 'invalid_request_error', 'context_length_exceeded',
                            'invalid_api_key', 'rate_limit_exceeded', 'insufficient_quota', 'model_not_found',
                            'server_error', 'authentication_error'))



# Native rollout fields and shipped model catalogs from Codex 0.153.4/0.163.0. Unknown
# strings are never retained: model aliases may contain private configuration.
CODEX_MODELS = frozenset(('gpt-6-astra', 'gpt-6.1-sol', 'gpt-6-sol', 'gpt-6-luna', 'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna',
    'gpt-daybreak-blue-latest', 'gpt-daybreak-red-latest', 'gpt-5.5', 'gpt-5.4',
    'gpt-5.4-mini', 'gpt-5.2', 'codex-auto-review', 'unrecognized'))
CODEX_EFFORTS = frozenset(('none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra', 'unrecognized'))
TOKEN_FIELDS = frozenset(('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens',
                         'output_tokens', 'reasoning_output_tokens', 'total_tokens'))


def token_number(value):
    return type(value) is int and 0 <= value <= 10**12


def valid_codex_session(value):
    if (not isinstance(value, dict) or value.get('source') != 'native_session'
            or value.get('usage_scope') != 'last_recorded' or type(value.get('truncated')) is not bool
            or set(value) - {'source', 'usage_scope', 'truncated', 'model', 'reasoning_effort',
                            'last_token_usage', 'total_token_usage', 'model_context_window'}):
        return False
    if 'model' in value and (not isinstance(value['model'], str) or value['model'] not in CODEX_MODELS):
        return False
    if 'reasoning_effort' in value and (not isinstance(value['reasoning_effort'], str)
                                       or value['reasoning_effort'] not in CODEX_EFFORTS):
        return False
    if 'model_context_window' in value and not token_number(value['model_context_window']):
        return False
    for key in ('last_token_usage', 'total_token_usage'):
        if key in value and (not isinstance(value[key], dict) or set(value[key]) - TOKEN_FIELDS
                             or any(not token_number(v) for v in value[key].values())):
            return False
    return True


def codex_session_diagnostic(events, directory):
    """Project one native session; never retain prompts, responses or auth.

    Codex 0.153.4 drops usage from response.incomplete. Any usage here is the
    last *recorded* token_count, potentially preceding the failed response.
    Missing usage is left missing. Failure diagnostics never govern admission.
    """
    import os
    import re
    import stat
    threads = {e.get('thread_id') for e in events if isinstance(e, dict)
               and e.get('type') == 'thread.started' and isinstance(e.get('thread_id'), str)
               and re.fullmatch(r'[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}', e['thread_id'])}
    if len(threads) != 1:
        return None
    thread = threads.pop()
    try:
        root = Path(directory)
        if root.is_symlink(): return None
        # A disposable worker has one session. Bound scanning anyway, including
        # directories; never follow links or inspect another thread's contents.
        pending, candidates, count = [(root, 0)], [], 0
        while pending:
            parent, depth = pending.pop()
            with os.scandir(parent) as entries:
                for entry in entries:
                    count += 1
                    if count > 128: return None
                    if entry.is_dir(follow_symlinks=False) and depth < 3:
                        pending.append((Path(entry.path), depth+1))
                    elif (entry.is_file(follow_symlinks=False) and entry.name.startswith('rollout-')
                          and entry.name.endswith('-'+thread+'.jsonl')):
                        candidates.append(Path(entry.path))
        if len(candidates) != 1: return None
        fd = os.open(candidates[0], os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode): return None
            head_limit, tail_limit = 256*1024, 512*1024
            head = stream.read(head_limit)
            first = head.split(b'\n', 1)[0]
            header = json.loads(first)
            if header.get('type') != 'session_meta' or header.get('payload', {}).get('id') != thread:
                return None
            truncated = info.st_size > head_limit+tail_limit
            if info.st_size > head_limit:
                offset = max(head_limit, info.st_size-tail_limit)
                stream.seek(offset)
                tail = stream.read(tail_limit)
                if offset == head_limit:
                    chunks = [head+tail]
                else:
                    chunks = [head.rsplit(b'\n', 1)[0], tail.split(b'\n', 1)[-1]]
            else:
                chunks = [head]
        result = {'source': 'native_session', 'usage_scope': 'last_recorded', 'truncated': truncated}
        for chunk in chunks:
            for line in chunk.splitlines():
                if len(line) > 256*1024: continue
                try: row = json.loads(line)
                except (ValueError, UnicodeError): continue
                if not isinstance(row, dict) or not isinstance(row.get('payload'), dict): continue
                payload = row['payload']
                if row.get('type') == 'turn_context':
                    model, effort = payload.get('model'), payload.get('effort')
                    if isinstance(model, str): result['model'] = model if model in CODEX_MODELS else 'unrecognized'
                    if isinstance(effort, str): result['reasoning_effort'] = effort if effort in CODEX_EFFORTS else 'unrecognized'
                elif row.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                    usage = payload.get('info')
                    if not isinstance(usage, dict): continue
                    for key in ('total_token_usage', 'last_token_usage'):
                        if isinstance(usage.get(key), dict):
                            result[key] = {k: v for k, v in usage[key].items() if k in TOKEN_FIELDS and token_number(v)}
                    if token_number(usage.get('model_context_window')):
                        result['model_context_window'] = usage['model_context_window']
        return result
    except (OSError, ValueError, TypeError, AttributeError):
        return None


def valid_provider_diagnostic(value):
    required = {'kind', 'event_scope', 'observed', 'error_fields', 'extra_error_fields'}
    if (not isinstance(value, dict) or not required <= set(value)
            or set(value) - required - {'structured_code', 'http_status', 'session'}
            or value['kind'] != 'unclassified_terminal_error' or value['event_scope'] != 'retained_tail'
            or type(value['extra_error_fields']) is not bool):
        return False
    observed, fields = value['observed'], value['error_fields']
    if (not isinstance(observed, list) or len(observed) > len(DIAGNOSTIC_EVENTS)
            or any(not isinstance(item, str) or item not in DIAGNOSTIC_EVENTS for item in observed)
            or len(set(observed)) != len(observed)
            or not isinstance(fields, dict) or set(fields) - DIAGNOSTIC_FIELDS
            or any(not isinstance(item, str) or item not in JSON_TYPES for item in fields.values())):
        return False
    if 'structured_code' in value and (not isinstance(value['structured_code'], str)
            or value['structured_code'] not in DIAGNOSTIC_CODES):
        return False
    if 'session' in value and not valid_codex_session(value['session']):
        return False
    return ('http_status' not in value or type(value['http_status']) is int and 400 <= value['http_status'] <= 599)


def codex_diagnostic(events, error):
    observed = set()
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = event.get('type')
        if isinstance(kind, str) and kind in ('thread.started', 'turn.started', 'error'):
            observed.add(kind)
        if kind in ('item.started', 'item.completed', 'item.updated') and isinstance(event.get('item'), dict):
            item = event['item'].get('type')
            if isinstance(item, str) and item in DIAGNOSTIC_EVENTS - {'thread.started', 'turn.started', 'error'}:
                observed.add(item)
    types = {type(None): 'null', bool: 'boolean', int: 'number', float: 'number',
             str: 'string', list: 'array', dict: 'object'}
    value = dict(kind='unclassified_terminal_error', event_scope='retained_tail', observed=sorted(observed),
                 error_fields={key: types[type(error[key])] for key in DIAGNOSTIC_FIELDS
                               if key in error and type(error[key]) in types},
                 extra_error_fields=bool(set(error) - DIAGNOSTIC_FIELDS))
    code, status = error.get('code'), error.get('status')
    if isinstance(code, str) and code in DIAGNOSTIC_CODES:
        value['structured_code'] = code
    if type(status) is int and 400 <= status <= 599:
        value['http_status'] = status
    return value


def fault(reason, *, phase='harness', file='/opt/forge/harness.py', line=1):
    value = dict(phase=phase, exception=reason, file=file, line=line)
    value['signature'] = hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    return value


def exception_fault(error):
    frames = [f for f in traceback.extract_tb(error.__traceback__) if f.filename.startswith('/opt/forge/')]
    frame = frames[-1] if frames else None
    return fault(type(error).__name__, file=frame.filename if frame else '/opt/forge/harness.py',
                 line=frame.lineno if frame else 1)


def write_receipt(note, status, *, harness_error=None, provider_error=None, path=RECEIPT):
    value = dict(version=1, attempt=note['id'], harness_status=status)
    if harness_error is not None:
        value['harness_error'] = harness_error
    if provider_error is not None:
        value['provider_error'] = provider_error
    path = Path(path)
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value)); tmp.chmod(0o600); tmp.replace(path)
    return value


def provider_error(response):
    """A terminal CLI error proves provider execution failed; cause may be unknown.

    Earlier retry events may precede a successful turn. Tool output and the model
    structured_output are deliberately not examined. Message text is irrelevant.
    """
    if (not isinstance(response, dict) or response.get('type') != 'result'
            or response.get('is_error') is not True):
        return None
    status = response.get('api_error_status')
    if type(status) is not int:
        return {'code': 'unknown'}
    if status in (401, 403):
        code = 'auth'
    elif status == 429:
        code = 'rate_limit'
    elif status in (500, 502, 503, 504, 529):
        code = 'overloaded'
    else:
        code = 'unknown'
    value = dict(code=code, status=status)
    retry = response.get('retry_after')
    if type(retry) in (int, float) and math.isfinite(retry) and retry >= 0:
        value['retry_after'] = min(3600, max(1, retry))
    return value


def valid(value, attempt, *, final=False):
    if (not isinstance(value, dict) or value.get('version') != 1 or type(value.get('version')) is not int
            or value.get('attempt') != attempt):
        return False
    status = value.get('harness_status')
    allowed = {'version', 'attempt', 'harness_status'} | ({'exit_code'} if final else set())
    if final and type(value.get('exit_code')) is not int:
        return False
    if status == 'harness_error':
        allowed.add('harness_error')
        detail = value.get('harness_error')
        if (not isinstance(detail, dict) or set(detail) != {'phase', 'exception', 'file', 'line', 'signature'}
                or detail.get('phase') not in ('harness', 'runner', 'before_agent', 'contract')
                or not isinstance(detail.get('exception'), str) or not detail['exception'].isidentifier()
                or len(detail['exception']) > 80 or not isinstance(detail.get('file'), str)
                or not detail['file'].startswith('/opt/forge/') or len(detail['file']) > 240
                or type(detail.get('line')) is not int or detail['line'] < 1):
            return False
        expected = hashlib.sha256(json.dumps({k:v for k,v in detail.items() if k != 'signature'}, sort_keys=True).encode()).hexdigest()
        if detail['signature'] != expected:
            return False
    elif status == 'provider_error':
        allowed.add('provider_error')
        detail = value.get('provider_error')
        if (not isinstance(detail, dict) or set(detail) - {'code', 'status', 'retry_after', 'diagnostic'}
                or detail.get('code') not in CODES
                or 'status' in detail and (type(detail['status']) is not int or not 400 <= detail['status'] <= 599)):
            return False
        if 'diagnostic' in detail and not valid_provider_diagnostic(detail['diagnostic']):
            return False
        if 'retry_after' in detail and (type(detail['retry_after']) not in (int, float)
                or not math.isfinite(detail['retry_after']) or not 1 <= detail['retry_after'] <= 3600):
            return False
    elif status not in ({'ok', 'timeout'} if final else {'ok'}):
        return False
    return set(value) == allowed


def finalize(note, code, *, timeout=False, receipt_path=RECEIPT, envelope_path=ENVELOPE):
    """Called by the image entrypoint after the harness process has stopped."""
    value = None
    try:
        path = Path(receipt_path)
        if path.stat().st_size <= 8192:
            value = json.loads(path.read_text())
    except (OSError, ValueError):
        pass
    if timeout:
        value = dict(version=1, attempt=note['id'], harness_status='timeout')
    elif not valid(value, note['id']):
        value = dict(version=1, attempt=note['id'], harness_status='harness_error',
                     harness_error=fault('MissingHarnessReceipt', phase='runner', file='/opt/forge/job.py'))
    value['exit_code'] = code
    path = Path(envelope_path)
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return value


def codex_provider_error(events):
    # codex exec 0.153.4 ThreadErrorEvent deliberately exposes only message.
    # Its terminal kind is trustworthy; a specific auth/quota cause is not.
    terminals = [event for event in events if isinstance(event, dict)
                 and event.get('type') in ('turn.failed', 'turn.completed')]
    if terminals and terminals[-1].get('type') == 'turn.failed' and isinstance(terminals[-1].get('error'), dict):
        return {'code': 'unknown', 'diagnostic': codex_diagnostic(events, terminals[-1]['error'])}
    return None


def stream(args, prompt, directory, env):
    """Forward provider stdout for inspection while retaining a bounded tail."""
    from collections import deque
    import os
    import subprocess
    import sys
    import tempfile
    with tempfile.TemporaryFile(mode='w+', dir='/work/home') as source:
        source.write(prompt); source.seek(0)
        child = subprocess.Popen(args, stdin=source, stdout=subprocess.PIPE,
                                 cwd=directory, env=env, close_fds=True)
        chunks, size = deque(), 0
        try:
            while True:
                block = os.read(child.stdout.fileno(), 65536)
                if not block:
                    break
                sys.stdout.buffer.write(block); sys.stdout.buffer.flush()
                chunks.append(block); size += len(block)
                while size > 2*1024*1024:
                    first = chunks.popleft(); excess = size - 2*1024*1024
                    if len(first) > excess:
                        chunks.appendleft(first[excess:]); size -= excess
                    else:
                        size -= len(first)
            code = child.wait()
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5); child.stdout.close()
    events = []
    for line in b''.join(chunks).splitlines():
        try:
            value = json.loads(line)
            if isinstance(value, dict): events.append(value)
        except (ValueError, UnicodeError):
            pass
    return code, events
