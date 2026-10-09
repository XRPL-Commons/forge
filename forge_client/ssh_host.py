"""Host-key discovery and explicit pinning; discovery alone never grants trust."""
import base64
from contextlib import contextmanager
import hashlib
import os
import re
import shutil
import tempfile

from .bootstrap import Host
from .contracts import SetupError, require
from .github import run_bounded


def environment():
    return {k: v for k, v in os.environ.items() if k in {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SSH_AUTH_SOCK'}}


def discover(ssh, *, run=run_bounded):
    Host(ssh, '/unused/source', '/unused/root')
    executable, scanner = shutil.which('ssh'), shutil.which('ssh-keyscan')
    require(executable and scanner, 'ssh_missing', 'Install the OpenSSH client on this computer.')
    try:
        raw = run([executable, '-G', ssh], environ=environment()).decode()
        config = dict(line.split(' ', 1) for line in raw.splitlines() if ' ' in line)
        hostname, port = config.get('hostname', ''), config.get('port', '')
        require(re.fullmatch('[A-Za-z0-9][A-Za-z0-9.-]{0,252}', hostname) and port.isdigit()
                and 1 <= int(port) <= 65535, 'invalid_host', 'Use an SSH destination with a DNS name or IPv4 address.')
        require(config.get('proxycommand', 'none') == 'none' and config.get('proxyjump', 'none') == 'none',
                'host_key_unavailable', 'For a jump-host connection, configure verified SSH host keys locally before setup.')
        output = run([scanner, '-T', '10', '-p', port, '-t', 'ed25519', hostname],
                     environ=environment(), timeout=15).decode()
        keys = set()
        for line in output.splitlines():
            if line.startswith('#'): continue
            parts = line.split()
            require(len(parts) == 3 and parts[1] == 'ssh-ed25519', 'host_key_unavailable', 'The host did not present one ED25519 key.')
            decoded = base64.b64decode(parts[2], validate=True)
            require(len(decoded) == 51 and decoded[:19] == b'\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20',
                    'host_key_unavailable', 'The host returned an invalid ED25519 key.')
            keys.add(parts[2])
        require(len(keys) == 1, 'host_key_unavailable', 'The destination presented inconsistent host keys.')
        key = keys.pop()
        fingerprint = 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(key)).digest()).decode().rstrip('=')
        return {'ssh': ssh, 'hostname': hostname, 'port': int(port), 'algorithm': 'ssh-ed25519',
                'key': key, 'fingerprint': fingerprint}
    except SetupError:
        raise
    except Exception:
        raise SetupError('host_key_unavailable', 'Could not read the SSH host key. Check the address, port and network access.') from None


@contextmanager
def pinned_options(ssh, fingerprint, *, discover_key=discover):
    if fingerprint is None:
        yield []
        return
    require(isinstance(fingerprint, str) and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', fingerprint),
            'invalid_host_key', 'Supply the verified ED25519 SHA256 fingerprint reported by forge setup host-key.')
    observed = discover_key(ssh)
    require(observed['fingerprint'] == fingerprint, 'host_key_changed',
            'The SSH host key differs from this setup’s approved fingerprint. Verify the VM identity before starting a new setup.')
    with tempfile.TemporaryDirectory(prefix='forge-ssh-') as directory:
        path = os.path.join(directory, 'known_hosts')
        with open(path, 'w') as stream:
            stream.write('forge-install-target ssh-ed25519 ' + observed['key'] + '\n')
        os.chmod(path, 0o600)
        yield ['-o', 'UserKnownHostsFile=' + path, '-o', 'GlobalKnownHostsFile=/dev/null',
               '-o', 'HostKeyAlias=forge-install-target', '-o', 'HostKeyAlgorithms=ssh-ed25519',
               '-o', 'UpdateHostKeys=no']
