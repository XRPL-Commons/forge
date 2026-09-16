"""Image entrypoint. Finite harness execution; stdout is inspection, not a verdict.

Runs with an isolated container PID namespace and no controller filesystem/socket.
No imports from the repository being worked on. The image owns this file read-only.
"""
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import runpy


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
    child = subprocess.Popen(note['harness_argv'], env=env, cwd='/work', start_new_session=True,
                             stdin=subprocess.DEVNULL, close_fds=True)
    end = time.monotonic() + timeout
    while child.poll() is None and time.monotonic() < end:
        print(json.dumps({'runner': 'alive', 'attempt': note['id'], 'at': time.time()}), flush=True)
        time.sleep(min(5, max(0, end - time.monotonic())))
    if child.poll() is None:
        os.killpg(child.pid, signal.SIGTERM)
        try:
            child.wait(timeout=note['grace_seconds'])
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        return 124
    return child.returncode


if __name__ == '__main__':
    raise SystemExit(main())
