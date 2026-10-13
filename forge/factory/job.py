"""Image entrypoint. Finite harness execution; stdout is inspection, not a verdict.

Runs with an isolated container PID namespace and no controller filesystem/socket.
No imports from the repository being worked on. The image owns this file read-only.
"""
import json
import os
from pathlib import Path
import signal
import select
import subprocess
import time
import runpy


STARTUP_EXIT = 78
HARNESS_ARGV = ['/usr/local/bin/python3', '-I', '/opt/forge/harness.py']


def startup_receipt(raw, returncode):
    """Only one trusted pre-agent exception, never a worker's stderr, is proof."""
    if not returncode or len(raw) > 4096 or b'agent_started' in raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if (not isinstance(value, dict) or set(value) != {'version', 'phase', 'exception', 'file', 'line', 'signature'}
            or type(value['version']) is not int or value['version'] != 1 or value['phase'] != 'before_agent'
            or not isinstance(value['exception'], str) or value['exception'] not in {'ImportError', 'ModuleNotFoundError', 'NameError', 'UnboundLocalError', 'SyntaxError', 'IndentationError', 'TabError'}
            or not isinstance(value['file'], str) or not value['file'].startswith('/opt/forge/')
            or len(value['file']) > 240 or type(value['line']) is not int or value['line'] < 1):
        return None
    import hashlib
    expected = hashlib.sha256(json.dumps({k:v for k,v in value.items() if k != 'signature'}, sort_keys=True).encode()).hexdigest()
    return value if value['signature'] == expected else None


def completed_startup(raw, returncode, attempt):
    receipt = startup_receipt(raw, returncode)
    if receipt is not None:
        return STARTUP_EXIT, {**receipt, 'attempt': attempt}
    return (1 if returncode == STARTUP_EXIT else returncode), None


class StartupChannel:
    """Parent-owned phase latch; no proof descriptor survives agent admission."""
    def __init__(self, read_fd, ack_fd):
        self.read_fd, self.ack_fd = read_fd, ack_fd
        self.raw = b''
        self.agent_started = False

    def close(self):
        for name in ('read_fd', 'ack_fd'):
            descriptor = getattr(self, name)
            if descriptor is not None:
                os.close(descriptor)
                setattr(self, name, None)

    def read(self, timeout=0):
        if self.read_fd is None or not select.select([self.read_fd], [], [], timeout)[0]:
            return
        chunk = os.read(self.read_fd, 4097 - len(self.raw))
        self.raw += chunk
        if self.raw == b'agent_started\n':
            self.agent_started = True
            # Latch and destroy the proof channel before allowing provider code.
            os.close(self.read_fd)
            self.read_fd = None
            try:
                os.write(self.ack_fd, b'1')
            except BrokenPipeError:
                pass  # The child exited before admission, not a startup proof.
            self.close()
        elif not chunk or len(self.raw) >= 4097:
            self.close()

    def completed(self, returncode, attempt):
        if self.agent_started:
            return (1 if returncode == STARTUP_EXIT else returncode), None
        return completed_startup(self.raw, returncode, attempt)


def main():
    note = json.loads(Path('/work/assignment.json').read_text())
    credentials = json.loads(Path('/work/credentials.json').read_text())
    Path('/work/credentials.json').unlink()
    Path('/work/home').mkdir(mode=0o700, exist_ok=True)
    auth = credentials.pop('FORGE_HARNESS_AUTH', None)
    origin = credentials.pop('FORGE_AUTH_DIGEST', None)
    if auth is not None:
        registry = runpy.run_path('/opt/forge/harnesses/registry.py')
        target = Path('/work/home') / registry['manifest'](note.get('provider', 'codex'))['auth_file']
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        target.write_text(json.dumps(auth)); target.chmod(0o600)
        Path('/work/home/.forge-auth-origin').write_text(origin)
    env = {'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/work/home', 'LANG': 'C.UTF-8',
           'FORGE_ASSIGNMENT': '/work/assignment.json', 'FORGE_RESULT': '/work/result.json', **credentials}
    env.update(HTTPS_PROXY=note['proxy'], HTTP_PROXY=note['proxy'], https_proxy=note['proxy'], http_proxy=note['proxy'])
    # No shell interpolation. An image-provided adapter reads the assignment and
    # invokes its pinned harness. The contract works with any coding provider.
    timeout = max(1, note['deadline'] - time.time())
    argv = note['harness_argv']
    channel = None
    write_fd = ack_read = None
    if argv == HARNESS_ARGV:
        read_fd, write_fd = os.pipe()
        ack_read, ack_write = os.pipe()
        channel = StartupChannel(read_fd, ack_write)
        env.update(FORGE_STARTUP_FD=str(write_fd), FORGE_STARTUP_ACK_FD=str(ack_read))
        argv = [*argv[:-1], '/opt/forge/startup.py']
    try:
        try:
            child = subprocess.Popen(argv, env=env, cwd='/work', start_new_session=True,
                                     stdin=subprocess.DEVNULL, close_fds=True,
                                     pass_fds=(write_fd, ack_read) if channel else ())
        finally:
            for descriptor in (write_fd, ack_read):
                if descriptor is not None:
                    os.close(descriptor)
        end = time.monotonic() + timeout
        next_heartbeat = 0
        while child.poll() is None and time.monotonic() < end:
            now = time.monotonic()
            if now >= next_heartbeat:
                print(json.dumps({'runner': 'alive', 'attempt': note['id'], 'at': time.time()}), flush=True)
                next_heartbeat = now + 5
            delay = max(0, min(next_heartbeat, end) - now)
            if channel and channel.read_fd is not None:
                channel.read(delay)
            else:
                time.sleep(delay)
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=note['grace_seconds'])
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)
            return 124
        if channel:
            channel.read()  # A fast pre-agent exception may precede the first poll.
            code, receipt = channel.completed(child.returncode, note['id'])
            if receipt is not None:
                Path('/work/forge-startup.json').write_text(json.dumps(receipt))
            return code
    finally:
        if channel:
            channel.close()
    # Custom harnesses cannot emit the entrypoint's reserved startup classification.
    return 1 if child.returncode == STARTUP_EXIT else child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
