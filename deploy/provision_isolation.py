#!/usr/bin/env python3
"""Add dedicated CLI identities and pinned executables. Dry-run unless --apply.

No authentication, global client settings, running workers, sudo memberships,
Docker access, or sysctl changes are installed by this program.
"""
import argparse
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import stat
import subprocess
import tempfile

BIN = Path('/opt/forge/bin')
HOMES = Path('/var/lib/forge-clients')
POLICY = Path('/etc/forge/isolation')
PROVIDERS = ('codex', 'claude', 'grok')
# Grok Build is an optional harness: it is provisioned only when a source is supplied.
OPTIONAL = {'grok'}


def run(*args):
    return subprocess.run(args, check=True, text=True, capture_output=True, timeout=120)


def source_default(provider):
    found = shutil.which(provider)
    if not found:
        return None
    path = Path(found).resolve()
    if provider == 'codex' and path.suffix == '.js':
        found = list(path.parent.parent.glob('node_modules/@openai/codex-*/vendor/*/bin/codex'))
        return str(found[0]) if len(found) == 1 else None
    return str(path)


def digest(path):
    value = hashlib.sha256()
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError(f'Not a regular file: {path}')
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(chunk)
    return value.hexdigest()


def profile(provider):
    return (f'# Managed by Forge additive isolation provisioning. Not a confinement policy.\n'
            '# Narrowly permits this pinned CLI to create its native sandbox.\n'
            '#include <tunables/global>\n'
            f'profile forge-{provider}-client {BIN / provider} flags=(unconfined) {{\n'
            '  userns,\n}\n')


def check_directory(path, uid=0, gid=0, mode=0o755, create=False):
    """Reject symlink/untrusted ancestry before root writes anything below it."""
    path = Path(path)
    for parent in reversed(path.parents):
        info = parent.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError(f'Unsafe root-owned ancestry: {parent}')
    if not path.exists() and not path.is_symlink():
        if create:
            path.mkdir(mode=mode)
            os.chown(path, uid, gid)
            os.chmod(path, mode)
        return
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (uid, gid, mode):
        raise ValueError(f'Existing directory has unexpected ownership/mode: {path}')


def check_identity(provider):
    name = 'forge-' + provider
    try:
        account = pwd.getpwnam(name)
    except KeyError:
        return None
    primary = grp.getgrnam(name)
    if (account.pw_dir != str(HOMES / provider) or account.pw_shell != '/usr/sbin/nologin'
            or account.pw_gid != primary.gr_gid or account.pw_uid == 0):
        raise ValueError(f'Existing account does not match isolated identity: {name}')
    if set(os.getgrouplist(name, account.pw_gid)) != {account.pw_gid}:
        raise ValueError(f'Existing account has supplementary groups: {name}')
    return account


