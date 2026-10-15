"""Resumable, host-owned Codex device login using the trusted harness image."""
from contextlib import redirect_stdout
import io
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
import uuid

from .harnesses.accounts import mark_login, owner
from .harnesses.registry import manifest
from .host_setup import login_lock, pending_login, connect_login
from .onboarding import Onboarding
from .sqlite_identity import identity_effect_lock
from forge_client.contracts import require


def device_prompt(output):
    """Only the provider's fixed device URL and one-time code may leave the host."""
    plain = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', output)
    code = re.search(r'^\s*([A-Z0-9]{4}-[A-Z0-9]{5})\s*$', plain, re.M)
    if 'https://auth.openai.com/codex/device' not in plain or not code:
        return None
    return {'url': 'https://auth.openai.com/codex/device', 'code': code.group(1)}


class DeviceAccount:
    def __init__(self, root, *, setup=None, run=subprocess.run, clock=time.time):
        self.root = Path(root)
        self.setup, self.run, self.clock = setup or Onboarding(root), run, clock

    def docker(self, *args, optional=False):
        result = self.run(['docker', *args], capture_output=True, text=True, timeout=30)
        require(len(result.stdout or '') + len(result.stderr or '') <= 131072, 'account_response_too_large')
        require(optional or result.returncode == 0, 'account_runtime_failed',
                'The provider login runtime could not complete this operation. Resume to inspect its state.')
        return result

    def inspect(self, record):
        result = self.docker('inspect', record['name'], optional=True)
        if result.returncode:
            # A failed daemon connection is not evidence that a container is absent.
            found = self.docker('container', 'ls', '-a', '--filter', 'name=^/' + record['name'] + '$', '--format', '{{.ID}}')
            require(not found.stdout.strip(), 'account_runtime_failed')
            return None
        rows = json.loads(result.stdout)
        require(isinstance(rows, list) and len(rows) == 1, 'account_runtime_failed')
        row = rows[0]; labels = row.get('Config', {}).get('Labels', {})
        require(labels.get('forge.login') == record['name'] and labels.get('forge.login-root') == owner(self.root)
                and labels.get('forge.login-generation') == record['generation']
                and labels.get('forge.login-session') == record['id'], 'account_owner_changed')
        return row

    def save(self, generation, record):
        with self.setup.store.transaction() as data:
            require(data['generation'] == generation and data['phase'] in ('setup', 'active') and not data.get('operation'),
                    'setup_changed', 'Setup changed during account login.')
            data.setdefault('device_logins', {})['codex'] = record

    def home(self, record):
        require(re.fullmatch('[a-f0-9]{32}', record['id']), 'invalid_account_session')
        return self.setup.credentials.root / ('device-login-' + record['id'])

    def cleanup(self, record):
        if self.inspect(record): self.docker('rm', '--force', record['name'])
        home = self.home(record)
        if home.exists():
            require(not home.is_symlink() and home.stat().st_uid == os.getuid(), 'account_owner_changed')
            shutil.rmtree(home)

    def call(self, provider, *, generation, setup_id, apply=False, retry=False):
        require(provider == 'codex', 'device_login_unsupported', 'This provider uses the existing guided terminal login.')
        require(type(apply) is bool and type(retry) is bool and (not retry or apply)
                and isinstance(setup_id, str) and re.fullmatch('[a-f0-9]{32}', setup_id))
        with login_lock(self.root, provider), identity_effect_lock(self.root):
            data = self.setup.read()
            require(data['generation'] == generation and data['phase'] in ('setup', 'active') and not data.get('operation'),
                    'setup_changed', 'Finish the current setup operation before signing in.')
            record = data.get('device_logins', {}).get(provider)
            if record:
                require(record['generation'] == generation and record['setup_id'] == setup_id,
                        'account_session_changed', 'This login belongs to an earlier setup; reset its owned login session first.')
            connected = next((item['connected'] for item in self.setup.harnesses(data) if item['id'] == provider), False)
            if connected and not record:
                return {'status': 'connected', 'provider': provider}
            if record and record['phase'] == 'complete':
                if connected:
                    return {'status': 'connected', 'provider': provider, 'session_id': record['id']}
                if not retry:
                    return {'status': 'account_login_failed', 'provider': provider, 'session_id': record['id'],
                            'reason': 'The completed account login is no longer connected. Restart login explicitly.'}
            if record and record['phase'] == 'finishing' and connected:
                reference = manifest(provider)['auth_reference']
                source = self.setup.credentials.environment(only=[reference]).get(reference)
                confirmed = source and hashlib.sha256(Path(source).read_bytes()).hexdigest() == record['credential_digest']
                if not confirmed:
                    return {'status': 'write_unconfirmed', 'provider': provider, 'session_id': record['id'],
                            'reason': 'The stored login changed during handoff. Inspect it before replacing credentials.'}
                if apply:
                    self.cleanup(record); self.save(generation, {**record, 'phase': 'complete'})
                return {'status': 'connected' if apply else 'account_login_saved', 'provider': provider, 'session_id': record['id']}
            if retry and record:
                require(record['phase'] == 'failed' or record['phase'] == 'complete' and not connected
                        or self.clock() >= record['expires_at'], 'account_login_running',
                        'Finish the current device login before starting another one.')
                self.cleanup(record)
                self.save(generation, None)
                record = None
            if not record:
                if not apply: return {'status': 'account_login_required', 'provider': provider}
                config = self.setup.runtime()
                require(config.get('image'), 'runner_image_required')
                identifier = uuid.uuid4().hex
                record = {'id': identifier, 'generation': generation, 'setup_id': setup_id,
                          'name': 'forge-login-' + owner(self.root) + '-codex', 'phase': 'prepared',
                          'started_at': self.clock(), 'expires_at': self.clock() + 900, 'image': config['image']}
                mark_login(self.root)
                self.save(generation, record)
            row = self.inspect(record)
            if self.clock() >= record['expires_at'] or record['phase'] == 'failed':
                if apply:
                    self.cleanup(record)
                    self.save(generation, {**record, 'phase': 'failed'})
                return {'status': 'account_login_failed', 'provider': provider, 'session_id': record['id'],
                        'reason': 'Device login expired or failed. Restart the account login explicitly.'}
            if row is None and record['phase'] != 'prepared':
                return {'status': 'write_unconfirmed', 'provider': provider, 'session_id': record['id'],
                        'reason': 'The recorded login container is missing. Inspect the host login session before restarting.'}
            if row is None:
                if not apply: return {'status': 'account_login_pending', 'provider': provider, 'session_id': record['id']}
                self.setup.credentials._private()
                home = self.home(record); home.mkdir(mode=0o700, exist_ok=True)
                require(not home.is_symlink() and home.stat().st_uid == os.getuid() and not home.stat().st_mode & 0o077,
                        'account_owner_changed')
                item = manifest(provider)
                for relative, content in item.get('login_files', {}).items():
                    path = home / relative; path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    path.write_text(content); path.chmod(0o600)
                self.docker('create', '--name', record['name'], '--label', 'forge.login=' + record['name'],
                    '--label', 'forge.login-root=' + owner(self.root), '--label', 'forge.login-generation=' + generation,
                    '--label', 'forge.login-session=' + record['id'], '--log-driver', 'local', '--log-opt', 'max-size=64k',
                    '--log-opt', 'max-file=1', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
                    '--user', str(os.getuid()) + ':' + str(os.getgid()), '--pids-limit', '128', '--memory', '1g',
                    '--tmpfs', '/tmp:rw,nosuid,nodev,size=128m', '--mount', 'type=bind,source=' + str(home) + ',target=/work/home',
                    '--env', 'HOME=/work/home', '--env', 'DISABLE_AUTOUPDATER=1', '--entrypoint', item['command'],
                    record['image'], *item['login'])
                row = self.inspect(record)
            state = row.get('State', {})
            if state.get('Status') == 'created':
                if apply:
                    self.docker('start', record['name'])
                    self.save(generation, {**record, 'phase': 'running'})
                return {'status': 'account_login_pending', 'provider': provider, 'session_id': record['id']}
            if state.get('Running'):
                output = self.docker('logs', '--tail', '100', record['name'])
                prompt = device_prompt((output.stdout or '') + '\n' + (output.stderr or ''))
                return {'status': 'account_login_required' if prompt else 'account_login_pending',
                        'provider': provider, 'session_id': record['id'], 'expires_at': record['expires_at'], **(prompt or {})}
            if state.get('ExitCode') != 0:
                if apply:
                    self.cleanup(record); self.save(generation, {**record, 'phase': 'failed'})
                return {'status': 'account_login_failed', 'provider': provider, 'session_id': record['id']}
            if not apply:
                return {'status': 'account_login_saved', 'provider': provider, 'session_id': record['id']}
            path = self.home(record) / manifest(provider)['auth_file']
            require(not path.is_symlink(), 'invalid_account_login')
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor) as stream:
                info = os.fstat(stream.fileno())
                require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and not info.st_mode & 0o077
                        and info.st_size <= 65536, 'invalid_account_login')
                value = stream.read()
            from .factory.codex_auth import renewable
            if renewable(json.loads(value)) is not None:
                self.cleanup(record); self.save(generation, {**record, 'phase': 'failed'})
                require(False, 'renewable_login_required', 'Codex did not save a renewable browser login; restart account login.')
            reference = manifest(provider)['auth_reference']
            source = self.setup.credentials.environment(only=[reference]).get(reference)
            same_login = source and hashlib.sha256(Path(source).read_bytes()).digest() == hashlib.sha256(value.encode()).digest()
            require(record['phase'] != 'finishing' or not connected or same_login, 'account_handoff_unconfirmed',
                    'A different login is now connected; inspect it before replacing provider credentials.')
            record = {**record, 'phase': 'finishing', 'credential_digest': hashlib.sha256(value.encode()).hexdigest()}
            self.save(generation, record)
            pending_login(self.setup, provider, generation, {'secret': None, 'value': value, 'models': None, 'kind': 'api_key'})
            # Reuse the same credential/configuration handoff as trusted terminal
            # login. It owns controller serialization and cooldown removal.
            if not same_login:
                with redirect_stdout(io.StringIO()): connect_login(self.root, provider)
            else:
                (self.setup.credentials.root / ('pending-login-' + provider + '.json')).unlink(missing_ok=True)
            self.cleanup(record)
            self.save(generation, {**record, 'phase': 'complete'})
            return {'status': 'connected', 'provider': provider, 'session_id': record['id']}
