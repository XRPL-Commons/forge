"""Standalone SSH installer payload. Uses only the Python standard library.

Keep this file importable without Forge: the local client sends its own copy to
the selected host before any server checkout exists. No HTTP endpoint runs it.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile
import time

STATE = Path('/var/lib/forge-install')
HOME = Path('/home/forge')
SOURCE = HOME / 'forge'
ROOT = HOME / '.local/share/forge'
REPOSITORY = 'https://github.com/XRPL-Commons/forge.git'
STEPS = ('packages', 'account', 'source', 'isolation', 'host')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'HOME': '/root', 'LANG': 'C.UTF-8',
       'DEBIAN_FRONTEND': 'noninteractive', 'GIT_TERMINAL_PROMPT': '0'}


class InstallError(ValueError):
    pass


def need(value, code):
    if not value:
        raise InstallError(code)


def run(argv, *, capture=False, timeout=1800):
    result = subprocess.run(argv, check=True, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.DEVNULL if capture else None,
                            env=ENV, timeout=timeout)
    if capture:
        need(len(result.stdout) <= 1048576, 'host_command_failed')
        return result.stdout.decode().strip()


def validate_intent(value):
    need(isinstance(value, dict) and set(value) == {'id', 'name', 'revision'}, 'invalid_request')
    need(isinstance(value['id'], str) and re.fullmatch('[a-f0-9]{32}', value['id']), 'invalid_request')
    need(isinstance(value['name'], str) and re.fullmatch('[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', value['name']), 'invalid_request')
    need(isinstance(value['revision'], str) and re.fullmatch('[a-f0-9]{40}', value['revision']), 'invalid_request')
    return value


def facts():
    need(sys.platform == 'linux' and os.geteuid() == 0, 'administrator_required')
    release = dict(line.split('=', 1) for line in Path('/etc/os-release').read_text().splitlines() if '=' in line)
    need(release.get('ID', '').strip('"') == 'ubuntu' and release.get('VERSION_ID', '').strip('"') == '24.04', 'unsupported_os')
    enabled = Path('/sys/module/apparmor/parameters/enabled')
    need(enabled.exists() and enabled.read_text().strip() == 'Y', 'apparmor_required')
    need(Path('/run/systemd/system').is_dir(), 'systemd_required')
    memory = next(int(line.split()[1]) for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemTotal:'))
    disk = os.statvfs('/home')
    return {'os': 'ubuntu', 'version': '24.04', 'cpus': os.cpu_count() or 1,
            'memory_mib': memory // 1024, 'free_disk_mib': disk.f_bavail * disk.f_frsize // 1048576}


def private_directory(path, *, create=False):
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    need(path.is_dir() and path.resolve() == path and path.stat().st_uid == 0
         and path.stat().st_mode & 0o077 == 0, 'unsafe_install_state')


def read():
    if not STATE.exists() and not STATE.is_symlink():
        return None
    private_directory(STATE)
    path = STATE / 'installation.json'
    need(not path.is_symlink(), 'unsafe_install_state')
    if not path.exists():
        # A directory can exist after an interruption before the first write.
        need(not any(p.name not in ('control.lock', 'work.lock') for p in STATE.iterdir()), 'install_state_missing')
        return None
    need(path.is_file() and path.stat().st_size <= 65536, 'unsafe_install_state')
    data = json.loads(path.read_text())
    need(isinstance(data, dict) and data.get('version') == 1, 'unsafe_install_state')
    validate_intent(data.get('intent'))
    need(data.get('step') in (*STEPS, 'complete') and isinstance(data.get('completed'), list)
         and all(step in STEPS for step in data['completed']), 'unsafe_install_state')
    return data


def write(data):
    fd, temporary = tempfile.mkstemp(dir=STATE, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, STATE / 'installation.json')
        fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


@contextmanager
def locked(name):
    fd = os.open(STATE / (name + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError('installation_busy') from None
        yield


def check_new_host():
    try:
        pwd.getpwnam('forge')
    except KeyError:
        pass
    else:
        raise InstallError('existing_installation')
    need(not HOME.exists() and not HOME.is_symlink(), 'existing_installation')
    need(not Path('/etc/sudoers.d/forge-installer').exists(), 'existing_installation')


def service(argv, *, capture=False):
    account = pwd.getpwnam('forge')
    need(account.pw_uid > 0 and account.pw_dir == str(HOME) and HOME.resolve() == HOME
         and HOME.stat().st_uid == account.pw_uid, 'existing_installation')
    return run(['runuser', '-u', 'forge', '--', 'env', '-i',
                'PATH=/usr/sbin:/usr/bin:/sbin:/bin', 'HOME=' + str(HOME), 'LANG=C.UTF-8',
                'XDG_RUNTIME_DIR=/run/user/' + str(account.pw_uid), *argv], capture=capture)


def unit(data):
    return 'forge-install-' + data['intent']['id']


def running(data):
    result = subprocess.run(['systemctl', 'is-active', unit(data)], env=ENV,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    return result.stdout.strip() in (b'active', b'activating', b'deactivating', b'reloading')


def view(data, host):
    return {'intent': data['intent'] if data else None, 'host': host,
            'status': ('installed' if data['step'] == 'complete' else
                       'installing' if running(data) else 'interrupted') if data else 'not_installed',
            'step': data['step'] if data else None,
            'completed': data['completed'] if data else [],
            'error': data.get('error') if data else None,
            'source': str(SOURCE), 'root': str(ROOT), 'account': 'forge'}


def dispatch(request, payload):
    need(isinstance(request, dict) and set(request) == {'version', 'action', 'intent'}, 'invalid_request')
    need(type(request['version']) is int and request['version'] == 1
         and request['action'] in ('plan', 'apply', 'status', 'resume'), 'invalid_request')
    intent = validate_intent(request['intent'])
    host = facts()
    data = read()
    if data:
        need(data['intent'] == intent, 'installation_conflict')
    else:
        check_new_host()
    if request['action'] in ('plan', 'status'):
        return view(data, host)
    private_directory(STATE, create=True)
    with locked('control'):
        data = read()
        if data:
            need(data['intent'] == intent, 'installation_conflict')
            if data['step'] == 'complete' or running(data):
                return view(data, host)
        else:
            check_new_host()
            data = {'version': 1, 'intent': intent, 'step': STEPS[0], 'completed': [],
                    'started_at': time.time(), 'error': None,
                    'payload': hashlib.sha256(payload.encode()).hexdigest()}
            write(data)
        # Resume the same installer implementation, not a changed client payload.
        need(data['payload'] == hashlib.sha256(payload.encode()).hexdigest(), 'installer_changed')
        worker = STATE / 'worker.py'
        need(not worker.is_symlink(), 'unsafe_install_state')
        worker.write_text(payload); worker.chmod(0o600)
        log = STATE / 'install.log'
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        # systemd owns the process: losing SSH or the client cannot kill the job.
        run(['systemd-run', '--quiet', '--collect', '--unit=' + unit(data),
             '--property=Type=exec', '--property=UMask=0077',
             '--property=StandardOutput=append:' + str(log),
             '--property=StandardError=append:' + str(log),
             '/usr/bin/python3', str(worker), '--worker'], capture=True, timeout=30)
        return view(read(), host)


def install_step(step, data):
    if step == 'packages':
        run(['apt-get', 'update'])
        run(['apt-get', 'install', '-y', 'ca-certificates', 'git', 'sudo', 'apparmor',
             'apparmor-utils', 'docker.io', 'docker-compose-v2', 'python3'])
        run(['systemctl', 'enable', '--now', 'docker'])
    elif step == 'account':
        try:
            account = pwd.getpwnam('forge')
        except KeyError:
            run(['useradd', '--create-home', '--home-dir', str(HOME), '--shell', '/bin/bash', 'forge'])
            account = pwd.getpwnam('forge')
        need(account.pw_dir == str(HOME) and account.pw_uid != 0 and HOME.resolve() == HOME,
             'existing_installation')
        run(['usermod', '-aG', 'docker', 'forge'])
        run(['loginctl', 'enable-linger', 'forge'])
        run(['systemctl', 'start', 'user@' + str(account.pw_uid) + '.service'])
    elif step == 'source':
        revision = data['intent']['revision']
        need(not SOURCE.is_symlink(), 'source_conflict')
        if not SOURCE.exists():
            service(['git', 'init', str(SOURCE)])
        git = ['git', '-C', str(SOURCE)]
        remotes = service(git + ['remote'], capture=True)
        if not remotes:
            service(git + ['remote', 'add', 'origin', REPOSITORY])
        need(service(git + ['remote', 'get-url', 'origin'], capture=True) == REPOSITORY, 'source_conflict')
        need(not service(git + ['status', '--porcelain'], capture=True), 'source_conflict')
        service(git + ['fetch', '--depth=1', 'origin', revision])
        service(git + ['checkout', '--detach', revision])
        need(service(git + ['rev-parse', 'HEAD'], capture=True) == revision, 'source_conflict')
    elif step == 'isolation':
        source = SOURCE / 'deploy/runner/forge-runner-private-proc.apparmor'
        target = Path('/etc/apparmor.d/forge-runner-private-proc')
        need(not target.is_symlink() and (not target.exists() or target.read_bytes() == source.read_bytes()), 'policy_conflict')
        run(['install', '-m', '0644', str(source), str(target)])
        run(['apparmor_parser', '-r', str(target)])
    elif step == 'host':
        need(service(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], capture=True) == data['intent']['revision'], 'source_conflict')
        need(not service(['git', '-C', str(SOURCE), 'status', '--porcelain', '--untracked-files=no'], capture=True), 'source_conflict')
        service([str(SOURCE / 'bin/forge'), 'setup', 'install', '--root', str(ROOT), '--bind', '127.0.0.1'])
        for name in ('forge-status', 'forge-ui', 'forge-supervisor'):
            need(service(['systemctl', '--user', 'is-active', name], capture=True) == 'active', 'services_unavailable')


def work():
    os.umask(0o077)
    private_directory(STATE)
    with locked('work'):
        data = read()
        try:
            facts()
            for step in STEPS:
                if step in data['completed']:
                    continue
                data.update(step=step, error=None); write(data)
                install_step(step, data)
                data['completed'].append(step)
                write(data)
            data.update(step='complete', error=None, finished_at=time.time()); write(data)
        except Exception as error:
            data['error'] = str(error) if isinstance(error, InstallError) else 'host_command_failed'
            write(data)
            return 1
    return 0


def main(payload=None):
    os.umask(0o077)
    if sys.argv[1:] == ['--worker']:
        return work()
    try:
        raw = sys.stdin.buffer.read(8193)
        need(len(raw) <= 8192, 'invalid_request')
        result = dispatch(json.loads(raw), payload)
        print(json.dumps({'ok': True, **result}, sort_keys=True))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, InstallError) else 'host_command_failed'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
