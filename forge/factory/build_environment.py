#!/usr/local/bin/python3
"""Image-owned build tools and scratch space; no repository code at import time.

The container cgroup is the hard memory/CPU boundary. This helper serializes
builds within that boundary, bounds Node's heap and kills timed-out process groups.
The host safe-build is a different implementation of the same command contract.
"""
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys


def environment(directory):
    root = Path(directory) / '.forge'
    for name in ('tmp', 'cache', 'config', 'home', 'corepack', 'pnpm'):
        (root / name).mkdir(parents=True, exist_ok=True)
    if not (root / 'corepack/v1').exists():
        shutil.copytree('/opt/forge/corepack', root / 'corepack', dirs_exist_ok=True)
    return {
        'TMPDIR': str(root / 'tmp'), 'TMP': str(root / 'tmp'), 'TEMP': str(root / 'tmp'),
        'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_CONFIG_HOME': str(root / 'config'),
        'npm_config_cache': str(root / 'cache/npm'),
        'npm_config_store_dir': str(root / 'pnpm'), 'npm_config_nodedir': '/usr/local',
        'COREPACK_HOME': str(root / 'corepack'), 'COREPACK_ENABLE_DOWNLOAD_PROMPT': '0',
        'COREPACK_ENABLE_AUTO_PIN': '0',
        'PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH': '/usr/bin/chromium',
        'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD': '1', 'CHROME_BIN': '/usr/bin/chromium',
    }


def memory_limit():
    # Native sandboxes must expose this read-only cgroup fact. Environment
    # variables or a repository-owned file are not evidence of a hard limit.
    for path in ('/sys/fs/cgroup/memory.max', '/sys/fs/cgroup/memory/memory.limit_in_bytes'):
        try:
            value = int(Path(path).read_text().strip())
            if 128 * 1024**2 <= value < 1024**4:
                return value
        except (OSError, ValueError):
            pass
    raise RuntimeError('safe-build requires a finite container memory limit')


def safe_build(argv):
    if not argv:
        raise ValueError('Usage: safe-build COMMAND [ARGS...]')
    limit = memory_limit()
    timeout = min(2400, max(1, int(os.environ.get('SAFE_BUILD_TIMEOUT_SECONDS', '1200'))))
    # Reserve memory for native allocations, browsers and the parent harness.
    heap = max(64, (limit // 1024**2) * 60 // 100)
    env = {**os.environ, 'NODE_OPTIONS': '--max-old-space-size=' + str(heap)}
    lock = Path(os.environ.get('TMPDIR', '/tmp')) / 'forge-safe-build.lock'
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        child = subprocess.Popen(argv, env=env, start_new_session=True, close_fds=True)
        try:
            return child.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124
        finally:
            # Descendants may outlive their command. Keep the lock until the
            # entire process group has been signalled, including on success.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


def check():
    """Executed inside the native tool sandbox, without provider/API calls."""
    for argv in (["node", "-e", "const before=process.memoryUsage().rss; global.buffer=Buffer.alloc(64*1024*1024,7); if(process.memoryUsage().rss-before<32*1024*1024)process.exit(1)"],
                 ['npm', '--version'], ['pnpm', '--version'], ['git', '--version'],
                 ['cc', '--version'], ['chromium', '--version']):
        # From an image-owned directory: a sandbox may place placeholder files
        # (an empty package.json) in the checkout that break Corepack shims.
        subprocess.run(argv, check=True, timeout=30, stdout=subprocess.DEVNULL, cwd='/opt/forge')
    if safe_build(['node', '-e', 'console.log("bounded-build-ok")']):
        raise RuntimeError('Bounded build failed')
    subprocess.run(['node', '/opt/forge/browser-canary.cjs'], check=True, timeout=45)
    print('forge-build-environment-ok', flush=True)


def blocked(note, detail, blocker='toolchain'):
    value = {'version': 1, 'attempt': note['id'], 'role': note['role'],
             'outcome': 'blocked_environment', 'blocker': blocker, 'summary': detail}
    if note['role'] in ('review', 'audit'):
        value['candidate'] = note['candidate']['key']
    if note['role'] == 'audit':
        value.update(human_required='unknown', human_reason='Evaluation has not started')
    if note['role'] == 'recovery':
        value.update(pattern='', evidence=[], uncertainty='Environment unavailable', alternative=None)
    temporary = Path('/work/final-result.json')
    temporary.write_text(json.dumps(value)); temporary.replace('/work/result.json')


if __name__ == '__main__':
    try:
        if sys.argv[1:] == ['--check-environment']:
            check()
        else:
            raise SystemExit(safe_build(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('Forge build environment: ' + str(error), file=sys.stderr)
        raise SystemExit(125)
