"""SSH-only bootstrap for operator identity on an installed Tailscale host.

One bounded JSON request on stdin; one secret-free JSON result on stdout.
This module is deliberately not exposed through the dashboard HTTP API.
"""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler

from .operator_auth import AuthError, OperatorAuth, configuration
from .settings import read_json

SOURCE = Path(__file__).resolve().parent.parent


class BootstrapError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def need(condition, code, message):
    if not condition:
        raise BootstrapError(code, message)


def run(argv, *, timeout=30):
    try:
        result = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=timeout, check=True)
        need(len(result.stdout) <= 1024 * 1024, 'host_unavailable', 'Host output exceeded the limit.')
        return result.stdout
    except (OSError, subprocess.SubprocessError):
        raise BootstrapError('host_unavailable', 'The host command failed. Check SSH, Tailscale and service permissions; use --resume after repairing them.') from None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise BootstrapError('https_unavailable', 'The configured HTTPS endpoint redirected unexpectedly.')


def verify(origin):
    try:
        opener = build_opener(ProxyHandler({}), NoRedirect())
        with opener.open(Request(origin + '/api/auth/session', headers={'Accept': 'application/json'}), timeout=15) as response:
            raw = response.read(4097)
            need(response.status == 200 and len(raw) <= 4096, 'https_unavailable', 'Could not verify the HTTPS sign-in endpoint.')
            value = json.loads(raw)
            need(value.get('ok') is True and value.get('enabled') is True and value.get('operator') is None,
                 'https_unavailable', 'The HTTPS endpoint has not confirmed operator sign-in.')
    except BootstrapError:
        raise
    except Exception:
        raise BootstrapError('https_unavailable', 'Could not verify the HTTPS sign-in endpoint. Repair DNS or TLS and use --resume.') from None


