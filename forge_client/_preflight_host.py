"""Standalone read-only preflight sent over verified SSH before installation."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import ssl
import subprocess
import sys

ENDPOINTS = ('registry-1.docker.io', 'registry.npmjs.org', 'pypi.org', 'files.pythonhosted.org',
             'github.com', 'api.openai.com', 'api.anthropic.com', 'api.x.ai', 'login.tailscale.com')


def reachable(host):
    try:
        with socket.create_connection((host, 443), timeout=5) as connection:
            with ssl.create_default_context().wrap_socket(connection, server_hostname=host):
                return True
    except (OSError, ValueError):
        return False


def main(payload):
    request = json.load(sys.stdin)
    if request != {'version': 1}:
        print(json.dumps({'ok': False, 'error': 'invalid_request'})); return 1
    path = Path('/proc/sys/kernel/apparmor_restrict_unprivileged_userns')
    restriction = path.read_text().strip() if path.exists() else None
    endpoints = dict(zip(ENDPOINTS, ThreadPoolExecutor(max_workers=5).map(reachable, ENDPOINTS)))
    tailnet = None
    if Path('/usr/bin/tailscale').is_file():
        result = subprocess.run(['/usr/bin/tailscale', 'status', '--json'], capture_output=True, timeout=15)
        if result.returncode == 0 and len(result.stdout) <= 1048576:
            value = json.loads(result.stdout)
            tailnet = {'state': value.get('BackendState'), 'cert_domains': value.get('CertDomains', []),
                       'dns_name': (value.get('Self') or {}).get('DNSName'),
                       'node_id': (value.get('Self') or {}).get('ID')}
    print(json.dumps({'ok': True, 'userns_restricted': restriction, 'egress': endpoints, 'tailnet': tailnet}))
    return 0
