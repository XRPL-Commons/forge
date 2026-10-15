"""Resumable migration from role Apps + an OAuth App to one Forge App and Tailscale identity.

Three independent, idempotent steps. ``plan`` reads where an installation is;
each apply step can be repeated after a crash or a stale read without losing
anything:

1. ``apps``: every role points at the controller (Forge) App; former role Apps
   become ``retired_apps`` so their checks, reviews and PRs stay attributable.
2. ``sign_in``: operator "Login with GitHub" moves to the Forge App's own
   client; operators, sessions, agent grants and audit history are kept.
3. ``tailscale``: operator identity comes from Tailscale Serve first, with the
   GitHub sign-in as fallback.

Nothing here changes GitHub App permissions, rulesets or Tailscale Serve; the
plan names those live approvals instead.
"""
import argparse
import json
import re
import sys

from .app_consolidation import Consolidation, ConsolidationError
from .operator_auth import AuthError, OperatorAuth
from .settings import Settings


class MigrationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class IdentityMigration:
    def __init__(self, root, *, settings=None, auth=None):
        self.settings = settings or Settings(root)
        self.auth = auth or OperatorAuth(root)
        self.apps = Consolidation(self.settings)

    def app_client_id(self):
        """The Forge App's OAuth client ID, read from GitHub with the App's own JWT."""
        connection = self.settings.load()['connections'].get('controller')
        if not connection:
            raise MigrationError('controller_required', 'Configure the Forge (controller) App first.')
        client = self.settings.client
        app = client.request('GET', '/app', client.jwt(connection))
        value = app.get('client_id') if isinstance(app, dict) and app.get('id') == connection['app_id'] else None
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{5,100}', value):
            raise MigrationError('app_unverified', 'GitHub did not confirm the Forge App client.')
        return value

    def plan(self, *, app_client_id=None):
        apps = self.apps.plan()
        config = self.auth.config()
        identity = self.auth.identity_settings() if config else {'mode': 'github', 'tailnet_operators': []}
        sign_in = ('not_configured' if not config else 'unknown' if app_client_id is None
                   else 'complete' if config['client_id'] == app_client_id else 'pending')
        approvals = []
        for blocker in apps['blockers']:
            if blocker['code'] in ('app_permissions_required', 'installation_coverage'):
                approvals.append(blocker)
        if sign_in == 'pending':
            approvals.append({'code': 'app_client_secret', 'detail':
                'Generate a client secret on the Forge App settings page and supply it privately.'})
        if identity['mode'] != 'tailscale':
            approvals.append({'code': 'tailscale_serve', 'detail':
                'Serve the operator UI with tailscale serve on the HTTPS origin, and set the UI to trust Serve identity.'})
        return {'revision': apps['revision'],
                'steps': {'apps': apps['status'], 'sign_in': sign_in,
                          'tailscale': 'complete' if identity['mode'] == 'tailscale' else 'pending'},
                'retire': apps['retire'], 'blockers': apps['blockers'], 'approvals': approvals,
                'tailnet_operators': len(identity['tailnet_operators'])}

    def apply_apps(self, revision):
        try:
            result = self.apps.apply(revision)
            checked = self.settings.refresh_all()
            return {**result, 'protection_check': 'complete' if checked else 'pending'}
        except ConsolidationError as error:
            raise MigrationError(error.code, str(error)) from None

    def apply_sign_in(self, client_secret, *, client_id=None):
        """Point sign-in at the Forge App, verified against GitHub, never another client."""
        expected = self.app_client_id()
        if client_id is not None and client_id != expected:
            raise MigrationError('client_mismatch', 'That client does not belong to the Forge App.')
        try:
            return self.auth.rotate_client(expected, client_secret)
        except AuthError as error:
            raise MigrationError('sign_in_unavailable', str(error)) from None

    def apply_tailscale(self, mode='tailscale'):
        try:
            return self.auth.set_identity_mode(mode)
        except AuthError as error:
            raise MigrationError('sign_in_unavailable', str(error)) from None


def main(argv=None, *, stdin=sys.stdin):
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('command', choices=['plan', 'apps', 'sign-in', 'tailscale'])
    parser.add_argument('--root', required=True)
    parser.add_argument('--revision', help='Plan revision for the apps step')
    parser.add_argument('--mode', choices=['tailscale', 'github'], default='tailscale')
    args = parser.parse_args(argv)
    migration = IdentityMigration(args.root)
    try:
        if args.command == 'plan':
            try:
                client_id = migration.app_client_id()
            except Exception:
                client_id = None
            result = migration.plan(app_client_id=client_id)
        elif args.command == 'apps':
            result = migration.apply_apps(args.revision or '')
        elif args.command == 'sign-in':
            # The secret is read from stdin only, never from arguments or the environment.
            secret = stdin.readline().strip()
            result = migration.apply_sign_in(secret)
        else:
            result = migration.apply_tailscale(args.mode)
        print(json.dumps({'ok': True, **result}, sort_keys=True))
        return 0
    except MigrationError as error:
        print(json.dumps({'ok': False, 'error': error.code, 'reason': str(error)}))
    except (ValueError, OSError):
        print(json.dumps({'ok': False, 'error': 'migration_failed', 'reason': 'The step did not complete; read a new plan and retry.'}))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
