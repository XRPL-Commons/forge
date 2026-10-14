#!/bin/sh
# Install only the local CLI. No sudo, server setup, or shell-profile edits.
set -eu
command -v python3 >/dev/null 2>&1 || { echo 'Install Python 3.10+ first, then rerun this installer.' >&2; exit 1; }
command -v git >/dev/null 2>&1 || { echo 'Install Git first, then rerun this installer.' >&2; exit 1; }
exec python3 - "$@" <<'PY'
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

if sys.version_info < (3, 10):
    sys.exit('Install Python 3.10+ first, then rerun this installer.')
os.umask(0o077)
started = time.time()
repository = 'https://github.com/XRPL-Commons/forge.git'
install_root = Path.home() / '.local/share/forge-cli'
state_root = Path.home() / '.local/state/forge-client'
executable = Path.home() / '.local/bin/forge'

def private(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('Use private, user-owned Forge client directories (mode 0700).')

def write(path, value):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, sort_keys=True)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def command(argv):
    # Public source only. Do not copy operator credentials into subprocesses.
    environment = {key: value for key, value in os.environ.items()
                   if key in ('PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SYSTEMROOT')}
    environment.update(GIT_TERMINAL_PROMPT='0', PIP_CONFIG_FILE=os.devnull,
                       PIP_DISABLE_PIP_VERSION_CHECK='1', PIP_NO_INPUT='1')
    return subprocess.run(argv, check=True, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, env=environment, timeout=600).stdout.strip()

try:
    parser = argparse.ArgumentParser(description='Install the Forge CLI atomically; server setup is a separate step.')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--local', action='store_true', help='Install the clean source checkout in the current directory')
    selection.add_argument('--revision', help='Install this exact 40-character commit SHA')
    parser.add_argument('--adopt', action='store_true', help='Replace an existing unmanaged forge command, preserving a backup')
    arguments = parser.parse_args()
    if arguments.revision and not re.fullmatch('[a-f0-9]{40}', arguments.revision):
        raise ValueError('Use a full 40-character commit SHA for --revision.')
    private(install_root); private(state_root)
    lock = os.open(install_root / 'install.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    original = executable.lstat() if executable.exists() or executable.is_symlink() else None
    foreign = False
    if original:
        if not executable.is_symlink() or not executable.resolve().is_relative_to(install_root.resolve()):
            if not arguments.adopt:
                raise ValueError('Another forge executable exists at ~/.local/bin/forge. Use --adopt to replace it after verification and preserve a backup.')
            if not executable.is_symlink() and not executable.is_file():
                raise ValueError('The existing forge command must be a regular file or symlink.')
            foreign = True
    attempt = {'version': 1, 'id': uuid.uuid4().hex, 'started_at': started, 'status': 'installing'}
    record = state_root / ('cli-install-' + attempt['id'] + '.json')
    write(record, attempt)
    if arguments.local:
        source = Path.cwd()
        if not (source / 'forge_client/cli.py').is_file():
            raise ValueError('Run --local from the cloned Forge repository.')
        commit = command(['git', 'rev-parse', 'HEAD'])
        if command(['git', 'status', '--porcelain', '--untracked-files=no']):
            raise ValueError('Commit tracked changes before installing a pinned local client.')
        requirement = str(source)
    elif arguments.revision:
        commit = arguments.revision
        requirement = 'git+' + repository + '@' + commit
    else:
        resolved = command(['git', 'ls-remote', repository, 'refs/heads/main'])
        if not re.fullmatch('[a-f0-9]{40}\trefs/heads/main', resolved):
            raise ValueError('Could not resolve the Forge source revision. Retry the installer.')
        commit = resolved.split()[0]
        requirement = 'git+' + repository + '@' + commit
    if not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('The source revision is invalid.')
    attempt['revision'] = commit
    write(record, attempt)
    # Versioned paths preserve venv shebangs and keep the old CLI usable until
    # its replacement has passed its smoke check.
    versions = install_root / 'versions'
    private(versions)
    release = versions / (commit + '-' + attempt['id'])
    print('Installing the Forge CLI at ' + commit[:12] + '…', flush=True)
    command([sys.executable, '-m', 'venv', str(release)])
    python = str(release / 'bin/python')
    command([python, '-m', 'pip', 'install', '--quiet', requirement])
    installed = release / 'bin/forge'
    schemas = json.loads(command([str(installed), '--json', 'commands']))
    if schemas.get('ok') is not True or 'factory_setup_commands' not in schemas:
        raise ValueError('This source does not contain the new setup CLI. The existing command was preserved.')
    executable.parent.mkdir(parents=True, exist_ok=True)
    temporary = executable.parent / ('.forge-' + attempt['id'])
    temporary.symlink_to(installed)
    current = executable.lstat() if executable.exists() or executable.is_symlink() else None
    if current != original:
        temporary.unlink()
        raise ValueError('The existing forge command changed during installation; it was preserved.')
    if foreign:
        backup = install_root / ('adopted-forge-' + attempt['id'])
        shutil.copy2(executable, backup, follow_symlinks=False)
        attempt['adopted_backup'] = str(backup)
        write(record, attempt)
    os.replace(temporary, executable)
    attempt.update(status='installed', installed_at=time.time())
    write(record, attempt)
    print('Forge CLI installed. Run: ' + str(executable) + ' setup new')
    if str(executable.parent) not in os.environ.get('PATH', '').split(os.pathsep):
        print('To use the shorter forge command, add ~/.local/bin to your shell PATH.')
except (Exception, KeyboardInterrupt) as error:
    if 'record' in locals():
        attempt.update(status='failed', finished_at=time.time())
        write(record, attempt)
    message = str(error) if isinstance(error, ValueError) else 'CLI installation did not finish. Check Python venv support and network access, then rerun. The existing command was preserved.'
    print(message, file=sys.stderr)
    sys.exit(1)
PY
