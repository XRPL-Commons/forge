"""Parallel ChatGPT workers receive expiring access, never refresh authority.

The installation credential lock serializes refreshes. Codex's own app-server
renews the login in a durable private directory, so a controller restart can
recover a rotated token before launching another refresher. No repository is
mounted into that container and no model turn is requested.
"""
import base64
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time

from ..credentials import Credentials
from ..settings import read_json, write_json


MARGIN = 300


def expires_at(auth):
    try:
        token = auth['tokens']['access_token']
        payload = json.loads(base64.urlsafe_b64decode(token.split('.')[1] + '==='))
        expiry = payload['exp']
        if type(expiry) not in (int, float) or not 0 < expiry < 10**11:
            raise ValueError()
        return expiry
    except (KeyError, IndexError, TypeError, ValueError):
        raise ValueError('Codex login has no usable access-token expiry; reconnect it') from None


def account(auth):
    tokens = auth.get('tokens', {})
    value = tokens.get('account_id')
    if auth.get('auth_mode', 'chatgpt') != 'chatgpt' or not value or not tokens.get('refresh_token') or not tokens.get('id_token'):
        raise ValueError('Parallel Codex requires a managed ChatGPT login')
    return value


def snapshot(root, reference, valid_until, renew, *, clock=time.time):
    """Return an access-only credential valid beyond the entire worker deadline.

    JWT expiry is only a scheduling hint; OpenAI still authenticates every use.
    The renewal callback runs under the private credential lock and must not
    return while its refresh process is still running.
    """
    credentials = Credentials(root)
    with credentials.locked():
        source = Path(credentials.environment()[reference])
        raw = source.read_bytes()
        auth = json.loads(raw)
        identity = account(auth)
        origin = hashlib.sha256(raw).hexdigest()
        name = hashlib.sha256(reference.encode()).hexdigest()[:24]
        staging = credentials.root / ('codex-refresh-' + name)
        if staging.is_symlink():
            raise ValueError('Codex refresh directory must be private')
        staging.mkdir(mode=0o700, exist_ok=True)
        if staging.stat().st_mode & 0o077:
            raise ValueError('Codex refresh directory must be private')
        marker = staging / 'origin.json'
        staged = staging / 'auth.json'
        matches = marker.exists() and read_json(marker).get('digest') == origin
        # A renewal may have persisted before a controller crash. Reconcile its
        # container before reading or replacing any of its files.
        renew(staging, reconcile=True)
        if matches and staged.is_file():
            recovered = read_json(staged)
            if account(recovered) != identity:
                raise ValueError('Codex refresh changed the account; reconnect it')
            # Rotation can retain the same expiry. Never discard a newer
            # refresh token just because its access-token expiry is unchanged.
            auth = recovered
        if expires_at(auth) <= valid_until + MARGIN:
            write_json(staged, auth)
            write_json(marker, {'digest': origin})
            renew(staging, reconcile=False)
            auth = read_json(staged)
            if account(auth) != identity or expires_at(auth) <= valid_until + MARGIN:
                raise ValueError('Codex login could not be renewed for this worker')
        if auth != json.loads(raw):
            credentials._put(reference, json.dumps(auth), file=True)
        result = {'auth_mode': 'chatgptAuthTokens', 'tokens': deepcopy(auth['tokens']),
                  'last_refresh': datetime.fromtimestamp(clock(), timezone.utc).isoformat()}
        result['tokens']['refresh_token'] = ''
        return result


# Executed by the pinned image, with a private auth directory and no repository.
# Credentials and protocol replies are deliberately never printed.
REFRESH_SCRIPT = r'''
import json, os, selectors, subprocess, time
p = subprocess.Popen(['codex', 'app-server', '--stdio'], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
def send(value):
    p.stdin.write((json.dumps(value)+'\n').encode()); p.stdin.flush()
end = time.monotonic() + 40
buffer = b''
ok = False
try:
    send({'id':1,'method':'initialize','params':{'clientInfo':{'name':'forge_auth','version':'1'}}})
    with selectors.DefaultSelector() as selector:
        selector.register(p.stdout, selectors.EVENT_READ)
        while time.monotonic() < end:
            if not selector.select(max(0.01, end-time.monotonic())): break
            block = os.read(p.stdout.fileno(), 65536)
            if not block: break
            buffer += block
            if len(buffer) > 1048576: break
            while b'\n' in buffer:
                line, buffer = buffer.split(b'\n',1)
                value = json.loads(line)
                if value.get('id') == 1:
                    if 'error' in value: raise RuntimeError()
                    send({'method':'initialized'})
                    send({'id':2,'method':'account/read','params':{'refreshToken':True}})
                elif value.get('id') == 2:
                    ok = 'error' not in value and bool((value.get('result') or {}).get('account'))
                    raise SystemExit(0 if ok else 1)
finally:
    p.terminate()
    try: p.wait(timeout=5)
    except subprocess.TimeoutExpired: p.kill(); p.wait(timeout=5)
raise SystemExit(1)
'''


def renew_in_container(runner, directory, *, reconcile):
    """Reconcile a deterministic renewal container, or run one bounded renewal."""
    identity = hashlib.sha256(str(directory.resolve()).encode()).hexdigest()
    name = 'forge-codex-auth-' + identity[:24]
    rows = runner.call(['container', 'ls', '-a', '--no-trunc', '--filter', 'name=^/' + name + '$', '--format', '{{.ID}}']).stdout.decode().splitlines()
    if len(rows) > 1:
        raise ValueError('Codex renewal ownership is ambiguous')
    if rows:
        value = json.loads(runner.call(['container', 'inspect', rows[0]]).stdout)[0]
        if value['Name'] != '/' + name or value['Config'].get('Labels', {}).get('forge.codex-auth') != identity:
            raise ValueError('Codex renewal ownership changed')
        if value['State']['Running']:
            runner.call(['wait', rows[0]], timeout=50)
        runner.call(['rm', rows[0]])
    if reconcile:
        return
    config = runner.config
    args = ['create', '--name', name, '--label', 'forge.codex-auth=' + identity,
            '--init', '--read-only', '--user', str(os.getuid()) + ':' + str(os.getgid()),
            '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--memory', '256m', '--memory-swap', '256m', '--pids-limit', '64',
            '--log-driver', 'none', '--network', config['network'],
            '--tmpfs', '/tmp:rw,nosuid,nodev,size=32m',
            '--tmpfs', '/work:rw,nosuid,nodev,uid=' + str(os.getuid()) + ',gid=' + str(os.getgid()) + ',size=32m',
            '--mount', 'type=bind,src=' + str(directory.resolve()) + ',dst=/work/home/.codex',
            '--env', 'HOME=/work/home', '--env', 'CODEX_HOME=/work/home/.codex',
            '--env', 'HTTPS_PROXY=' + config['proxy'], '--env', 'HTTP_PROXY=' + config['proxy'],
            '--entrypoint', '/usr/local/bin/python3', config['image'], '-I', '-c', REFRESH_SCRIPT]
    # File storage is required for durable refresh recovery, regardless of any
    # host login preference. This directory contains no user configuration.
    (directory / 'config.toml').write_text('cli_auth_credentials_store = "file"\n')
    runner.call(args)
    runner.call(['start', '-a', name], timeout=50)
    value = json.loads(runner.call(['container', 'inspect', name]).stdout)[0]
    if value['State']['Running'] or value['State']['ExitCode'] != 0:
        raise ValueError('Codex subscription renewal failed; reconnect the login')
    runner.call(['rm', name])
