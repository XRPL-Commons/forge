"""Ownership and cleanup for interactive login containers, separate from jobs."""
import hashlib
import json
from pathlib import Path
import subprocess
import os
import re
import shutil


def owner(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]


def reset_logins(root):
    from ..store.runtime import control_enabled
    if not control_enabled(root, 'harness_login_enabled'):
        return
    expected = owner(root)
    def docker(*args):
        return subprocess.run(['docker', *args], check=True, capture_output=True, text=True, timeout=30).stdout
    def inventory():
        return docker('container', 'ls', '-a', '--filter', 'label=forge.login-root=' + expected, '--format', '{{.ID}}').splitlines()
    for identifier in inventory():
        record = json.loads(docker('inspect', identifier))[0]
        if record['Config']['Labels'].get('forge.login-root') != expected:
            raise ValueError('Login container ownership changed during reset')
        docker('rm', '--force', record['Id'])
    if inventory():
        raise ValueError('Login container cleanup remains pending')
    # Detached device login homes are installation-owned, like their containers.
    # Remove them only after the owned-container inventory is empty.
    directory = Path(root) / 'credentials'
    for path in directory.glob('device-login-*'):
        if not re.fullmatch('device-login-[a-f0-9]{32}', path.name):
            continue
        if path.is_symlink() or not path.is_dir() or path.stat().st_uid != os.getuid():
            raise ValueError('Device login home ownership changed during reset')
        shutil.rmtree(path)


def mark_login(root):
    """Persist cleanup responsibility before starting a browser login container."""
    import time
    from ..store.runtime import required, writable
    from ..onboarding import control_in
    with required(root).write() as db:
        writable(db)
        control_in(db, 'harness_login_enabled', True, time.time(), {'owner': owner(root)})
