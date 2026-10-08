"""Local orchestration of host sign-in over authenticated SSH, never an HTTP setup form."""
import getpass
import json
import os
from pathlib import PurePosixPath
import re
import shlex
import shutil
import sys
from urllib.request import Request, build_opener, ProxyHandler

from .connection import NoRedirect, origin
from .contracts import SetupError, require
from .github import GitHub, run_bounded

ERRORS = {'invalid_root', 'installation_required', 'reset_in_progress', 'tailscale_origin_mismatch',
          'https_conflict', 'identity_conflict', 'plan_changed', 'identity_app_required', 'identity_secret_required',
          'bootstrap_busy', 'host_unavailable', 'host_update_failed', 'https_setup_failed', 'https_unavailable',
          'bootstrap_failed', 'invalid_request'}
MESSAGES = {
    'https_conflict': 'This host has another Tailscale Serve configuration. Reconcile it before continuing.',
    'identity_conflict': 'Existing sign-in settings differ. Bootstrap will not replace them or revoke sessions.',
    'plan_changed': 'The host changed. Run the command again to read a fresh plan.',
    'identity_secret_required': 'Provide the identity-app client secret using the hidden prompt or --secret-stdin.',
    'tailscale_origin_mismatch': 'Use the selected host’s own Tailscale HTTPS name without a port.',
    'installation_required': 'Install Forge on the selected host before configuring its sign-in.',
    'host_update_failed': 'Identity is saved, but the host update failed. Reconcile services with bin/forge update on the host, then use --resume.',
    'https_setup_failed': 'Identity is saved, but Tailscale Serve failed. Check the SSH account’s noninteractive sudo access and tailnet HTTPS, then use --resume.',
    'https_unavailable': 'Identity is saved, but HTTPS verification failed on the host. Check Tailscale, DNS and service health, then use --resume.',
}


def absolute_path(value):
    require(isinstance(value, str) and value.startswith('/') and len(value) <= 1024
            and not any(ord(c) < 32 for c in value) and '..' not in PurePosixPath(value).parts,
            'invalid_host', 'Use an explicit absolute source and runtime path.')
    return value