def install_file(path, source, expected, group=0, mode=0o644):
    """Root stages bytes, verifies pin, then atomically publishes; no implicit upgrades."""
    path = Path(path)
    if path.exists() or path.is_symlink():
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, group, mode) or
                digest(path) != expected):
            raise ValueError(f'Existing target differs; explicit reviewed upgrade required: {path}')
        return
    fd, temporary = tempfile.mkstemp(prefix='.forge-install-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as output:
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            with os.fdopen(source_fd, 'rb') as incoming:
                shutil.copyfileobj(incoming, output, 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
        if digest(temporary) != expected:
            raise ValueError('Source changed or does not match reviewed SHA256')
        os.chown(temporary, 0, group)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def install_text(path, text):
    # The temporary source is never executed. install_file verifies exact bytes.
    with tempfile.NamedTemporaryFile(mode='w') as source:
        source.write(text)
        source.flush()
        install_file(path, source.name, hashlib.sha256(text.encode()).hexdigest())


def make_plan(args):
    clients = []
    skipped = {}
    for provider in PROVIDERS:
        source = getattr(args, provider + '_source', None)
        if not source:
            if provider in OPTIONAL:
                skipped[provider] = f'not provisioned; pass --{provider}-source to add this optional harness'
                continue
            raise ValueError(f'Provide --{provider}-source pointing to the installed native executable')
        path = Path(source).absolute()
        with path.open('rb') as stream:
            if stream.read(4) != b'\x7fELF':
                raise ValueError(f'{provider} source must be its native ELF executable, not a shell/Node launcher')
        actual = digest(path)
        supplied = getattr(args, provider + '_sha256', None)
        if args.apply and not supplied:
            raise ValueError(f'--apply requires the reviewed --{provider}-sha256')
        if supplied and (not re.fullmatch('[0-9a-f]{64}', supplied) or supplied != actual):
            raise ValueError(f'{provider} SHA256 pin mismatch')
        check_identity(provider)
        clients.append({'provider': provider, 'source': str(path), 'sha256': actual,
                        'target': str(BIN / provider), 'account': 'forge-' + provider,
                        'home': str(HOMES / provider), 'executable_mode': '0750 root:forge-' + provider,
                        'profile': '/etc/apparmor.d/forge-' + provider + '-client',
                        'profile_content': profile(provider)})
    return {'mode': 'apply' if args.apply else 'dry-run', 'clients': clients, 'skipped': skipped,
            'missing_packages': [p for p in ('bubblewrap', 'socat')
                                 if not shutil.which('bwrap' if p == 'bubblewrap' else p)],
            'auth_credentials': 'not copied', 'workers': 'not launched',
            'global_sysctl_and_client_config': 'unchanged'}


def apply(plan):
    if os.geteuid() != 0:
        raise ValueError('--apply requires root')
    if plan['missing_packages']:
        raise ValueError('Install prerequisites using the separate --install-packages step first')
    if not shutil.which('apparmor_parser'):
        raise ValueError('AppArmor parser is required; do not disable AppArmor or global userns restrictions')
    if Path('/sys/module/apparmor/parameters/enabled').read_text().strip() != 'Y':
        raise ValueError('AppArmor is not enabled')
    # Preflight syntax without changing kernel policy or cache. No package or
    # native CLI is ever executed as root by this provisioner.
    for client in plan['clients']:
        with tempfile.NamedTemporaryFile(mode='w') as candidate:
            candidate.write(client['profile_content'])
            candidate.flush()
            run('/usr/sbin/apparmor_parser', '--skip-kernel-load', '--skip-cache', candidate.name)
    for path, mode in ((Path('/opt/forge'), 0o755), (BIN, 0o711), (HOMES, 0o711),
                       (Path('/etc/forge'), 0o755), (POLICY, 0o755)):
        check_directory(path, mode=mode, create=True)
    check_directory('/etc/apparmor.d')
    for client in plan['clients']:
        name = client['account']
        try:
            group = grp.getgrnam(name)
            if group.gr_mem:
                raise ValueError(f'Existing provider group has unexpected members: {name}')
        except KeyError:
            run('/usr/sbin/groupadd', '--system', name)
            group = grp.getgrnam(name)
        account = check_identity(client['provider'])
        if account is None:
            run('/usr/sbin/useradd', '--system', '--no-create-home', '--no-user-group',
                '--gid', name, '--home-dir', client['home'], '--shell', '/usr/sbin/nologin', name)
            account = check_identity(client['provider'])
        check_directory(client['home'], uid=account.pw_uid, gid=group.gr_gid, mode=0o700, create=True)
        install_file(client['target'], client['source'], client['sha256'], group=group.gr_gid, mode=0o750)
        install_text(client['profile'], client['profile_content'])
        run('/usr/sbin/apparmor_parser', '--replace', client['profile'])
    receipt = dict(plan, mode='installed; isolation not yet proven')
    install_text(POLICY / 'manifest.json', json.dumps(receipt, indent=2, sort_keys=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true')
    mode.add_argument('--install-packages', action='store_true', help='Separate explicit root step: distro prerequisites only')
    for provider in PROVIDERS:
        parser.add_argument('--' + provider + '-source', default=source_default(provider))
        parser.add_argument('--' + provider + '-sha256')
    args = parser.parse_args()
    try:
        if args.install_packages:
            if os.geteuid() != 0:
                raise ValueError('--install-packages requires root')
            missing = [p for p in ('bubblewrap', 'socat') if not shutil.which('bwrap' if p == 'bubblewrap' else p)]
            if missing:
                subprocess.run(['/usr/bin/apt-get', 'install', '--yes', *missing], check=True)
            print(json.dumps({'installed_prerequisites': missing}))
            return 0
        plan = make_plan(args)
        print(json.dumps(plan, indent=2))
        if args.apply:
            apply(plan)
        return 0
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Provisioning stopped: {exc}\n')


if __name__ == '__main__':
    raise SystemExit(main())
