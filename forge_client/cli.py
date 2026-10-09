"""Standalone client entry point. JSON output never includes client credentials."""
import argparse
import json
import sys
import webbrowser

from .connection import Client, CAPABILITIES, connection_lock
from .contracts import SetupError, require
from .github import GitHub


def repository_commands():
    commands = {}
    for action in ('inspect', 'plan', 'setup', 'verify', 'enable', 'disable', 'activity', 'issues', 'issue'):
        properties = {'repository': {'type': 'string', 'description': 'GitHub OWNER/REPO'},
                      'server': {'type': 'string', 'format': 'uri', 'description': 'Previously connected HTTPS origin'}}
        required = ['repository', 'server']
        if action in ('setup', 'verify', 'enable', 'disable'):
            properties.update(resume={'type': 'boolean', 'default': False},
                              wait={'type': 'integer', 'minimum': 0, 'maximum': 600, 'default': 120})
        if action == 'setup':
            properties.update(apply={'type': 'boolean', 'default': False}, enable={'type': 'boolean', 'default': False})
        if action == 'issues':
            properties['page'] = {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 1}
        if action == 'issue':
            properties['number'] = {'type': 'integer', 'minimum': 1}
            required.append('number')
        commands[action] = {
            'usage': 'forge repo ' + action + ' OWNER/REPO' + (' NUMBER' if action == 'issue' else '') + ' --server HTTPS_ORIGIN --json',
            'capability': 'repositories:activate' if action in ('enable', 'disable') else 'repositories:prepare' if action in ('setup', 'verify') else 'repositories:read',
            'input_schema': {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': required},
        }
    return commands


def migration_commands():
    result = {}
    for action in ('plan', 'prepare', 'capture', 'resume', 'import', 'verify', 'activate'):
        remote = action in ('plan', 'prepare', 'capture', 'resume', 'activate')
        names = ('ssh', 'source', 'root') if remote else ('snapshot', 'destination')
        properties = {name: {'type': 'string'} for name in names}
        required = list(names)
        if action == 'capture':
            properties.update(snapshot={'type': 'string'}, apply={'type': 'boolean', 'const': True})
            required += ['snapshot', 'apply']
        if action in ('prepare','activate'):
            properties['apply'] = {'type':'boolean','const':True}
            required.append('apply')
        if action == 'resume':
            properties.update(accept_tool_update={'type':'boolean'},source_revision={'type':'string','pattern':'^[0-9a-f]{40}$'})
        if not remote:
            properties['batch'] = {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 100}
        result[action] = {
            'usage': 'forge migrate ' + action + ' --json',
            'input_schema': {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': required},
            'maintenance': action in ('capture', 'resume', 'activate'),
            'installs_private_runtime': action == 'prepare',
            'activation_supported': action in ('activate','resume'),
            'target': 'selected SSH host' if remote else 'private local staging',
        }
    return result


def parser():
    p = argparse.ArgumentParser(prog='forge', description='Connect a local agent to an existing Forge server.')
    p.add_argument('--json', action='store_true', help='Structured output; never opens a browser or prompts')
    sub = p.add_subparsers(dest='command', required=True)
    connect = sub.add_parser('connect', help='Connect this computer to a Forge server (approve it in your browser)')
    connect.add_argument('server')
    connect.add_argument('--name', default='Local agent')
    connect.add_argument('--repo', action='append', default=[])
    connect.add_argument('--owner', action='append', default=[])
    connect.add_argument('--read-only', action='store_true')
    connect.add_argument('--resume', action='store_true')
    connect.add_argument('--wait', type=int, default=0)
    auth = sub.add_parser('auth', help='Check this computer’s connection to a Forge server').add_subparsers(dest='auth_command', required=True)
    auth.add_parser('status', help='Show whether the saved connection is still valid').add_argument('server')
    sub.add_parser('disconnect', help='Forget a saved Forge server connection').add_argument('server')
    requirements = sub.add_parser('requirements', help='Show what a repository needs before Forge can work on it')
    requirements.add_argument('server'); requirements.add_argument('repository')
    repo = sub.add_parser('repo', help='Inspect, prepare and enable a repository').add_subparsers(dest='repo_command', required=True)
    for action in ('inspect', 'plan', 'setup', 'verify', 'enable', 'disable', 'activity', 'issues', 'issue'):
        command = repo.add_parser(action)
        command.add_argument('repository', help='GitHub OWNER/REPO')
        command.add_argument('--server', required=True, help='Previously connected HTTPS Forge origin')
        command.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        if action == 'issue':
            command.add_argument('number', type=int)
        if action == 'issues':
            command.add_argument('--page', type=int, default=1)
        if action in ('setup', 'verify', 'enable', 'disable'):
            command.add_argument('--resume', action='store_true', help='Resume the saved operation with the same options')
            command.add_argument('--wait', type=int, default=120, help='Bounded wait for each Forge operation (0–600 seconds)')
        if action == 'setup':
            command.add_argument('--apply', action='store_true', help='Apply the local preparation plan')
            command.add_argument('--enable', action='store_true', help='Enable automation after verification')
    server = sub.add_parser('server', help='Configure an existing Forge host over SSH').add_subparsers(dest='server_command', required=True)
    setup = server.add_parser('setup', help='Bootstrap operator sign-in on an existing host over SSH')
    setup.add_argument('--ssh', required=True, help='Explicit SSH host alias or user@hostname; known host key required')
    setup.add_argument('--source', required=True, help='Absolute Forge source directory on that host')
    setup.add_argument('--root', required=True, help='Absolute private Forge runtime directory on that host')
    setup.add_argument('--origin', required=True, help='This host’s private Tailscale HTTPS origin')
    setup.add_argument('--operator', action='append', required=True, help='GitHub login to authorize; resolved locally to an immutable ID')
    setup.add_argument('--client-id', help='Dedicated identity-only GitHub OAuth App client ID')
    mode = setup.add_mutually_exclusive_group()
    mode.add_argument('--apply', action='store_true', help='Configure the selected host')
    mode.add_argument('--resume', action='store_true', help='Finish an interrupted setup without another secret')
    setup.add_argument('--secret-stdin', action='store_true', help='Read the identity-app secret from stdin; never a command argument')
    migration = sub.add_parser('migrate', help='Plan, verify and activate SQLite with a resumable one-way migration')
    migration.add_argument('migration_command', choices=('plan','prepare','capture','resume','import','verify','activate'))
    for name in ('ssh','source','root','snapshot','destination'):
        migration.add_argument('--'+name)
    migration.add_argument('--apply', action='store_true', help='Authorize runtime preparation, capture, or one-way SQLite activation')
    migration.add_argument('--accept-tool-update', action='store_true', help='resume: authorize a pinned source-only repair and reimport the same frozen snapshot before selection')
    migration.add_argument('--source-revision', help='resume repair: full commit SHA to install by clean fast-forward, without restarting services')
    migration.add_argument('--batch', type=int, default=100)
    migration.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    sub.add_parser('commands', help='List every command as JSON for agents')
    sub.add_parser('skill', help='Print the bundled Forge repository agent skill')
    return p


def execute(args):
    if args.command == 'migrate':
        from .migration import execute as migrate
        return migrate(args)
    if args.command == 'skill':
        from importlib.resources import files
        return {'skill': files('forge_client').joinpath('skills/forge-repositories/SKILL.md').read_text()}
    if args.command == 'server':
        from .bootstrap import setup
        return setup(args)
    if args.command == 'commands':
        return {'commands': ['connect', 'connect --resume', 'migrate plan', 'migrate prepare', 'migrate capture', 'migrate resume', 'migrate import', 'migrate verify', 'migrate activate --apply', 'auth status', 'disconnect', 'requirements', 'repo', 'skill', 'server setup', 'server setup --apply', 'server setup --resume'],
                'schema_version': 1, 'repository_commands': repository_commands(),
                'migration_commands': migration_commands(),
                'setup_statuses': ['planned', 'ready', 'enabled', 'blocked', 'needs_user_action',
                                   'operation_pending', 'verification_pending', 'write_unconfirmed'],
                'credential_storage': 'OS keyring', 'transport': 'verified HTTPS',
                'server_setup': {'authority': 'explicit SSH host', 'secret_input': 'hidden prompt or stdin',
                                 'scope': 'existing Tailscale host'}}
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
        if args.command == 'repo':
            from .workflow import Workflow
            github = GitHub()
            target = {k: v for k, v in github.repository(args.repository).items() if k in ('id', 'name')}
            if args.repo_command in ('inspect', 'activity', 'issues', 'issue'):
                return client.read(target, 'repository' if args.repo_command == 'inspect' else args.repo_command,
                                   getattr(args, 'number', None), getattr(args, 'page', 1))
            workflow = Workflow(client, github, target, wait=getattr(args, 'wait', 120))
            return workflow.execute('setup' if args.repo_command == 'plan' else args.repo_command,
                                    apply=getattr(args, 'apply', False), enable=getattr(args, 'enable', False),
                                    resume=getattr(args, 'resume', False))
        repo = GitHub().repository(args.repository)
        return client.requirements({k: repo[k] for k in ('id', 'name')})


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        parser().print_help()
        return 0
    args = parser().parse_args(argv)
    try:
        result = execute(args)
        if args.command == 'skill' and not args.json:
            print(result['skill'])
            return 0
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