class Bootstrap:
    def __init__(self, root, *, command=run, check_https=verify):
        self.root = Path(root)
        self.command, self.check_https = command, check_https
        need(self.root.is_absolute() and self.root.is_dir() and self.root.resolve() == self.root,
             'invalid_root', 'Choose the existing absolute runtime directory without symlinks.')
        info = self.root.stat()
        need(info.st_uid == os.getuid() and not info.st_mode & 0o077,
             'invalid_root', 'The runtime must be private and owned by the SSH service account.')
        from .store.runtime import selected
        from .store.configuration import Configuration
        self.database = selected(self.root)
        installed = Configuration(self.database).get('host-runtime.json') is not None if self.database else (self.root / 'host-runtime.json').is_file()
        need(installed, 'installation_required', 'Install this host with bin/forge setup before configuring sign-in.')
        self.auth = OperatorAuth(self.root)

    def generation(self):
        if self.database:
            from .onboarding import Onboarding
            setup = Onboarding(self.root).read()
        else:
            setup = read_json(self.root / 'state/onboarding/setup.json')
        need(setup.get('phase') in ('setup', 'active') and isinstance(setup.get('generation'), str),
             'reset_in_progress', 'Wait for the current Forge setup or reset to finish.')
        return setup['generation']

    def snapshot(self, target):
        # Validate public configuration using the same contract as operator sign-in.
        try:
            configuration(target['origin'], target['client_id'] or 'pending-client', '0' * 40, target['operators'])
        except AuthError:
            raise BootstrapError('invalid_request', 'Invalid bootstrap target.') from None
        origin = target['origin']
        host = urlsplit(origin).netloc
        status = json.loads(self.command(['tailscale', 'status', '--json']))
        dns = (status.get('Self') or {}).get('DNSName', '').rstrip('.').lower()
        need(status.get('BackendState') == 'Running' and host == dns and host.endswith('.ts.net'),
             'tailscale_origin_mismatch', 'Use this host’s own Tailscale HTTPS name without a port; keep it on the private tailnet.')
        serve = json.loads(self.command(['tailscale', 'serve', 'status', '--json']))
        expected = {'TCP': {'443': {'HTTPS': True}}, 'Web': {host + ':443': {'Handlers': {'/': {'Proxy': 'http://127.0.0.1:8787'}}}}}
        # Never overwrite another application, path, port or public Funnel configuration.
        need(serve in ({}, expected), 'https_conflict', 'Tailscale Serve already has a different configuration. Reconcile it on the host before continuing.')
        config = self.auth.config()
        if config:
            need(config['origin'] == origin and config['operators'] == target['operators']
                 and (target['client_id'] is None or config['client_id'] == target['client_id']),
                 'identity_conflict', 'This host already has different sign-in settings. Bootstrap will not replace them or revoke sessions.')
        public = {'origin': origin, 'client_id': config['client_id'] if config else target['client_id'],
                  'operators': target['operators'], 'generation': self.generation(), 'configured': bool(config)}
        revision = hashlib.sha256(json.dumps({'public': public, 'serve': serve}, sort_keys=True).encode()).hexdigest()
        return {**public, 'revision': revision}

    @contextmanager
    def locked(self):
        path = self.database.root / '.operator-bootstrap.lock' if self.database else self.root / 'state/operator-auth/bootstrap.lock'
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'a') as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BootstrapError('bootstrap_busy', 'Another CLI is configuring sign-in on this host.') from None
            yield

    def execute(self, request):
        need(isinstance(request, dict) and set(request) == {'version', 'action', 'target', 'revision', 'secret'},
             'invalid_request', 'Invalid bootstrap request.')
        target = request['target']
        need(type(request['version']) is int and request['version'] == 1 and request['action'] in ('plan', 'apply', 'resume')
             and isinstance(target, dict) and set(target) == {'origin', 'client_id', 'operators'}
             and isinstance(target['operators'], list) and all(type(i) is int for i in target['operators'])
             and target['operators'] == sorted(set(target['operators']))
             and (target['client_id'] is None or isinstance(target['client_id'], str)),
             'invalid_request', 'Invalid bootstrap target.')
        need(request['action'] == 'apply' or request['secret'] is None, 'invalid_request', 'Only apply may receive an identity-app secret.')
        with self.locked():
            from .sqlite_identity import identity_effect_lock
            with identity_effect_lock(self.root):
                view = self.snapshot(target)
                if request['action'] == 'plan':
                    return {'status': 'configured' if view['configured'] else 'planned', **view}
                need(request['revision'] == view['revision'], 'plan_changed', 'The host changed. Read a new plan before applying sign-in setup.')
                need(view['client_id'] is not None, 'identity_app_required', 'Register a dedicated identity-only GitHub OAuth App and supply its client ID.')
                if not view['configured']:
                    need(request['action'] == 'apply' and isinstance(request['secret'], str)
                         and re.fullmatch('[a-fA-F0-9]{40}', request['secret']),
                         'identity_secret_required', 'Supply the OAuth App client secret through the hidden prompt or --secret-stdin; never use a GitHub access token.')
                    try:
                        self.auth.configure(view['origin'], view['client_id'], request['secret'], view['operators'],
                                            if_unconfigured=True)
                    except AuthError:
                        raise BootstrapError('plan_changed', 'Sign-in was configured concurrently. Read a new plan.') from None
                request['secret'] = None
                need(self.generation() == view['generation'], 'plan_changed', 'Forge was reset during setup. Read a new plan and resume.')
            # No git pull, factory reset/start, App permission changes or shell from a response.
            try:
                self.command([str(SOURCE / 'bin/forge'), 'update', '--root', str(self.root), '--no-pull', '--bind', '127.0.0.1'], timeout=300)
            except BootstrapError:
                raise BootstrapError('host_update_failed', 'Identity is saved, but service reconciliation failed. Repair the host update and use --resume.') from None
            # Recheck after the updater: never replace a foreign proxy added during it.
            current = self.snapshot(target)
            need(current['generation'] == view['generation'], 'plan_changed', 'Forge was reset during setup. Read a new plan and resume.')
            try:
                self.command(['sudo', '-n', 'tailscale', 'serve', '--bg', '--yes', '--https=443', 'http://127.0.0.1:8787'])
            except BootstrapError:
                raise BootstrapError('https_setup_failed', 'Identity is saved, but Tailscale Serve failed. Check noninteractive sudo and tailnet HTTPS, then use --resume.') from None
            self.check_https(view['origin'])
            need(self.generation() == view['generation'], 'plan_changed', 'Forge was reset during setup. Read a new plan and resume.')
            return {'status': 'ready', **self.snapshot(target)}


def main():
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(16385)
        need(len(raw) <= 16384, 'invalid_request', 'Bootstrap request exceeded the size limit.')
        result = Bootstrap(args.root).execute(json.loads(raw))
        print(json.dumps({'ok': True, **result}, sort_keys=True))
        return 0
    except BootstrapError as error:
        print(json.dumps({'ok': False, 'error': error.code, 'reason': str(error)}))
    except Exception:
        # Never return subprocess output, input values or exception diagnostics.
        print(json.dumps({'ok': False, 'error': 'bootstrap_failed', 'reason': 'Bootstrap could not complete. Check the selected host and resume; no credentials were returned.'}))
    return 1


if __name__ == '__main__':
    sys.exit(main())
