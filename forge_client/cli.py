"""Standalone client entry point. JSON output never includes client credentials."""
import argparse
import json
import sys
import webbrowser

from .connection import Client, CAPABILITIES, connection_lock
from .contracts import SetupError, require
from .github import GitHub


def parser():
    p = argparse.ArgumentParser(prog='forge', description='Connect a local agent to an existing Forge server.')
    p.add_argument('--json', action='store_true', help='Structured output; never opens a browser or prompts')
    sub = p.add_subparsers(dest='command', required=True)
    connect = sub.add_parser('connect')
    connect.add_argument('server')
    connect.add_argument('--name', default='Local agent')
    connect.add_argument('--repo', action='append', default=[])
    connect.add_argument('--owner', action='append', default=[])
    connect.add_argument('--read-only', action='store_true')
    connect.add_argument('--resume', action='store_true')
    connect.add_argument('--wait', type=int, default=0)
    auth = sub.add_parser('auth').add_subparsers(dest='auth_command', required=True)
    auth.add_parser('status').add_argument('server')
    sub.add_parser('disconnect').add_argument('server')
    requirements = sub.add_parser('requirements')
    requirements.add_argument('server'); requirements.add_argument('repository')
    sub.add_parser('commands')
    return p


def execute(args):
    if args.command == 'commands':
        return {'commands': ['connect', 'connect --resume', 'auth status', 'disconnect', 'requirements'],
                'credential_storage': 'OS keyring', 'transport': 'verified HTTPS'}
    client = Client(args.server)
    with connection_lock(client.key):
        if args.command == 'connect':
            if args.resume:
                return client.resume(args.wait)
            require(bool(args.repo or args.owner), 'scope_required', 'Select --repo or --owner for this connection.')
            github = GitHub()
            scope = {'repositories': [], 'owners': []}
            for name in args.repo:
                repo = github.repository(name)
                scope['repositories'].append({k: repo[k] for k in ('id', 'name')})
            for login in args.owner:
                scope['owners'].append(github.owner(login))
            value = client.begin(args.name, list(CAPABILITIES[:1] if args.read_only else CAPABILITIES), scope)
            if not args.json:
                print('Approve code ' + value['user_code'] + ' at ' + value['verification_uri_complete'])
                webbrowser.open(value['verification_uri_complete'])
                return client.resume(args.wait or 600)
            return {'status': 'authorization_pending', **value}
        if args.command == 'auth':
            return client.status()
        if args.command == 'disconnect':
            return client.disconnect()
        repo = GitHub().repository(args.repository)
        return client.requirements({k: repo[k] for k in ('id', 'name')})


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        print(json.dumps({'ok': True, **result}, sort_keys=True))
        return 0
    except SetupError as error:
        print(json.dumps({'ok': False, 'error': error.code, 'reason': str(error)}))
        return 1
    except (Exception, KeyboardInterrupt):
        # Raw network/keyring/subprocess exceptions may contain credentials.
        print(json.dumps({'ok': False, 'error': 'client_unavailable', 'reason': 'Client operation could not complete. Check the connection and OS credential store.'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
