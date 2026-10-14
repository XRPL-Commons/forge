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
        if (not isinstance(detail, dict) or set(detail) - {'code', 'status', 'retry_after'}
                or detail.get('code') not in CODES
                or 'status' in detail and (type(detail['status']) is not int or not 400 <= detail['status'] <= 599)):
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
        return {'code': 'unknown'}
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
