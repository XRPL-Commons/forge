"""Private immutable qualification evidence, never lifecycle authority or a UI view."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from .factory.runner import scrub
from .harnesses.execution import DIAGNOSTIC_CODES, valid
from .store.db import _private, _new_file, _fsync_directory

MAX_RECORD = 96 * 1024
MAX_SCHEMA = 64 * 1024
MAX_MESSAGE = 8192
IDENTIFIER = re.compile(r'[0-9a-f]{32}')


def directory(root, *, create=False):
    root = Path(root).absolute()
    _private(root, directory=True)
    for part in ('state', 'qualification-diagnostics'):
        root = root / part
        if create:
            root.mkdir(mode=0o700, exist_ok=True)
        _private(root, directory=True)
    return root


def terminal_error(capture, job, *, secrets=()):
    """Keep only Codex's final terminal error, after a matching sealed failure.

    This text remains untrusted diagnostic evidence. It never selects a retry,
    changes an outcome, or authorizes an action. Model/tool event text is omitted.
    Runner.capture already scrubs the exact credentials issued to this worker;
    scrub the parsed message again before truncation, including JSON encodings.
    """
    envelope, execution = capture.get('execution_envelope'), capture.get('execution') or {}
    if (job.get('provider', job.get('runner_config', {}).get('provider')) != 'codex'
            or not valid(envelope, job['id'], final=True)
            or envelope['harness_status'] != 'provider_error'
            or execution.get('running') is not False or execution.get('oom_killed')
            or envelope['exit_code'] != execution.get('exit_code') or envelope['exit_code'] == 0
            or capture.get('result') is not None):
        return None
    logs = capture.get('logs')
    if not isinstance(logs, str) or len(logs.encode()) > 256 * 1024:
        return None
    terminal = None
    for line in logs.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get('type') in ('turn.failed', 'turn.completed'):
            terminal = event
    if not terminal or terminal['type'] != 'turn.failed' or not isinstance(terminal.get('error'), dict):
        return None
    error = terminal['error']
    if not isinstance(error.get('message'), str):
        return None
    message = scrub(error['message'], secrets).encode()
    result = {'message': message[:MAX_MESSAGE].decode(errors='ignore'), 'truncated': len(message) > MAX_MESSAGE}
    if isinstance(error.get('code'), str) and error['code'] in DIAGNOSTIC_CODES:
        result['code'] = error['code']
    if type(error.get('status')) is int and 400 <= error['status'] <= 599:
        result['status'] = error['status']
    return result


def retain(root, capture, job, notes, *, source_revision, image, secrets=()):
    error = terminal_error(capture, job, secrets=secrets)
    if error is None:
        return None
    from .factory.harness import schema
    contract = schema(notes)
    encoded_schema = json.dumps(contract, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(encoded_schema) > MAX_SCHEMA:
        raise ValueError('Qualification schema exceeds diagnostic bound')
    identifier = uuid.uuid4().hex
    record = dict(version=1, diagnostic_id=identifier, created_at=time.time(), job_id=job['id'],
                  role=job['role'], provider='codex', source_revision=source_revision, image=image,
                  output_schema=contract, schema_sha256=hashlib.sha256(encoded_schema).hexdigest(),
                  terminal_error=error)
    data = json.dumps(record, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(data) > MAX_RECORD:
        raise ValueError('Qualification diagnostic exceeds its bound')
    target = directory(root, create=True)
    _new_file(target / identifier, data)
    _fsync_directory(target)
    return identifier


def read(root, identifier):
    if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
        raise ValueError('Invalid qualification diagnostic identity')
    path = directory(root) / identifier
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > MAX_RECORD):
            raise ValueError('Qualification diagnostic is not an owned private file')
        data = stream.read(MAX_RECORD + 1)
    record = json.loads(data)
    if len(data) > MAX_RECORD or not isinstance(record, dict) or record.get('diagnostic_id') != identifier:
        raise ValueError('Qualification diagnostic identity changed')
    return data


def main():
    # Internal SSH transport: the operator CLI writes this only to --output.
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True)
    parser.add_argument('--id', required=True)
    args = parser.parse_args()
    try:
        data = read(args.root, args.id)
    except (OSError, ValueError, RuntimeError):
        print(json.dumps({'ok': False, 'error': 'qualification_diagnostic_unavailable'}))
        return 1
    print(json.dumps({'ok': True, 'content': base64.b64encode(data).decode()}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
