"""Converge a Forge host to its checked-out source.

`bin/forge setup` and `bin/forge update` run the same reconciliation. Each host
component (runner image, isolation policies, egress, operator UI, services) is
compared with what this source wants; only what changed is rebuilt, qualified,
replaced and restarted. Every new artifact is built and the runner canary passes
before anything in use is touched, so a failed update leaves the host as it was.
Rolling back is the same operation on an older checkout.
"""
import difflib
import hashlib
import ipaddress
import json
import os
import re
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from .onboarding import Onboarding
from .settings import read_json, write_json
from .factory.control import controller_lock, ControllerBusy

SOURCE = Path(__file__).resolve().parent.parent
NODE_IMAGE = 'node:24-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6'
PYTHON_IMAGE = 'python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea'
SECCOMP_URL = 'https://raw.githubusercontent.com/moby/profiles/3c28324314729dbade8287e868eef6338c42807a/seccomp/default.json'
SECCOMP_SHA = '536529b665dd0972c37bfb569f5d4ac8a53592e7b00752bc39ff063ca9864c74'
APPARMOR_PROFILE = 'forge-runner-private-proc'
APPARMOR_TARGET = Path('/etc/apparmor.d') / APPARMOR_PROFILE
UI_PORT, API_PORT = 8787, 8788
# Everything the runner Dockerfile can see (see .dockerignore). Over-inclusion
# only costs a rebuild; the image is tagged by this digest, so an unchanged
# source never rebuilds and never needs requalification.
RUNNER_INPUTS = ('deploy/runner/Dockerfile', '.dockerignore', 'forge/harnesses', 'forge/factory/job.py',
                 'forge/factory/harness.py', 'forge/factory/probe.py', 'forge/factory/build_environment.py',
                 'deploy/runner/browser-canary.cjs')
UI_SKIP = {'node_modules', '.output', '.nuxt', '.data', '.npm', 'tests'}
SERVICES = ('forge-status', 'forge-ui', 'forge-supervisor')
RECORD = 'host.json'
# A drop-in that sets any of these makes systemd run something other than the
# unit update writes, silently undoing it. Other drop-ins (NITRO_ALLOWED_HOSTS,
# resource limits) are the operator's and are kept.
OVERRIDING = re.compile(r'^\s*(ExecStart|ExecStartPre|WorkingDirectory)\s*=|^\s*Environment\s*=.*?(?<![A-Z_])'
                        r'(PYTHONPATH|FORGE_ROOT|FORGE_BIND_ADDRESS|FORGE_PORT|HOST|PORT|NITRO_FORGE_API)=', re.M)


def command(argv, **kwargs):
    return subprocess.run(argv, check=True, **kwargs)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def tree_digest(paths, *extra, skip=frozenset()):
    h = hashlib.sha256()
    for value in extra:
        h.update(value.encode() + b'\0')
    for relative in paths:
        path = SOURCE / relative
        files = [path] if path.is_file() else sorted(
            f for f in path.rglob('*') if f.is_file() and '__pycache__' not in f.parts
            and not skip.intersection(f.relative_to(path).parts))
        for file in files:
            h.update(str(file.relative_to(SOURCE)).encode() + b'\0' + file.read_bytes() + b'\0')
    return h.hexdigest()


