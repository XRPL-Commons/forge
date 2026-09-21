"""Terminal setup: install the host, connect harness accounts, then open web setup."""
import argparse
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import platform
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
from contextlib import contextmanager

from .onboarding import Onboarding
from .settings import read_json, write_json
from .setup_service import Service
from .factory.control import controller_lock, ControllerBusy
from .harnesses.registry import manifests, manifest
from .harnesses.accounts import owner as login_owner
from .state import StateError

SOURCE = Path(__file__).resolve().parent.parent
NODE_IMAGE = 'node:22-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5'
PYTHON_IMAGE = 'python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea'
SECCOMP_URL = 'https://raw.githubusercontent.com/moby/profiles/3c28324314729dbade8287e868eef6338c42807a/seccomp/default.json'
SECCOMP_SHA = '536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74'


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def private_root(root):
    root = Path(root).absolute()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink() or root.stat().st_uid != os.getuid() or root.stat().st_mode & 0o077:
        raise ValueError('Use a private runtime directory owned by the Forge service account (mode 0700)')
    return root


def install(root, bind='127.0.0.1'):
    """Idempotent host preparation; never reset or start issue processing."""
    if platform.system() != 'Linux' or os.getuid() == 0:
        raise ValueError('Run setup on Linux as the non-root Forge service account')
    for binary in ('docker', 'systemctl', 'python3'):
        if not shutil.which(binary):
            raise ValueError('Install Docker Engine with Compose, Python 3, and systemd first; see docs/installation.md')
    command(['docker', 'info'], stdout=subprocess.DEVNULL)
    command(['docker', 'compose', 'version'], stdout=subprocess.DEVNULL)
    command(['systemctl', '--user', 'show-environment'], stdout=subprocess.DEVNULL)
    if (root / 'host-runtime.json').exists():
        print('Host already configured; preserving its image, policies and services.')
        return
    tag = 'forge-runner:setup-' + hashlib.sha256(str(root).encode()).hexdigest()[:12]
    command(['docker', 'build', '--build-arg', 'NODE_IMAGE=' + NODE_IMAGE, '--build-arg', 'PYTHON_IMAGE=' + PYTHON_IMAGE,
             '-f', str(SOURCE / 'deploy/runner/Dockerfile'), '-t', tag, str(SOURCE)])
    image = command(['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], capture_output=True, text=True).stdout.strip()
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / 'seccomp.json'
        with urllib.request.urlopen(SECCOMP_URL, timeout=30) as response:
            source.write_bytes(response.read(1024 * 1024))
        generate = runpy.run_path(str(SOURCE / 'deploy/runner/seccomp_profile.py'))['generate']
        write_json(root / 'runner-seccomp.json', generate(source, SECCOMP_SHA))
    apparmor = Path('/sys/module/apparmor/parameters/enabled')
    profile = None
    if apparmor.exists() and apparmor.read_text().strip() == 'Y':
        profile = 'forge-runner-private-proc'
        target = Path('/etc/apparmor.d/forge-runner-private-proc')
        source = SOURCE / 'deploy/runner/forge-runner-private-proc.apparmor'
        if target.exists() and target.read_bytes() != source.read_bytes():
            raise ValueError('An existing Forge AppArmor policy differs; review it before replacing it')
        if not target.exists():
            command(['sudo', 'install', '-m', '0644', str(source), str(target)])
            command(['sudo', 'apparmor_parser', '-a', str(target)])
    network_dir = root / 'deploy/runner'; network_dir.mkdir(parents=True, exist_ok=True)
    for name in ('compose.yaml', 'egress.py'):
        shutil.copyfile(SOURCE / 'deploy/runner' / name, network_dir / name)
    command(['docker', 'compose', '-p', 'forge', '-f', str(network_dir / 'compose.yaml'), 'up', '-d', 'egress'],
            env={**os.environ, 'FORGE_EGRESS_IMAGE': PYTHON_IMAGE})
    config = Onboarding(root).runtime()
    config.update(image=image, apparmor_profile=profile)
    # Image presence is not readiness. This runs without accounts or network,
    # before any service or success marker is installed on a fresh host.
    runpy.run_path(str(SOURCE / 'deploy/test_build_environment.py'))['validate'](
        image, root / 'runner-seccomp.json', profile)
    # Save last: failed installation can be repeated without a false success marker.
    install_services(root, bind)
    write_json(root / 'host-runtime.json', config)


def unit_quote(value):
    return json.dumps(str(value).replace('%', '%%'))


def install_services(root, bind):
    import ipaddress
    address = ipaddress.ip_address(bind)
    if not address.is_loopback and not (address.version == 4 and address in ipaddress.ip_network('100.64.0.0/10')):
        raise ValueError('Bind the unauthenticated operator UI to loopback or a private tailnet address')
    units = Path.home() / '.config/systemd/user'; units.mkdir(parents=True, exist_ok=True)
    commands = {
        'forge-status': '/usr/bin/python3 ' + unit_quote(SOURCE / 'legacy/forge-status.py'),
        'forge-supervisor': unit_quote(SOURCE / 'bin/forge') + ' service --root ' + unit_quote(root),
    }
    pending = {}
    for name, execution in commands.items():
        path = units / (name + '.service')
        content = ('[Unit]\nDescription=Forge ' + name + '\nAfter=network-online.target\n\n[Service]\n'
                        'Type=simple\nExecStart=' + execution + '\n'
                        'Environment=' + unit_quote('PYTHONPATH=' + str(SOURCE)) + '\n'
                        'Environment=' + unit_quote('FORGE_ROOT=' + str(root)) + '\n'
                        'Environment=' + unit_quote('FORGE_BIND_ADDRESS=' + bind) + '\n'
                        'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nUMask=0077\n\n'
                        '[Install]\nWantedBy=default.target\n')
        if path.exists() and path.read_text() != content:
            raise ValueError('Existing Forge services differ; use the documented upgrade procedure')
        pending[path] = content
    for path, content in pending.items():
        path.write_text(content)
    command(['systemctl', '--user', 'daemon-reload'])
    command(['systemctl', '--user', 'enable', '--now', 'forge-status.service', 'forge-supervisor.service'])


@contextmanager
def login_lock(root, provider):
    with open(root / ('state-login-' + provider + '.lock'), 'a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another terminal is already connecting this harness') from None
        yield


def login(root, provider, **kwargs):
    manifest(provider)
    with login_lock(root, provider):
        return connect_login(root, provider, **kwargs)


def pending_login(setup, provider, generation, record=None):
    """Keep successful sign-ins private and recoverable until their handoff commits."""
    setup.credentials._private()
    path = setup.credentials.root / ('pending-login-' + provider + '.json')
    if record is not None:
        # Reset owns credential removal; never resurrect an older generation's login.
        with setup.store.transaction() as data:
            if data['generation'] != generation or data['phase'] not in ('setup', 'active'):
                raise ValueError('Forge was reset during login; reconnect from the current setup')
            write_json(path, {'generation': generation, **record})
        return record
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 262144:
            raise ValueError('Saved sign-in must be a private, bounded file owned by the Forge account')
        record = json.load(stream)
    if record.get('generation') != generation:
        path.unlink()
        return None
    return record


def cleanup_login(name):
    owned = subprocess.run(['docker', 'inspect', '--format', '{{index .Config.Labels "forge.login"}}', name],
                           capture_output=True, text=True)
    if owned.returncode == 0:
        if owned.stdout.strip() != name:
            raise ValueError('A login container has unexpected ownership; inspect it before retrying')
        command(['docker', 'rm', '--force', name], stdout=subprocess.DEVNULL)


def connect_login(root, provider, *, api_key=False, import_login=None, models=None):
    item = manifest(provider)
    setup = Onboarding(root)
    initial = setup.read()
    if initial['phase'] not in ('setup', 'active') or initial.get('operation'):
        raise ValueError('Finish the current setup operation before signing in')
    generation = initial['generation']
    saved = pending_login(setup, provider, generation) if not api_key and not import_login else None
    if saved:
        print('Finishing the saved ' + item['name'] + ' sign-in; no browser login is needed.', flush=True)
        secret, value = saved['secret'], saved['value']
        models = models if models is not None else saved['models']
        if saved.get('interactive'):
            cleanup_login('forge-login-' + login_owner(root) + '-' + provider)
    elif api_key:
        secret = getpass.getpass(item['name'] + ' API key: ')
        value = None
    elif import_login:
        path = Path(import_login)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise ValueError('Use a regular login file smaller than 64 KiB')
        secret, value = None, path.read_text()
    else:
        config = setup.runtime()
        if not config['image']:
            raise ValueError('Install the runner image first: bin/forge setup install --root ' + str(root))
        name = 'forge-login-' + login_owner(root) + '-' + provider
        old = subprocess.run(['docker', 'inspect', '--format', '{{index .Config.Labels "forge.login"}}', name],
                             capture_output=True, text=True)
        if old.returncode == 0:
            if old.stdout.strip() != name:
                raise ValueError('A login container has unexpected ownership; inspect it before retrying')
            command(['docker', 'rm', '--force', name], stdout=subprocess.DEVNULL)
        # This container runs only the trusted CLI login command, without a repo,
        # GitHub credential, Docker socket or existing host authentication.
        # Intent precedes container creation; reset can find even an interrupted
        # login without relying on its parent terminal process.
        write_json(root / 'state/harness-login-enabled.json', {'owner': login_owner(root)})
        setup.credentials._private()
        with tempfile.TemporaryDirectory(prefix='login-', dir=setup.credentials.root) as temporary:
            home = Path(temporary); home.chmod(0o700)
            for relative, content in item.get('login_files', {}).items():
                path = home / relative
                path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                path.write_text(content)
            args = ['docker', 'create', '-it', '--name', name, '--label', 'forge.login=' + name,
                    '--label', 'forge.login-root=' + login_owner(root),
                    '--log-driver', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--user', str(os.getuid()) + ':' + str(os.getgid()), '--pids-limit', '128', '--memory', '1g',
                    '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m', '--mount', 'type=bind,source=' + temporary + ',target=/work/home',
                    '--env', 'HOME=/work/home', '--env', 'DISABLE_AUTOUPDATER=1',
                    '--entrypoint', item['command'], config['image'], *item['login']]
            try:
                # Creation is serialized with reset intent; attachment does not
                # hold the setup lock. Reset may terminate login at any time.
                with setup.store.transaction() as data:
                    if data['generation'] != generation or data['phase'] not in ('setup', 'active') or data.get('operation'):
                        raise ValueError('Setup changed during login preparation; retry from the current setup')
                    identifier = command(args, capture_output=True, text=True, timeout=30).stdout.strip()
                command(['docker', 'start', '--attach', '--interactive', identifier], timeout=900)
                path = home / item['auth_file']
                if not path.is_file() or path.stat().st_size > 65536:
                    raise ValueError('The harness did not save a usable login; retry or use --api-key')
                value, secret = path.read_text(), None
                # Persist before deleting the temporary login container/home.
                saved = pending_login(setup, provider, generation, {'secret': secret, 'value': value, 'models': models, 'interactive': True})
            finally:
                # Check ownership before cleanup, including interrupted login.
                cleanup_login(name)
    if not saved:
        pending_login(setup, provider, generation, {'secret': secret, 'value': value, 'models': models})
    print('Sign-in saved locally. Waiting for the current supervisor cycle to finish…', flush=True)
    try:
        with controller_lock(root, timeout=120):
            if setup.read()['generation'] != generation:
                raise ValueError('Forge was reset during login; reconnect from the current setup')
            setup.connect(provider, api_key=secret, auth_value=value, models=models)
            ledger = root / 'state/factory/factory-ledger.json'
            if ledger.exists():
                from .factory.ledger import Ledger
                with Ledger(ledger.parent).transaction() as data:
                    data.get('provider_cooldowns', {}).pop(provider, None)
            (setup.credentials.root / ('pending-login-' + provider + '.json')).unlink(missing_ok=True)
    except ControllerBusy:
        raise ValueError('Sign-in is saved, but the supervisor is still busy. Rerun the same login command to finish; you will not need to sign in again.') from None
    print(item['name'] + ' connected. Run setup check to verify the runner.')


def check(root):
    setup = Onboarding(root)
    operation = setup.queue('harness_check', setup.view()['revision'])
    service = Service(root)
    deadline = time.monotonic() + 900
    # If the service is running it owns progress. Otherwise this terminal drives
    # the same durable state machine, so closing it cannot lose runner ownership.
    with open(root / 'state-setup-service.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            drive = True
        except BlockingIOError:
            drive = False
        while time.monotonic() < deadline:
            data = setup.read()
            if not data.get('operation'):
                if data.get('error'):
                    raise ValueError(data['error'])
                print('Harness requests, isolation, termination and reuse verified.')
                return
            if data['operation']['id'] != operation:
                raise ValueError('Verification was superseded; inspect setup status')
            if drive:
                service.tick()
            time.sleep(2)
    raise ValueError('Verification is still pending. Check setup status; cleanup ownership is retained.')


def status(root):
    view = Onboarding(root).view()
    for item in view['harnesses']:
        state = ', '.join((m['model'] or 'default model') + ': ' + m['status'] for m in item['models']) or 'not connected'
        print(item['name'] + ' ' + item['version'] + ' — ' + state)
    if view.get('error'):
        print(view['error'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', nargs='?', choices=['install', 'login', 'check', 'status'], default=None)
    parser.add_argument('provider', nargs='?', choices=list(manifests()))
    parser.add_argument('--root', default=str(Path.home() / '.local/share/forge'))
    parser.add_argument('--bind', default='127.0.0.1')
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument('--api-key', action='store_true')
    auth.add_argument('--import-login')
    parser.add_argument('--model', action='append', help='Repeat to rotate equally between models of this harness')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        root = private_root(args.root)
        if args.action in (None, 'install'):
            install(root, args.bind)
        if args.action == 'login':
            if not args.provider:
                parser.error('login requires a harness name')
            login(root, args.provider, api_key=args.api_key, import_login=args.import_login, models=args.model)
        elif args.action == 'check':
            check(root)
        elif args.action == 'status':
            status(root)
        elif args.action is None:
            for key, item in manifests().items():
                answer = input('Connect ' + item['name'] + '? [Y/n] ').strip().lower()
                if answer not in ('n', 'no'):
                    login(root, key)
            if Onboarding(root).view()['provider_connected']:
                print('Checking connections with a small model request per entry; provider charges may apply.')
                check(root)
            print('Continue GitHub setup at http://' + args.bind + ':8787/onboarding')
            print('For a remote VM, forward port 8787 over SSH to reach the loopback dashboard.')
        return 0
    except (OSError, ValueError, StateError, subprocess.SubprocessError) as error:
        print('Setup did not finish: ' + (str(error) if isinstance(error, (ValueError, StateError)) else 'Host command failed; correct it and rerun setup.'), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nSetup interrupted. Saved connections are retained; rerun setup to continue.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())
