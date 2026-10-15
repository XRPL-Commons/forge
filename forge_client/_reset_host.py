"""Pinned, restartable archive of an installer-owned Forge instance.

This retires Forge's account and installation. It never removes the OS, SSH,
Tailscale, Docker images, GitHub objects, or the root-only diagnostic archives.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import sys
import tempfile

if globals().get('__package__'):
    from ._install_host import (ENV, HOME, ROOT, SOURCE, STATE, REPOSITORY, InstallError,
        need, read, validate_intent, service, require_setup_entrypoint, private_directory, run)
else:
    from forge_install_helpers import (ENV, HOME, ROOT, SOURCE, STATE, REPOSITORY, InstallError,
        need, read, validate_intent, service, require_setup_entrypoint, private_directory, run)

ARCHIVES = Path('/var/lib/forge-reset')
SERVICES = ['forge-mirror.service', 'forge-status.service', 'forge-ui.service', 'forge-supervisor.service']
# Only these package defaults may remain active while retiring the dedicated
# account. Other inactive package services are checked for an empty cgroup.
SYSTEM_UNITS = frozenset({
    'dbus.service', 'dbus.socket', 'dbus-broker.service',
    'systemd-tmpfiles-setup.service', 'systemd-tmpfiles-clean.service', 'systemd-tmpfiles-clean.timer',
    'launchpadlib-cache-clean.timer',
    'dirmngr.socket', 'gpg-agent.socket', 'gpg-agent-browser.socket', 'gpg-agent-extra.socket',
    'gpg-agent-ssh.socket', 'keyboxd.socket', 'pk-debconf-helper.socket', 'snapd.session-agent.socket',
})


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def file_digest(path):
    need(path.is_file() and not path.is_symlink(), 'unsafe_install_state')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_receipt(directory):
    if not directory.exists():
        need(not directory.is_symlink(), 'unsafe_install_state')
        return None
    private_directory(ARCHIVES)
    private_directory(directory)
    path = directory / 'receipt.json'
    if not path.exists():
        need(not path.is_symlink() and all(p.name.startswith('.pending-') and p.is_file() and not p.is_symlink()
             for p in directory.iterdir()), 'unsafe_install_state')
        return None
    need(not path.is_symlink() and path.is_file() and path.stat().st_size <= 65536, 'unsafe_install_state')
    return json.loads(path.read_text())


def write_receipt(directory, value):
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='.pending-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, allow_nan=False, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, directory / 'receipt.json')
        descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def snapshot(*, instance=None, generation=None):
    require_setup_entrypoint(SOURCE, 'forge.install_reset')
    command = ['/usr/bin/env', '-C', str(SOURCE), '/usr/bin/python3', '-m', 'forge.launch', 'forge.install_reset', '--root', str(ROOT)]
    if instance is not None:
        command += ['--instance', instance, '--generation', generation]
    raw = service(command, capture=True)
    need(len(raw) <= 16384, 'factory_setup_failed')
    return json.loads(raw)


def account():
    found = pwd.getpwnam('forge')
    need(found.pw_uid > 0 and found.pw_dir == str(HOME) and HOME.resolve() == HOME
         and HOME.is_dir() and HOME.stat().st_uid == found.pw_uid, 'existing_installation')
    # Reset must not terminate its own SSH login account.
    need(os.environ.get('SUDO_USER') != 'forge', 'administrator_required')
    return found


def paths(account_info):
    need(SOURCE.resolve() == SOURCE and SOURCE.is_dir() and SOURCE.stat().st_uid == account_info.pw_uid,
         'source_conflict')
    need(ROOT.resolve() == ROOT and ROOT.is_dir() and ROOT.stat().st_uid == account_info.pw_uid,
         'unsafe_install_state')
    marker = ROOT / 'state/storage'
    lines = marker.read_text().splitlines()
    need(len(lines) == 3 and lines[0] == 'forge-sqlite-v1'
         and Path(lines[1]).resolve().is_relative_to(HOME), 'unsafe_install_state')
    # Archival cannot relocate mounted data or a symlinked home tree.
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount = Path(line.split()[4].replace('\\040', ' ').replace('\\134', '\\'))
        need(not mount.is_relative_to(HOME), 'unsafe_install_state')
    return {'home_device': HOME.stat().st_dev, 'home_inode': HOME.stat().st_ino,
            'state_device': STATE.stat().st_dev, 'state_inode': STATE.stat().st_ino,
            'source_device': SOURCE.stat().st_dev, 'source_inode': SOURCE.stat().st_ino}


def unit_inventory(view):
    units = HOME / '.config/systemd/user'
    need(units.resolve() == units, 'unsafe_install_state')
    for path, expected in view['units'].items():
        need(Path(path).parent == units and Path(path).name in SERVICES
             and file_digest(Path(path)) == expected, 'source_conflict')
    need(set(view['units']) == {str(units / name) for name in SERVICES}, 'unsafe_install_state')
    # A dedicated service account may contain only these managed user services
    # and their enablement links. Preserve unrelated services by refusing reset.
    for path in units.iterdir():
        need(path.name in SERVICES or path.name == 'default.target.wants', 'unsafe_install_state')
        if path.name == 'default.target.wants':
            need(path.is_dir() and not path.is_symlink(), 'unsafe_install_state')
            for link in path.iterdir():
                need(link.name in SERVICES and link.is_symlink() and link.resolve() == units / link.name,
                     'unsafe_install_state')


def plan(intent):
    installed = read()
    need(installed and installed['intent'] == intent, 'installation_conflict')
    need(installed['step'] == 'complete', 'installation_required')
    owner = account()
    scope = paths(owner)
    git = ['git', '-C', str(SOURCE)]
    need(service(git + ['remote', 'get-url', 'origin'], capture=True) == REPOSITORY, 'source_conflict')
    need(not service(git + ['status', '--porcelain', '--untracked-files=no'], capture=True), 'source_conflict')
    view = snapshot()
    unit_inventory(view)
    user_units_clear(intent, owner.pw_uid)
    need(isinstance(view.get('instance'), str) and isinstance(view.get('generation'), str), 'unsafe_install_state')
    target = ARCHIVES / intent['id']
    home_archive = HOME.parent / ('.forge-reset-' + intent['id'])
    need(read_receipt(target) is None and not target.is_symlink() and not home_archive.exists() and not home_archive.is_symlink(),
         'installation_conflict')
    identity = {'intent': intent, 'instance': view['instance'], 'generation': view['generation'],
                'source_revision': view['source_revision'], 'setup_revision': view['setup_revision'],
                'setup_fingerprint': view['setup_fingerprint'], 'uid': owner.pw_uid, 'gid': owner.pw_gid,
                'paths': scope, 'units': view['units']}
    return {'status': 'reset_planned', 'revision': digest(identity), 'identity': identity,
            'archive': {'home': str(home_archive / 'home'), 'installer': str(target / 'installer')},
            'effects': ['Drain and fence Forge workers; reconcile pending GitHub effects.',
                        'Stop the four owned user services and remove the Forge service account.',
                        'Remove the verified Forge Compose egress container by its recorded ID.',
                        'Archive its source, runtime, history and remaining credentials in root-only directories.',
                        'Archive the host installer state and matching local setup journals.'],
            'preserved': ['SSH access and host keys', 'Ubuntu and installed packages', 'Tailscale identity and Serve',
                          'Docker images', 'GitHub repositories, Apps, branches, issues and pull requests'],
            'pending_effects': view['pending_effects'], 'leases': view['leases']}


def containers():
    ids = run(['docker', 'container', 'ls', '-aq', '--no-trunc'], capture=True).splitlines()
    need(all(re.fullmatch('[a-f0-9]{64}', value) for value in ids), 'unsafe_install_state')
    if not ids:
        return []
    observed = json.loads(run(['docker', 'container', 'inspect', *ids], capture=True))
    need(isinstance(observed, list) and {row.get('Id') for row in observed} == set(ids), 'unsafe_install_state')
    return observed


def egress_candidates(observed):
    result = []
    for container in observed:
        labels = container.get('Config', {}).get('Labels') or {}
        if (container.get('Name') == '/forge-egress-1'
                or labels.get('com.docker.compose.project') == 'forge' and labels.get('com.docker.compose.service') == 'egress'
                or any(mount.get('Source') == str(ROOT / 'deploy/runner/egress.py') for mount in container.get('Mounts', []))):
            result.append(container)
    return result


def egress_identity(container, receipt):
    network = ROOT / 'deploy/runner'
    labels = container.get('Config', {}).get('Labels') or {}
    expected = {'project': 'forge', 'service': 'egress', 'project.working_dir': str(network),
                'project.config_files': str(network / 'compose.yaml')}
    need(container.get('Name') == '/forge-egress-1'
         and all(labels.get('com.docker.compose.' + key) == value for key, value in expected.items()), 'foreign_egress')
    mounts = container.get('Mounts', [])
    need(len(mounts) == 1 and mounts[0].get('Type') == 'bind' and mounts[0].get('RW') is False
         and mounts[0].get('Source') == str(network / 'egress.py')
         and mounts[0].get('Destination') == '/opt/forge/egress.py', 'foreign_egress')
    files = {}
    for name in ('compose.yaml', 'egress.py'):
        path = network / name
        need(path.resolve() == path and path.stat().st_uid == receipt['plan']['identity']['uid'], 'foreign_egress')
        files[name] = file_digest(path)
        need(files[name] == file_digest(SOURCE / 'deploy/runner' / name), 'foreign_egress')
    return {'id': container['Id'], 'name': container['Name'], 'compose': expected,
            'mount': {key: mounts[0][key] for key in ('Type', 'Source', 'Destination', 'RW')}, 'files': files}


def cleanup_egress(directory, receipt):
    """Reconcile only the observed, installer-owned proxy; never a Compose project."""
    found = egress_candidates(containers())
    need(len(found) <= 1, 'foreign_egress')
    saved = receipt.get('egress_cleanup')
    if found:
        identity = egress_identity(found[0], receipt)
        if saved is None:
            saved = {'status': 'pending', 'identity': identity}
            receipt['egress_cleanup'] = saved
            write_receipt(directory, receipt)
        need(saved['status'] == 'pending' and saved.get('identity') == identity, 'egress_changed')
        # Persisted identity survives a lost Docker response. Retry observes absence
        # before deciding whether this exact container still needs removal.
        run(['docker', 'container', 'rm', '--force', identity['id']])
        need(not egress_candidates(containers()), 'egress_changed')
    receipt['egress_cleanup'] = {**(saved or {}), 'status': 'removed' if saved and saved.get('identity') else 'absent'}
    write_receipt(directory, receipt)


def docker_mounts_clear():
    for container in containers():
        for mount in container.get('Mounts', []):
            source = mount.get('Source', '')
            need(not source or not Path(source).is_relative_to(HOME), 'unsafe_install_state')
        # Owned job/login cleanup must have completed before archiving data.
        labels = container.get('Config', {}).get('Labels') or {}
        need(not labels.get('forge.execution') and not labels.get('forge.login-root'), 'unsafe_install_state')
        need(not container.get('Name', '').startswith('/forge-build-canary-'), 'unsafe_install_state')


def user_units_clear(intent, uid):
    """Never terminate an unrelated user-manager workload to delete the account."""
    raw = service(['systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend',
                   '--type=service,scope,socket,timer,path'], capture=True)
    environment = 'forge-install-environment-' + intent['id'] + '.service'
    for line in raw.splitlines():
        parts = line.split(None, 4)
        need(len(parts) >= 4, 'unsafe_user_units')
        name, _, active, substate = parts[:4]
        # Devices, mounts, slices and targets reflected from the host do not
        # describe executable workloads owned by this user manager.
        if name.rsplit('.', 1)[-1] not in ('service', 'scope', 'socket', 'timer', 'path'):
            continue
        inactive_service = name.endswith('.service') and active == 'inactive' and substate == 'dead'
        need(name in SYSTEM_UNITS or name in SERVICES or name in ('init.scope', environment)
             or inactive_service, 'unrelated_user_unit')
        if name == 'init.scope':
            continue  # systemd's own manager scope; it has no unit file.
        properties = ['FragmentPath', 'DropInPaths', 'Transient']
        if inactive_service:
            properties.append('ControlGroup')
        observed = service(['systemctl', '--user', 'show', name,
                            '--property=' + ','.join(properties)], capture=True)
        values = dict(row.split('=', 1) for row in observed.splitlines() if '=' in row)
        need(set(values) == set(properties) and not values['DropInPaths']
             and (not inactive_service or not values['ControlGroup']), 'unsafe_user_units')
        path = Path(values['FragmentPath'])
        if name in SERVICES:
            need(values['Transient'] == 'no' and path == HOME / '.config/systemd/user' / name, 'unsafe_user_units')
        elif name == environment:
            need(values['Transient'] == 'yes' and path == Path('/run/user') / str(uid) / 'systemd/transient' / name,
                 'unsafe_user_units')
        else:
            resolved = path.resolve()
            need(values['Transient'] == 'no' and path.is_file()
                 and (resolved.name in SYSTEM_UNITS or inactive_service and resolved.suffix == '.service')
                 and resolved.parent == Path('/usr/lib/systemd/user')
                 and path.stat().st_uid == 0 and path.stat().st_mode & 0o022 == 0, 'unsafe_user_units')


def drained_identity(view):
    need(view.get('phase') == 'setup' and type(view.get('setup_revision')) is int
         and isinstance(view.get('setup_fingerprint'), str) and re.fullmatch('[a-f0-9]{64}', view['setup_fingerprint'])
         and view.get('reset_receipt') and view['pending_effects'] == 0 and view['leases'] == 0,
         'installation_conflict')
    return {key: view[key] for key in ('instance', 'generation', 'phase', 'source_revision',
                                     'setup_revision', 'setup_fingerprint', 'reset_receipt')}


def confirm_drained(receipt):
    need(receipt.get('drained') is not None and drained_identity(snapshot()) == receipt['drained'],
         'installation_conflict')


def retire(directory, receipt):
    identity, intent = receipt['plan']['identity'], receipt['intent']
    home_archive = Path(receipt['plan']['archive']['home']).parent
    if receipt['step'] == 'drain':
        unit_inventory({'units': identity['units']})
        service(['systemctl', '--user', 'start', 'forge-supervisor.service'])
        view = snapshot(instance=identity['instance'], generation=identity['generation'])
        need(view['source_revision'] == identity['source_revision'], 'source_conflict')
        finished = (view.get('reset_receipt') or {}).get('generation') == identity['generation']
        if not finished or view['pending_effects'] or view['leases']:
            receipt['waiting'] = view.get('error') or 'The supervisor is draining owned work and reconciling effects.'
            write_receipt(directory, receipt)
            return result(receipt)
        need(view['instance'] == identity['instance'], 'installation_conflict')
        receipt.update(step='stop', waiting=None, drained=drained_identity(view))
        write_receipt(directory, receipt)
    if receipt['step'] == 'stop':
        owner = account()
        need(owner.pw_uid == identity['uid'] and paths(owner) == identity['paths'], 'installation_conflict')
        unit_inventory({'units': identity['units']})
        confirm_drained(receipt)
        user_units_clear(intent, identity['uid'])
        service(['systemctl', '--user', 'stop', *SERVICES])
        # A qualified environment worker is installation-owned and must finish
        # or stop before source/runtime is archived.
        environment_unit = 'forge-install-environment-' + intent['id']
        active = service(['systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend', environment_unit + '.service'], capture=True)
        if active:
            service(['systemctl', '--user', 'stop', environment_unit])
        cleanup_egress(directory, receipt)
        docker_mounts_clear()
        # Re-read after all owned services stop: a concurrent operator may have
        # changed setup between the earlier observation and the stop request.
        confirm_drained(receipt)
        user_units_clear(intent, identity['uid'])
        receipt['step'] = 'manager'
        write_receipt(directory, receipt)
    if receipt['step'] == 'manager':
        # Persist this boundary before stopping the bus needed by user commands.
        # Snapshot itself only reads SQLite and works even after the manager stops.
        confirm_drained(receipt)
        manager = 'user@' + str(identity['uid']) + '.service'
        state = run(['systemctl', 'show', manager, '--property=ActiveState', '--value'], capture=True)
        need(state in ('active', 'inactive'), 'user_manager_busy')
        if state == 'active':
            user_units_clear(intent, identity['uid'])
        run(['loginctl', 'disable-linger', 'forge'])
        run(['systemctl', 'stop', manager])
        receipt['step'] = 'account'
        write_receipt(directory, receipt)
    if receipt['step'] == 'account':
        try:
            owner = pwd.getpwnam('forge')
        except KeyError:
            owner = None
        if owner:
            need(owner.pw_uid == identity['uid'] and owner.pw_gid == identity['gid']
                 and owner.pw_dir == str(HOME), 'installation_conflict')
            # userdel refuses an account with remaining processes. No broad kill
            # or force option may erase unrelated activity on this account.
            run(['userdel', 'forge'])
        receipt['step'] = 'archive_home'
        write_receipt(directory, receipt)
    if receipt['step'] == 'archive_home':
        need(not egress_candidates(containers()), 'egress_changed')
        docker_mounts_clear()
        private_directory(home_archive, create=True)
        destination = home_archive / 'home'
        if HOME.exists():
            need(not destination.exists() and HOME.resolve() == HOME
                 and HOME.stat().st_dev == identity['paths']['home_device']
                 and HOME.stat().st_ino == identity['paths']['home_inode'], 'installation_conflict')
            os.rename(HOME, destination)
        need(not HOME.is_symlink() and destination.is_dir() and not destination.is_symlink()
             and destination.stat().st_dev == identity['paths']['home_device']
             and destination.stat().st_ino == identity['paths']['home_inode'], 'unsafe_install_state')
        receipt['step'] = 'archive_installer'
        write_receipt(directory, receipt)
    if receipt['step'] == 'archive_installer':
        destination = directory / 'installer'
        if STATE.exists():
            need(read()['intent'] == intent and not destination.exists()
                 and STATE.stat().st_dev == identity['paths']['state_device']
                 and STATE.stat().st_ino == identity['paths']['state_inode'], 'installation_conflict')
            os.rename(STATE, destination)
        need(not STATE.is_symlink() and destination.is_dir() and not destination.is_symlink()
             and destination.stat().st_dev == identity['paths']['state_device']
             and destination.stat().st_ino == identity['paths']['state_inode'], 'unsafe_install_state')
        receipt['step'] = 'complete'
        write_receipt(directory, receipt)
    return result(receipt)


def result(receipt):
    return {**receipt['plan'], 'status': 'reset_complete' if receipt['step'] == 'complete' else 'reset_pending',
            'step': receipt['step'], 'waiting': receipt.get('waiting')}


def dispatch(request):
    need(isinstance(request, dict) and set(request) == {'version', 'intent', 'apply', 'revision'}, 'invalid_request')
    need(request['version'] == 1 and type(request['apply']) is bool, 'invalid_request')
    intent = validate_intent(request['intent'])
    revision = request['revision']
    need(revision is None or isinstance(revision, str) and re.fullmatch('[a-f0-9]{64}', revision), 'invalid_request')
    directory = ARCHIVES / intent['id']
    existing = read_receipt(directory)
    if existing:
        need(existing['intent'] == intent and (not request['apply'] or existing['plan']['revision'] == revision),
             'installation_conflict')
        if not request['apply'] or existing['step'] == 'complete':
            return {'ok': True, 'result': result(existing)}
    elif not request['apply']:
        return {'ok': True, 'result': plan(intent)}
    # Serialize root teardown independently of the installer directory, which
    # moves during completion. A private archive is also its durable receipt.
    private_directory(ARCHIVES, create=True)
    with (ARCHIVES / 'reset.lock').open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing = read_receipt(directory)
        if existing is None:
            proposed = plan(intent)
            need(proposed['revision'] == revision, 'installation_conflict')
            private_directory(directory, create=True)
            existing = {'version': 1, 'intent': intent, 'plan': proposed, 'step': 'drain'}
            write_receipt(directory, existing)
        need(existing['plan']['revision'] == revision, 'installation_conflict')
        return {'ok': True, 'result': retire(directory, existing)}


def main(payload=None):
    try:
        raw = sys.stdin.buffer.read(4097)
        need(len(raw) <= 4096, 'invalid_request')
        print(json.dumps(dispatch(json.loads(raw)), allow_nan=False))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, InstallError) else 'factory_setup_failed'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