def source_revision():
    result = subprocess.run(['git', '-C', str(SOURCE), 'rev-parse', 'HEAD'], capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else None


def pull():
    """Fast-forward the checkout to its upstream. Returns True when it moved."""
    git = ['git', '-C', str(SOURCE)]
    if not (SOURCE / '.git').exists():
        raise ValueError('This source is not a Git checkout; update it yourself, then run bin/forge update --no-pull')
    if command(git + ['status', '--porcelain', '--untracked-files=no'], capture_output=True, text=True).stdout.strip():
        raise ValueError('The Forge checkout has local changes. Commit or discard them, '
                         'or apply the checkout as it is with bin/forge update --no-pull')
    command(git + ['fetch', '--quiet'])
    head = command(git + ['rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
    upstream = subprocess.run(git + ['rev-parse', '@{upstream}'], capture_output=True, text=True)
    if upstream.returncode != 0:
        raise ValueError('The Forge checkout has no upstream branch; update it yourself, then run bin/forge update --no-pull')
    if upstream.stdout.strip() == head:
        return False
    if subprocess.run(git + ['merge-base', '--is-ancestor', 'HEAD', '@{upstream}']).returncode != 0:
        raise ValueError('The Forge checkout has diverged from its upstream. Reconcile it, then run bin/forge update --no-pull')
    command(git + ['merge', '--ff-only', '--quiet', '@{upstream}'])
    return True


def check_bind(bind):
    address = ipaddress.ip_address(bind)
    if not address.is_loopback and not (address.version == 4 and address in ipaddress.ip_network('100.64.0.0/10')):
        raise ValueError('Bind the unauthenticated operator UI to loopback or a private tailnet address')
    return bind


def resolve_bind(root, requested, units):
    """Explicit choice, then the recorded one, then what an older install bound."""
    if requested:
        return check_bind(requested)
    recorded = Record(root).data.get('bind')
    if recorded:
        return recorded
    # Before the UI moved in front, forge-status itself served the operator address.
    old = units / 'forge-status.service'
    if old.exists():
        for line in old.read_text().splitlines():
            if 'FORGE_BIND_ADDRESS=' in line:
                value = line.split('FORGE_BIND_ADDRESS=', 1)[1].strip().strip('"')
                if value and value != '127.0.0.1':
                    return check_bind(value)
    return '127.0.0.1'


class Record:
    """What this reconciliation installed: digests of managed files and the
    releases in use. Saved after every managed write so an interrupted update
    never mistakes its own output for an operator's hand edit."""

    def __init__(self, root):
        self.path = Path(root) / RECORD
        self.data = read_json(self.path) if self.path.exists() else {}
        self.data.setdefault('managed', {})

    def save(self, **values):
        self.data.update(values)
        write_json(self.path, self.data)

    def managed(self, path, content):
        self.data['managed'][str(path)] = digest_bytes(content)
        self.save()

    def owns(self, path, current):
        return self.data['managed'].get(str(path)) == digest_bytes(current)


def unit_quote(value):
    return json.dumps(str(value).replace('%', '%%'))


def unit(description, execution, environment=(), *, after='network-online.target', extra=''):
    return ('[Unit]\nDescription=' + description + '\nAfter=' + after + '\n\n[Service]\nType=simple\n' + extra
            + 'ExecStart=' + execution + '\n'
            + ''.join('Environment=' + unit_quote(value) + '\n' for value in environment)
            + 'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nUMask=0077\n\n[Install]\nWantedBy=default.target\n')


def ui_container(root):
    return 'forge-ui-' + hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:12]


def desired_units(root, bind):
    root = Path(root)
    python = ['PYTHONPATH=' + str(SOURCE), 'FORGE_ROOT=' + str(root)]
    docker = shutil.which('docker') or '/usr/bin/docker'
    name = ui_container(root)
    # The UI runs from a pinned Node image, so the host needs no Node toolchain.
    # Host networking lets it bind the operator address and reach the loopback API.
    ui = [docker, 'run', '--rm', '--name', name, '--network', 'host', '--read-only',
          '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', f'{os.getuid()}:{os.getgid()}',
          '--memory', '512m', '--pids-limit', '256', '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m',
          '--mount', 'type=bind,source=' + str(root / 'ui/current') + ',target=/app,readonly',
          '--env', 'NODE_ENV=production', '--env', 'HOST=' + bind, '--env', 'PORT=' + str(UI_PORT),
          '--env', f'NITRO_FORGE_API=http://127.0.0.1:{API_PORT}', '--env', 'FORGE_ROOT=/app',
          NODE_IMAGE, 'node', '/app/server/index.mjs']
    return {
        'forge-status': unit('Forge status API (loopback)', '/usr/bin/python3 ' + unit_quote(SOURCE / 'legacy/forge-status.py'),
                             python + ['FORGE_BIND_ADDRESS=127.0.0.1', 'FORGE_PORT=' + str(API_PORT)]),
        'forge-ui': unit('Forge operator UI', ' '.join(unit_quote(part) for part in ui),
                         after='network-online.target forge-status.service',
                         extra='ExecStartPre=-' + docker + ' rm --force ' + name + '\n'
                               'ExecStop=' + docker + ' stop --time 10 ' + name + '\n'),
        'forge-supervisor': unit('Forge supervisor', unit_quote(SOURCE / 'bin/forge') + ' service --root ' + unit_quote(root), python),
    }


def legacy_unit(name, root, bind):
    """The units `bin/forge setup` wrote before this reconciler; recognised as ours."""
    commands = {'forge-status': '/usr/bin/python3 ' + unit_quote(SOURCE / 'legacy/forge-status.py'),
                'forge-supervisor': unit_quote(SOURCE / 'bin/forge') + ' service --root ' + unit_quote(root)}
    if name not in commands:
        return None
    return ('[Unit]\nDescription=Forge ' + name + '\nAfter=network-online.target\n\n[Service]\n'
            'Type=simple\nExecStart=' + commands[name] + '\n'
            'Environment=' + unit_quote('PYTHONPATH=' + str(SOURCE)) + '\n'
            'Environment=' + unit_quote('FORGE_ROOT=' + str(root)) + '\n'
            'Environment=' + unit_quote('FORGE_BIND_ADDRESS=' + bind) + '\n'
            'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\nUMask=0077\n\n'
            '[Install]\nWantedBy=default.target\n')


def runner_image():
    tag = 'forge-runner:' + tree_digest(RUNNER_INPUTS, NODE_IMAGE, PYTHON_IMAGE)[:16]
    inspect = ['docker', 'image', 'inspect', tag, '--format', '{{.Id}}']
    found = subprocess.run(inspect, capture_output=True, text=True)
    if found.returncode != 0:
        print('Building the runner image…', flush=True)
        command(['docker', 'build', '--build-arg', 'NODE_IMAGE=' + NODE_IMAGE, '--build-arg', 'PYTHON_IMAGE=' + PYTHON_IMAGE,
                 '-f', str(SOURCE / 'deploy/runner/Dockerfile'), '-t', tag, str(SOURCE)])
        found = command(inspect, capture_output=True, text=True)
    return found.stdout.strip()


def seccomp_policy(root, record):
    """Returns the new policy path, or None when the installed one is current."""
    inputs = digest_bytes((SECCOMP_SHA + (SOURCE / 'deploy/runner/seccomp_profile.py').read_text()).encode())
    installed = Path(root) / 'runner-seccomp.json'
    if installed.exists() and record.data.get('seccomp_inputs') == inputs:
        return None, inputs
    candidate = Path(root) / 'runner-seccomp.json.new'
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary) / 'seccomp.json'
        with urllib.request.urlopen(SECCOMP_URL, timeout=30) as response:
            source.write_bytes(response.read(1024 * 1024))
        generate = runpy.run_path(str(SOURCE / 'deploy/runner/seccomp_profile.py'))['generate']
        write_json(candidate, generate(source, SECCOMP_SHA))
    if installed.exists() and installed.read_bytes() == candidate.read_bytes():
        candidate.unlink()
        return None, inputs
    return candidate, inputs


def ui_release(root):
    """Build the operator UI in a pinned Node container; releases are content-addressed."""
    root = Path(root)
    guides = sorted(str(path.relative_to(SOURCE)) for path in (SOURCE / 'docs').glob('*.md'))
    release = tree_digest(('ui', *guides), NODE_IMAGE, skip=UI_SKIP)[:16]
    target = root / 'ui/releases' / release
    if (target / 'server/index.mjs').exists():
        return release
    print('Building the operator UI…', flush=True)
    build = root / 'ui/build'
    build.mkdir(parents=True, exist_ok=True)
    # Keep the npm cache between builds; everything else mirrors the source.
    for child in build.iterdir():
        if child.name != '.npm':
            shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
    for child in (SOURCE / 'ui').iterdir():
        if child.name in UI_SKIP:
            continue
        if child.is_dir():
            shutil.copytree(child, build / child.name, ignore=shutil.ignore_patterns(*UI_SKIP))
        else:
            shutil.copy2(child, build / child.name)
    command(['docker', 'run', '--rm', '--user', f'{os.getuid()}:{os.getgid()}', '--cap-drop', 'ALL',
             '--security-opt', 'no-new-privileges', '--mount', 'type=bind,source=' + str(build) + ',target=/ui',
             '--workdir', '/ui', '--env', 'HOME=/ui/.npm', '--env', 'npm_config_cache=/ui/.npm',
             '--env', 'NUXT_TELEMETRY_DISABLED=1', NODE_IMAGE,
             'sh', '-c', 'npm ci --no-audit --no-fund && npm run build'])
    staging = target.with_name(release + '.tmp')
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(build / '.output', staging)
    # The Help pages read <FORGE_ROOT>/docs/<guide>.md; the container sees only the release.
    (staging / 'docs').mkdir()
    for guide in guides:
        shutil.copy2(SOURCE / guide, staging / guide)
    os.replace(staging, target)
    return release


def switch_ui(root, release):
    root = Path(root)
    link, temporary = root / 'ui/current', root / 'ui/current.new'
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(Path('releases') / release)
    os.replace(temporary, link)


def prune_ui(root, keep):
    releases = Path(root) / 'ui/releases'
    for child in releases.iterdir() if releases.exists() else ():
        if child.name not in keep:
            shutil.rmtree(child, ignore_errors=True)


def switch_runtime(root, image, profile):
    """Point every runtime configuration at the qualified image, between supervisor cycles."""
    try:
        with controller_lock(root, timeout=120):
            for name in ('host-runtime.json', 'setup/runtime.json', 'runtime.json'):
                path = Path(root) / name
                if path.exists():
                    config = read_json(path)
                    if config.get('image') != image or config.get('apparmor_profile') != profile:
                        config.update(image=image, apparmor_profile=profile)
                        write_json(path, config)
    except ControllerBusy:
        raise ValueError('The supervisor stayed busy for two minutes; nothing was switched. Rerun bin/forge update.') from None


def reachable(url):
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            return response.status < 500
    except urllib.error.HTTPError as error:
        return error.code < 500
    except (OSError, ValueError):
        return False


def wait_healthy(bind, timeout=90):
    host = '[' + bind + ']' if ':' in bind else bind
    checks = {'forge-status': f'http://127.0.0.1:{API_PORT}/api/onboarding',
              'forge-ui': f'http://{host}:{UI_PORT}/'}
    deadline = time.monotonic() + timeout
    pending = dict(checks)
    while pending and time.monotonic() < deadline:
        pending = {name: url for name, url in pending.items() if not reachable(url)}
        if pending:
            time.sleep(2)
    if pending:
        raise ValueError(', '.join(pending) + ' did not answer after the update; inspect it with '
                         'journalctl --user -u ' + ' -u '.join(pending))


def dropins(units):
    """(service, path, overriding) for each drop-in on a service this module manages."""
    for name in SERVICES:
        for path in sorted((units / (name + '.service.d')).glob('*.conf')):
            yield name, path, bool(OVERRIDING.search(path.read_text()))


def confirm(changes, assume_yes, overrides=()):
    if not changes and not overrides:
        return
    for path, current, desired in changes:
        print('\n' + str(path) + ' was changed outside Forge and will be replaced:')
        sys.stdout.writelines(difflib.unified_diff(current.splitlines(True), desired.splitlines(True), 'installed', 'update'))
    for _, path, _ in overrides:
        print('\n' + str(path) + ' overrides what this service runs and will be moved to the backups directory:')
        print(path.read_text().rstrip())
    if assume_yes:
        return
    if not sys.stdin.isatty():
        raise ValueError('Files changed outside Forge would be replaced; review the differences above and rerun with --yes')
    if input('\nReplace them? [y/N] ').strip().lower() not in ('y', 'yes'):
        raise ValueError('Update cancelled; nothing was replaced')


def qualify_runner(image, seccomp, profile):
    """Offline canary: tools, memory limits, browser and credential protection in the real sandbox."""
    runpy.run_path(str(SOURCE / 'deploy/test_build_environment.py'))['validate'](image, seccomp, profile)


def reconcile(root, bind=None, assume_yes=False):
    """Install or update the host. Returns True when running workers need requalification."""
    root = Path(root)
    fresh = not (root / 'host-runtime.json').exists()
    record = Record(root)
    units = Path.home() / '.config/systemd/user'
    bind = resolve_bind(root, bind, units)
    source = source_revision()
    plan = []

    # 1. Build and qualify everything new. Nothing in use changes here.
    image = runner_image()
    seccomp, seccomp_inputs = seccomp_policy(root, record)
    apparmor = (SOURCE / 'deploy/runner' / (APPARMOR_PROFILE + '.apparmor')).read_bytes()
    installed_apparmor = APPARMOR_TARGET.read_bytes() if APPARMOR_TARGET.exists() else None
    current_runtime = read_json(root / 'host-runtime.json') if not fresh else {}
    # The recorded image covers an update interrupted after switching but before
    # it finished: the rerun still requalifies.
    runtime_changed = (fresh or seccomp is not None or installed_apparmor != apparmor
                       or current_runtime.get('image') != image or record.data.get('image') not in (None, image))
    release = ui_release(root)
    wanted = desired_units(root, bind)
    foreign, unit_changes = [], []
    for name, content in wanted.items():
        path = units / (name + '.service')
        current = path.read_text() if path.exists() else None
        if current == content:
            continue
        unit_changes.append(name)
        if current is not None and not record.owns(path, current.encode()) and current != legacy_unit(name, root, bind):
            foreign.append((path, current, content))
    if installed_apparmor is not None and installed_apparmor != apparmor and not record.owns(APPARMOR_TARGET, installed_apparmor):
        foreign.append((APPARMOR_TARGET, installed_apparmor.decode(errors='replace'), apparmor.decode()))
    overrides, kept = [], []
    for name, path, overriding in dropins(units):
        (overrides if overriding else kept).append((name, path, overriding))
    for _, path, _ in kept:
        print('Keeping drop-in ' + str(path) + ' (it does not change what the service runs).')
    restart = set(unit_changes) | {name for name, _, _ in overrides}
    if source != record.data.get('source'):
        restart.update(('forge-status', 'forge-supervisor'))  # Python services run this checkout.
    if release != record.data.get('ui_release'):
        restart.add('forge-ui')
    if runtime_changed and not fresh:
        plan.append('runner image ' + (current_runtime.get('image') or 'none')[:19] + ' → ' + image[:19])
    if seccomp is not None and not fresh:
        plan.append('runner seccomp policy')
    if installed_apparmor != apparmor and not fresh:
        plan.append('runner AppArmor policy')
    if release != record.data.get('ui_release'):
        plan.append('operator UI release ' + release)
    plan += ['service ' + name for name in unit_changes]
    plan += ['remove overriding drop-in ' + path.name + ' from ' + name for name, path, _ in overrides]
    if not fresh and not plan and not restart:
        print('Forge host is up to date' + (' at ' + source[:12] if source else '') + '.')
        return False
    if not fresh:
        print('Updating: ' + ('; '.join(plan) if plan else 'restart services for source ' + (source or 'unknown')[:12]))
    confirm(foreign, assume_yes, overrides)
    if installed_apparmor != apparmor:
        # The canary runs under the loaded policy, so it is installed first.
        with tempfile.NamedTemporaryFile(dir=root, delete=False) as stream:
            stream.write(apparmor)
        try:
            command(['sudo', 'install', '-m', '0644', stream.name, str(APPARMOR_TARGET)])
            command(['sudo', 'apparmor_parser', '-r', str(APPARMOR_TARGET)])
        finally:
            os.unlink(stream.name)
        record.managed(APPARMOR_TARGET, apparmor)
    if runtime_changed:
        # Image presence is not readiness: run the offline canary before switching.
        qualify_runner(image, seccomp or root / 'runner-seccomp.json', APPARMOR_PROFILE)

    # 2. Switch. Each step is idempotent; an interrupted update converges when rerun.
    if seccomp is not None:
        os.replace(seccomp, root / 'runner-seccomp.json')
    record.save(seccomp_inputs=seccomp_inputs)
    network = root / 'deploy/runner'
    network.mkdir(parents=True, exist_ok=True)
    for name in ('compose.yaml', 'egress.py'):
        shutil.copyfile(SOURCE / 'deploy/runner' / name, network / name)
    command(['docker', 'compose', '-p', 'forge', '-f', str(network / 'compose.yaml'), 'up', '-d', 'egress'],
            env={**os.environ, 'FORGE_EGRESS_IMAGE': PYTHON_IMAGE})
    if not fresh and runtime_changed:
        switch_runtime(root, image, APPARMOR_PROFILE)
    switch_ui(root, release)
    units.mkdir(parents=True, exist_ok=True)
    for name in unit_changes:
        path = units / (name + '.service')
        path.write_text(wanted[name])
        record.managed(path, wanted[name].encode())
    if overrides:
        backup = root / 'backups' / time.strftime('unit-dropins-%Y%m%dT%H%M%SZ', time.gmtime())
        for name, path, _ in overrides:
            target = backup / path.parent.name
            target.mkdir(parents=True, exist_ok=True)
            shutil.move(str(path), target / path.name)
        print('Moved overriding drop-ins to ' + str(backup))
    command(['systemctl', '--user', 'daemon-reload'])
    command(['systemctl', '--user', 'enable', *[name + '.service' for name in SERVICES]], stdout=subprocess.DEVNULL)
    # The API first: the UI and supervisor depend on it.
    ordered = [name + '.service' for name in SERVICES if fresh or name in restart]
    if ordered:
        command(['systemctl', '--user', 'restart', *ordered])
    wait_healthy(bind)
    prune_ui(root, {release, record.data.get('ui_release')})
    record.save(bind=bind, source=source, image=image, ui_release=release, updated_at=time.time(),
                previous={key: record.data.get(key) for key in ('source', 'image', 'ui_release')} if not fresh else None)
    if fresh:
        # Written last: a failed installation can be repeated without a false success marker.
        config = Onboarding(root).runtime()
        config.update(image=image, apparmor_profile=APPARMOR_PROFILE)
        write_json(root / 'host-runtime.json', config)
    elif record.data['previous'].get('source') not in (None, source):
        print('Updated to ' + (source or 'unknown')[:12] + '. To roll back: git checkout '
              + record.data['previous']['source'][:12] + ' && bin/forge update --no-pull')
    return runtime_changed and not fresh