class Host:
    def __init__(self, ssh, source, root, *, run=run_bounded):
        require(isinstance(ssh, str) and re.fullmatch(r'(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]{0,252}', ssh),
                'invalid_host', 'Select a configured SSH host alias or user@hostname.')
        self.source, self.root = absolute_path(source), absolute_path(root)
        self.ssh, self.run = ssh, run

    def call(self, action, target, revision=None, secret=None):
        executable = shutil.which('ssh')
        require(executable, 'ssh_missing', 'Install the OpenSSH client and configure the selected host.')
        # Every argument is local intent. The server cannot return a shell command.
        remote = 'cd ' + shlex.quote(self.source) + ' && exec python3 -m forge.launch forge.operator_bootstrap --root ' + shlex.quote(self.root)
        argv = [executable, '-T', '-a', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ConnectTimeout=15', '-o', 'ClearAllForwardings=yes', '-o', 'ForwardX11=no', self.ssh, remote]
        environment = {key: value for key, value in os.environ.items()
                       if key in {'PATH', 'HOME', 'LANG', 'LC_ALL', 'TMPDIR', 'SSH_AUTH_SOCK'}}
        try:
            raw = self.run(argv, payload={'version': 1, 'action': action, 'target': target,
                                         'revision': revision, 'secret': secret}, environ=environment,
                           timeout=420, allowed_codes=(0, 1))
            result = json.loads(raw)
        except Exception:
            raise SetupError('host_unavailable', 'Could not run bootstrap over SSH. Verify the host key, local SSH login and Forge source version. If setup was interrupted, use --resume.') from None
        require(isinstance(result, dict), 'invalid_host_response', 'The host returned an invalid bootstrap response.')
        if result.get('ok') is not True:
            code = result.get('error') if result.get('error') in ERRORS else 'bootstrap_failed'
            # A remote message could contain anything, including the submitted secret.
            raise SetupError(code, MESSAGES.get(code, 'Host setup could not complete. Repair the selected host and use --resume.'))
        require(set(result) == {'ok', 'status', 'origin', 'client_id', 'operators', 'generation', 'configured', 'revision'}
                and result['status'] in ('planned', 'configured', 'ready')
                and result['origin'] == target['origin'] and result['operators'] == target['operators']
                and (target['client_id'] is None or result['client_id'] == target['client_id'])
                and (result['client_id'] is None or isinstance(result['client_id'], str) and re.fullmatch('[A-Za-z0-9_.-]{5,100}', result['client_id']))
                and isinstance(result['generation'], str) and re.fullmatch('[A-Za-z0-9_-]{1,100}', result['generation'])
                and type(result['configured']) is bool and isinstance(result['revision'], str)
                and re.fullmatch('[a-f0-9]{64}', result['revision']),
                'invalid_host_response', 'The host returned a mismatched bootstrap response.')
        return result


def verify_https(base):
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(Request(base + '/api/auth/session', headers={'Accept': 'application/json'}), timeout=15) as response:
            raw = response.read(4097)
            require(response.status == 200 and len(raw) <= 4096)
            value = json.loads(raw)
            require(value.get('ok') is True and value.get('enabled') is True and value.get('operator') is None)
    except Exception:
        raise SetupError('https_unavailable', 'The host was configured, but verified HTTPS is not reachable from this computer. Check Tailscale and DNS, then use --resume.') from None


def setup(args, *, host_factory=Host, github_factory=GitHub, verify=verify_https, prompt=getpass.getpass, stdin=None):
    base = origin(args.origin)
    require(not args.secret_stdin or args.apply, 'invalid_request', '--secret-stdin requires --apply.')
    require(not args.client_id or re.fullmatch('[A-Za-z0-9_.-]{5,100}', args.client_id), 'invalid_request', 'Supply the identity-only OAuth App client ID.')
    host = host_factory(args.ssh, args.source, args.root)
    github = github_factory()
    operators = [github.owner(login) for login in args.operator]
    target = {'origin': base, 'client_id': args.client_id, 'operators': sorted({operator['id'] for operator in operators})}
    plan = host.call('plan', target)
    # The structured browser handoff has a fixed GitHub URL and explicit fields.
    if not plan['client_id']:
        return {'status': 'identity_app_required', 'server': base, 'operators': operators,
                'next_action': {'url': 'https://github.com/settings/applications/new',
                                'application_name': 'Forge operator sign-in', 'homepage_url': base,
                                'callback_url': base + '/auth/github/callback', 'oauth_scopes': [],
                                'instruction': 'Create or reuse a dedicated identity-only OAuth App. Complete any GitHub passkey check in the browser, then rerun with --client-id. Supply the client secret privately, never in agent messages or command arguments.'}}
    if not args.apply and not args.resume:
        return {'status': 'planned', 'server': base, 'operators': operators, 'client_id': plan['client_id'],
                'configured': plan['configured'],
                'steps': ([] if plan['configured'] else ['Configure operator sign-in']) +
                         ['Reconcile services from the selected checkout with the UI on loopback',
                          'Ensure private Tailscale HTTPS', 'Verify HTTPS from the host and this computer'],
                'next_action': 'Run with --apply to configure this host, or --resume to finish an interrupted setup.'}
    secret = None
    if not plan['configured'] and not args.resume:
        if args.secret_stdin:
            stream = sys.stdin if stdin is None else stdin
            secret = stream.read(202).strip()
        elif args.json:
            return {'status': 'identity_secret_required', 'server': base,
                    'next_action': 'Supply the OAuth App client secret on stdin with --apply --secret-stdin, or use the interactive hidden prompt.'}
        else:
            require(sys.stdin.isatty(), 'identity_secret_required', 'Use --secret-stdin for non-interactive secret input.')
            secret = prompt('GitHub OAuth client secret (identity app only): ')
        require(isinstance(secret, str) and re.fullmatch('[a-fA-F0-9]{40}', secret), 'invalid_identity_secret',
                'Use the OAuth App’s 40-character client secret, not a GitHub personal access token.')
    result = host.call('resume' if args.resume else 'apply', target, plan['revision'], secret)
    secret = None
    require(result['status'] == 'ready' and result['configured'] and result['generation'] == plan['generation'],
            'invalid_host_response', 'The host did not confirm completed setup for the planned generation.')
    verify(base)
    return {'status': 'sign_in_required', 'server': base, 'operators': operators, 'client_id': result['client_id'],
            'next_action': {'url': base + '/login', 'instruction': 'Continue with GitHub to verify your operator account. Then connect your agent with forge connect.'}}
