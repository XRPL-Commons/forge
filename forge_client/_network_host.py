"""Private network setup using the same standalone installer helpers."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.request import build_opener, ProxyHandler, HTTPRedirectHandler

if globals().get('__package__'):
    from ._install_host import STATE, ENV, InstallError, need, run, read, facts, private_directory, locked, validate_intent
else:
    # The SSH payload registers this exact packaged helper module in memory.
    from forge_install_helpers import STATE, ENV, InstallError, need, run, read, facts, private_directory, locked, validate_intent

NETWORK = STATE / 'network.json'
TAILSCALE_LIST = b'deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu noble main\n'
SERVE_POLICY = b'forge ALL=(root) NOPASSWD: /usr/bin/tailscale serve --bg --yes --https\\=443 http\\://127.0.0.1\\:8787\n'


def network_write(data):
    fd, temporary = tempfile.mkstemp(prefix='.network-', dir=STATE)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, sort_keys=True, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, NETWORK)
        fd = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def network_read():
    need(not NETWORK.is_symlink(), 'unsafe_install_state')
    if not NETWORK.exists(): return None
    need(NETWORK.is_file() and NETWORK.stat().st_size <= 65536, 'unsafe_install_state')
    value = json.loads(NETWORK.read_text())
    need(isinstance(value, dict) and value.get('version') == 1, 'unsafe_install_state')
    return value


def network_running(data):
    result = subprocess.run(['systemctl', 'is-active', 'forge-network-' + data['intent']['id']],
                            env=ENV, stdin=subprocess.DEVNULL, capture_output=True, timeout=15)
    return result.stdout.strip() in (b'active', b'activating', b'deactivating', b'reloading')


def tailnet_status():
    import shutil
    if not shutil.which('tailscale', path=ENV['PATH']): return None
    return json.loads(run(['tailscale', 'status', '--json'], capture=True, timeout=15))


def tailnet_origin(value):
    if not value or value.get('BackendState') != 'Running': return None
    dns = (value.get('Self') or {}).get('DNSName', '').rstrip('.').lower()
    need(re.fullmatch(r'[a-z0-9][a-z0-9-]*\.[a-z0-9.-]+\.ts\.net', dns), 'network_not_ready')
    return 'https://' + dns


def network_view(data):
    if not data:
        return {'status': 'network_required', 'origin': None, 'url': None, 'step': None, 'error': None}
    if network_running(data):
        return {'status': 'network_installing', 'origin': None, 'url': None, 'step': data['step'], 'error': None}
    status = tailnet_status()
    origin = tailnet_origin(status)
    if origin and data.get('policy_ready'):
        return {'status': 'network_ready', 'origin': origin, 'url': None, 'step': 'complete', 'error': None}
    if data.get('error'):
        return {'status': 'network_interrupted', 'origin': None, 'url': None, 'step': data['step'], 'error': data['error']}
    if status and status.get('BackendState') == 'NeedsMachineAuth':
        return {'status': 'tailnet_approval_required', 'origin': None, 'url': 'https://login.tailscale.com/admin/machines', 'step': 'authorization', 'error': None}
    url = (status or {}).get('AuthURL') or data.get('url')
    if isinstance(url, str) and re.fullmatch(r'https://login\.tailscale\.com/a/[A-Za-z0-9]+', url):
        return {'status': 'tailnet_authorization_required', 'origin': None, 'url': url, 'step': 'authorization', 'error': None}
    return {'status': 'network_interrupted', 'origin': None, 'url': None, 'step': data['step'], 'error': data.get('error') or 'network_not_ready'}


def network_dispatch(request, payload):
    need(isinstance(request, dict) and set(request) == {'version', 'action', 'intent', 'hostname', 'upgrade_installer'}, 'invalid_request')
    need(type(request['version']) is int and request['version'] == 1 and request['action'] in ('plan', 'apply')
         and type(request['upgrade_installer']) is bool, 'invalid_request')
    validate_intent(request['intent'])
    need(isinstance(request['hostname'], str) and re.fullmatch('[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', request['hostname']), 'invalid_request')
    facts()
    installation = read()
    need(installation and installation['intent'] == request['intent'], 'installation_conflict')
    need(installation['step'] == 'complete', 'installation_required')
    data = network_read()
    if data:
        need(data['intent'] == request['intent'] and data['hostname'] == request['hostname'], 'network_conflict')
    if request['action'] == 'plan':
        return network_view(data)
    with locked('control'):
        data = network_read()
        if data:
            need(data['intent'] == request['intent'] and data['hostname'] == request['hostname'], 'network_conflict')
            current = network_view(data)
            if current['status'] in ('network_ready', 'network_installing'):
                return current
        else:
            data = {'version': 1, 'intent': request['intent'], 'hostname': request['hostname'],
                    'step': 'packages', 'url': None, 'error': None, 'policy_ready': False,
                    'payload': hashlib.sha256(payload.encode()).hexdigest()}
        digest = hashlib.sha256(payload.encode()).hexdigest()
        need(data['payload'] == digest or request['upgrade_installer'], 'installer_changed')
        with locked('network'):
            data.update(payload=digest, error=None)
            network_write(data)
            worker = STATE / 'network-worker.py'
            need(not worker.is_symlink(), 'unsafe_install_state')
            worker.write_text(payload); worker.chmod(0o600)
        log = STATE / 'network-install.log'
        fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600); os.close(fd)
        run(['systemd-run', '--quiet', '--collect', '--unit=forge-network-' + data['intent']['id'],
             '--property=Type=exec', '--property=UMask=0077',
             '--property=StandardOutput=append:' + str(log), '--property=StandardError=append:' + str(log),
             '/usr/bin/python3', str(worker), '--network-worker'], capture=True, timeout=30)
        return network_view(network_read())


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise InstallError('network_download_failed')


def public_download(url):
    with build_opener(ProxyHandler({}), NoRedirect()).open(url, timeout=30) as response:
        value = response.read(65537)
        need(response.status == 200 and 0 < len(value) <= 65536, 'network_download_failed')
        return value


def managed_network_file(path, content, mode=0o644):
    need(not path.is_symlink() and (not path.exists() or path.read_bytes() == content), 'network_conflict')
    if path.exists():
        need(path.stat().st_uid == os.geteuid() and not path.stat().st_mode & 0o022, 'network_conflict')
        return
    fd, temporary = tempfile.mkstemp(prefix='.forge-network-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content); stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


def network_work():
    import shutil
    os.umask(0o077)
    private_directory(STATE)
    with locked('network'):
        data = network_read()
        try:
            installation = read()
            need(installation['intent'] == data['intent'] and installation['step'] == 'complete', 'installation_conflict')
            if not shutil.which('tailscale', path=ENV['PATH']):
                data['step'] = 'packages'; network_write(data)
                key = public_download('https://pkgs.tailscale.com/stable/ubuntu/noble.noarmor.gpg')
                managed_network_file(Path('/usr/share/keyrings/tailscale-archive-keyring.gpg'), key)
                managed_network_file(Path('/etc/apt/sources.list.d/tailscale.list'), TAILSCALE_LIST)
                run(['apt-get', 'update'])
                run(['apt-get', 'install', '-y', 'tailscale'])
            run(['systemctl', 'enable', '--now', 'tailscaled'])
            # The existing identity bootstrap runs as the Forge service account.
            # Grant precisely its fixed private HTTPS command, with no secret.
            candidate = STATE / 'https-sudoers'
            need(not candidate.is_symlink(), 'unsafe_install_state')
            candidate.write_bytes(SERVE_POLICY); candidate.chmod(0o440)
            run(['visudo', '-cf', str(candidate)], capture=True)
            managed_network_file(Path('/etc/sudoers.d/forge-https'), SERVE_POLICY, 0o440)
            data.update(policy_ready=True, step='authorization'); network_write(data)
            status = tailnet_status()
            if not tailnet_origin(status):
                result = subprocess.run(['tailscale', 'up', '--json', '--timeout=10s', '--hostname=' + data['hostname']],
                                        env=ENV, stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
                need(len(result.stdout) <= 1048576, 'network_not_ready')
                # The CLI emits JSON objects as events. Return only a validated
                # browser handoff; never stdout, QR images, peer lists or errors.
                remaining = result.stdout.decode().strip()
                decoder = json.JSONDecoder()
                while remaining:
                    value, offset = decoder.raw_decode(remaining)
                    url = value.get('AuthURL') if isinstance(value, dict) else None
                    if isinstance(url, str) and re.fullmatch(r'https://login\.tailscale\.com/a/[A-Za-z0-9]+', url):
                        data['url'] = url
                    remaining = remaining[offset:].lstrip()
            data['error'] = None
            network_write(data)
            return 0
        except Exception as error:
            data['error'] = str(error) if isinstance(error, InstallError) else 'network_failed'
            network_write(data)
            return 1


def network_main(payload=None):
    os.umask(0o077)
    if sys.argv[1:] == ['--network-worker']:
        return network_work()
    try:
        raw = sys.stdin.buffer.read(8193)
        need(len(raw) <= 8192, 'invalid_request')
        result = network_dispatch(json.loads(raw), payload)
        print(json.dumps({'ok': True, **result}, sort_keys=True))
        return 0
    except Exception as error:
        code = str(error) if isinstance(error, InstallError) else 'network_failed'
        print(json.dumps({'ok': False, 'error': code}))
        return 1


main = network_main
if __name__ == '__main__':
    sys.exit(network_main())
