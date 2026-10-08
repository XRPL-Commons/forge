"""Ownership and cleanup for interactive login containers, separate from jobs."""
import hashlib
import json
from pathlib import Path
import subprocess


def owner(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]


def reset_logins(root):
    from ..store.runtime import selected
    database = selected(root)
    marker = Path(root) / 'state/harness-login-enabled.json'
    if database is not None:
        with database.read_snapshot() as db:
            row = db.execute("SELECT enabled FROM runtime_controls WHERE name='harness_login_enabled'").fetchone()
        enabled = bool(row and row[0])
    else:
        enabled = marker.exists()
    if not enabled:
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


def mark_login(root):
    """Persist cleanup responsibility before starting a browser login container."""
    from ..store.runtime import selected, writable
    database = selected(root)
    if database is None:
        from ..settings import write_json
        write_json(Path(root) / 'state/harness-login-enabled.json', {'owner': owner(root)})
    else:
        import time
        from ..sqlite_onboarding import control_in
        with database.write() as db:
            writable(db)
            control_in(db, 'harness_login_enabled', True, time.time(), {'owner': owner(root)})
