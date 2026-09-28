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
import socket
import tempfile
import time
from contextlib import contextmanager


def environment(directory, services=()):
    root = Path(directory) / '.forge'
    for name in ('tmp', 'cache', 'config', 'home', 'corepack', 'pnpm'):
        (root / name).mkdir(parents=True, exist_ok=True)
    if not (root / 'corepack/v1').exists():
        shutil.copytree('/opt/forge/corepack', root / 'corepack', dirs_exist_ok=True)
    return {**service_environment(services),
        'TMPDIR': str(root / 'tmp'), 'TMP': str(root / 'tmp'), 'TEMP': str(root / 'tmp'),
        'XDG_CACHE_HOME': str(root / 'cache'), 'XDG_CONFIG_HOME': str(root / 'config'),
        'npm_config_cache': str(root / 'cache/npm'),
        'npm_config_store_dir': str(root / 'pnpm'), 'npm_config_nodedir': '/usr/local',
        'COREPACK_HOME': str(root / 'corepack'), 'COREPACK_ENABLE_DOWNLOAD_PROMPT': '0',
        'COREPACK_ENABLE_AUTO_PIN': '0',
        'PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH': '/usr/bin/chromium',
        'PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD': '1', 'CHROME_BIN': '/usr/bin/chromium',
    }


SERVICE_PORTS = {'mongodb': 27017, 'postgres': 5432, 'redis': 6379}


def service_environment(services):
    if any(s not in SERVICE_PORTS for s in services) or len(set(services)) != len(services):
        raise ValueError('Unsupported database service')
    env = {'FORGE_REQUIRED_SERVICES': ','.join(services)}
    if 'mongodb' in services:
        env.update(MONGODB_URI='mongodb://127.0.0.1:27017/forge', MONGOMS_SYSTEM_BINARY='/usr/bin/mongod',
                   MONGOMS_RUNTIME_DOWNLOAD='false')
    if 'postgres' in services:
        env['DATABASE_URL'] = 'postgresql://forge@127.0.0.1:5432/postgres'
    if 'redis' in services:
        env['REDIS_URL'] = 'redis://127.0.0.1:6379/0'
    return env


@contextmanager
def running_services(directory, requested):
    """Job-local processes, data and sockets; no daemon, host port or new capability."""
    env = service_environment(requested)
    root = Path(directory) / '.forge/services'
    root.mkdir(parents=True, exist_ok=True)
    with (root / 'lock').open('a') as lock, tempfile.TemporaryDirectory(dir=root) as temporary:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        work = Path(temporary).resolve()
        processes, logs = [], []
        try:
            for service in requested:
                data = work / service; data.mkdir()
                if service == 'mongodb':
                    argv = ['mongod', '--bind_ip', '127.0.0.1', '--port', '27017', '--dbpath', str(data),
                            '--wiredTigerCacheSizeGB', '0.25', '--nounixsocket']
                elif service == 'postgres':
                    subprocess.run(['initdb', '-D', str(data), '-U', 'forge', '-A', 'trust', '--no-locale'],
                                   check=True, timeout=30, stdout=subprocess.DEVNULL)
                    argv = ['postgres', '-D', str(data), '-h', '127.0.0.1', '-p', '5432', '-k', str(work)]
                else:
                    argv = ['redis-server', '--bind', '127.0.0.1', '--port', '6379', '--save', '',
                            '--appendonly', 'no', '--dir', str(data)]
                log = (work / (service + '.log')).open('w+'); logs.append(log)
                child = subprocess.Popen(argv, stdout=log, stderr=log, start_new_session=True)
                processes.append(child)
                deadline = time.monotonic() + 30
                while True:
                    if child.poll() is not None or time.monotonic() >= deadline:
                        log.seek(0)
                        raise RuntimeError(service + ' failed to start: ' + log.read(2000))
                    try:
                        with socket.create_connection(('127.0.0.1', SERVICE_PORTS[service]), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
            yield env
        finally:
            for child in reversed(processes):
                try: os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError: pass
            for child in reversed(processes):
                try: child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try: os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError: pass
                    child.wait()
            for log in logs:
                log.close()


def with_services(argv):
    requested = [s for s in os.environ.get('FORGE_REQUIRED_SERVICES', '').split(',') if s]
    with running_services(Path.cwd(), requested) as env:
        os.environ.update(env)
        return safe_build(argv)


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
    if Path('/opt/forge/full-environment').exists():
        with running_services(Path.cwd(), list(SERVICE_PORTS)) as env:
            subprocess.run(['node', '/opt/forge/services-canary.cjs'], env={**os.environ, **env},
                           check=True, timeout=30)
    print('forge-build-environment-ok', flush=True)


def blocked(note, detail, blocker='toolchain'):
    value = {'version': 1, 'attempt': note['id'], 'role': note['role'],
             'outcome': 'blocked_environment', 'blocker': blocker, 'summary': detail,
             'headline': 'Stopped: Forge’s working environment is not ready',
             'plain': ('The worker could not start because its environment is missing something it needs. '
                       'No code was changed by this attempt. Forge waits for the environment to be repaired.')}
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
        elif Path(sys.argv[0]).name == 'forge-services':
            raise SystemExit(with_services(sys.argv[1:]))
        else:
            raise SystemExit(safe_build(sys.argv[1:]))
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print('Forge build environment: ' + str(error), file=sys.stderr)
        raise SystemExit(125)
