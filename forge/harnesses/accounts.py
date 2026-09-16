"""Ownership and cleanup for interactive login containers, separate from jobs."""
import hashlib
import json
from pathlib import Path
import subprocess


def owner(root):
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:24]


def reset_logins(root):
    marker = Path(root) / 'state/harness-login-enabled.json'
    if not marker.exists():
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
