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
    for action in ('inspect', 'plan', 'setup', 'verify', 'enable', 'disable', 'activity', 'issues', 'issue', 'fix-rules'):
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
    for action in ('plan', 'prepare', 'capture', 'resume', 'import', 'verify', 'activate', 'upgrade'):
        remote = action in ('plan', 'prepare', 'capture', 'resume', 'activate', 'upgrade')
        names = ('ssh', 'source', 'root') if remote else ('snapshot', 'destination')
        properties = {name: {'type': 'string'} for name in names}
        required = list(names)
        if action == 'capture':
            properties.update(snapshot={'type': 'string'}, apply={'type': 'boolean', 'const': True})
            required += ['snapshot', 'apply']
        if action in ('prepare','activate'):
            properties['apply'] = {'type':'boolean','const':True}
            required.append('apply')
        if action == 'upgrade':
            properties.update(apply={'type':'boolean'},source_revision={'type':'string','pattern':'^[0-9a-f]{40}$'})
        if action == 'resume':
            properties.update(accept_tool_update={'type':'boolean'},source_revision={'type':'string','pattern':'^[0-9a-f]{40}$'})
        if not remote:
            properties['batch'] = {'type': 'integer', 'minimum': 1, 'maximum': 1000, 'default': 100}
        result[action] = {
            'usage': 'forge migrate ' + action + ' --json',
            'input_schema': {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': required},
            'maintenance': action in ('capture', 'resume', 'activate', 'upgrade'),
            'installs_private_runtime': action == 'prepare',
            'activation_supported': action in ('activate','resume'),
            'target': 'selected SSH host' if remote else 'private local staging',
        }
    return result


def parser():
    p = argparse.ArgumentParser(prog='forge', description='Install Forge factories and connect local agents.')
    p.add_argument('--json', action='store_true', help='Structured output; never opens a browser or prompts')
    sub = p.add_subparsers(dest='command', required=True)
    fresh = sub.add_parser('setup', help='Install a fresh factory from this computer').add_subparsers(dest='setup_command', required=True)
    for action in ('new', 'plan', 'apply', 'status', 'resume', 'host-key', 'network', 'identity'):
        command = fresh.add_parser(action, aliases=['sign-in'] if action == 'identity' else [],
                                   help='Walk through GitHub sign-in setup' if action == 'identity' else
                                   'Guided fresh-host installation' if action == 'new' else action.capitalize() + ' a saved host installation')
        command.add_argument('--ssh', help='SSH account and destination; verified host key and root or noninteractive sudo required')
        command.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        command.add_argument('--wait', type=int, default=0, help='Watch progress for at most 0–600 seconds')
        if action in ('resume', 'network'):
            command.add_argument('--upgrade-installer', action='store_true', help='Explicitly repair a stopped installation with this client implementation; keeps the pinned server source and completed stages')
        if action == 'network':
            command.add_argument('--apply', action='store_true', help='Install and initialize private Tailscale access')
            command.add_argument('--hostname', help='Tailscale hostname; defaults to the factory name')
        if action == 'identity':
            command.add_argument('--operator', action='append', help='GitHub operator login; repeat for multiple accounts')
            command.add_argument('--client-id', help='Dedicated identity OAuth App client ID')
            mode = command.add_mutually_exclusive_group()
            mode.add_argument('--apply', action='store_true')
            mode.add_argument('--resume', action='store_true')
            command.add_argument('--secret-stdin', action='store_true', help='Read the identity App secret privately from stdin')
        if action in ('new', 'plan'):
            command.add_argument('--name', help='Factory display name')
            command.add_argument('--revision', help='Full Forge commit SHA; default main is resolved once and saved')
            command.add_argument('--host-key', help='Verified ED25519 SHA256 fingerprint for this fresh VM')
            command.add_argument('--new-run', action='store_true', help='Archive the local run and plan again after rebuilding the VM; never wipes the host')
    for action in ('continue', 'inspect', 'apps', 'account', 'repository', 'check', 'start', 'pilot'):
        command = fresh.add_parser(action, help='Continue initial setup through the first verified merge' if action == 'continue' else action.capitalize() + ' initial factory setup')
        command.add_argument('--ssh', required=True, help='The saved administrative SSH destination')
        command.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        command.add_argument('--wait', type=int, default=0, help='Watch for at most 0–600 seconds')
        if action not in ('continue', 'inspect'):
            command.add_argument('--apply', action='store_true', help='Apply this explicit setup step')
        if action == 'apps':
            command.add_argument('--role', choices=('build', 'review', 'audit', 'controller'))
            command.add_argument('--owner', help='GitHub organization or user that owns all four Apps')
        if action == 'account':
            command.add_argument('--provider', required=True, choices=('codex', 'claude', 'grok'))
            command.add_argument('--kind', choices=('browser', 'api_key', 'token'), default='browser')
            command.add_argument('--secret-stdin', action='store_true', help='Read only the selected provider credential from stdin')
        if action in ('repository', 'pilot'):
            command.add_argument('--repository', help='First GitHub OWNER/REPO; subsequent calls reuse it')
        if action == 'pilot':
            command.add_argument('--issue', type=int, help='Existing issue to complete normally; retries preserve this binding')
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
    for action in ('inspect', 'plan', 'setup', 'verify', 'enable', 'disable', 'activity', 'issues', 'issue', 'fix-rules'):
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
    migration.add_argument('migration_command', choices=('plan','prepare','capture','resume','import','verify','activate','upgrade'))
    for name in ('ssh','source','root','snapshot','destination'):
        migration.add_argument('--'+name)
    migration.add_argument('--apply', action='store_true', help='Authorize runtime preparation, capture, or one-way SQLite activation')
    migration.add_argument('--accept-tool-update', action='store_true', help='resume: authorize a pinned source-only repair and reimport the same frozen snapshot before selection')
    migration.add_argument('--source-revision', help='Full commit SHA for an upgrade or an explicitly authorized resume repair')
    migration.add_argument('--batch', type=int, default=100)
    migration.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    sub.add_parser('commands', help='List every command as JSON for agents')
    sub.add_parser('skill', help='Print the bundled Forge repository agent skill')
    return p


def execute(args):
    if args.command == 'setup':
        if args.setup_command in ('continue', 'inspect', 'apps', 'account', 'repository', 'check', 'start', 'pilot'):
            from .factory_setup import execute as setup_stage, wizard as finish_setup
            from .install import Installation
            if args.setup_command == 'continue':
                if not args.json and sys.stdin.isatty():
                    return finish_setup(Installation(args.ssh))
                args.setup_command = 'inspect'
            return setup_stage(args)
        from .install import execute as install, wizard
        if args.setup_command == 'sign-in':
            args.setup_command = 'identity'
        if args.setup_command == 'identity' and not args.json and sys.stdin.isatty() and not (args.apply or args.resume or args.secret_stdin):
            from .identity import wizard as sign_in
            from .install import Installation
            require(args.ssh, 'ssh_required', 'Run forge setup sign-in --ssh HOST with your saved SSH destination.')
            return sign_in(Installation(args.ssh), operator=args.operator, client_id=args.client_id)
        if args.setup_command == 'new' and not args.json and sys.stdin.isatty():
            return wizard(args)
        return install(args)
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
        from .install import schemas
        return {'commands': ['setup new', 'setup plan', 'setup apply', 'setup status', 'setup resume', 'setup host-key', 'setup network', 'setup identity', 'setup sign-in', 'setup continue', 'setup inspect', 'setup apps', 'setup account', 'setup repository', 'setup check', 'setup start', 'setup pilot', 'connect', 'connect --resume', 'migrate plan', 'migrate prepare', 'migrate capture', 'migrate resume', 'migrate import', 'migrate verify', 'migrate activate --apply', 'migrate upgrade', 'auth status', 'disconnect', 'requirements', 'repo', 'skill', 'server setup', 'server setup --apply', 'server setup --resume'],
                'schema_version': 1, 'repository_commands': repository_commands(), 'factory_setup_commands': schemas(),
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
            if args.repo_command == 'fix-rules':
                from .rule_fixes import fixes
                return fixes(client.read(target, 'repository'))
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
        if args.command == 'repo' and args.repo_command == 'fix-rules' and not args.json:
            print(result['instruction'])
            for fix in result['fixes']:
                print('\n' + fix['title'])
                print(fix['command'] or '\n'.join(str(i + 1) + '. ' + step for i, step in enumerate(fix['steps'])))
            if not result['fixes']:
                print('No failing review rules in the latest Forge observation.')
            return 0
        if args.command == 'skill' and not args.json:
            print(result['skill'])
            return 0
        if args.command == 'setup' and not args.json:
            from .identity import display
            if display(result, args.ssh):
                return 0
        print(json.dumps({'ok': True, **result}, sort_keys=True))
        return 0
    except SetupError as error:
        if args.command == 'setup' and args.setup_command in ('identity', 'sign-in') and not args.json:
            from .identity import display_error
            display_error(error, args.ssh)
            return 1
        print(json.dumps({'ok': False, 'error': error.code, 'reason': str(error)}))
        return 1
    except (Exception, KeyboardInterrupt):
        # Raw network/keyring/subprocess exceptions may contain credentials.
        print(json.dumps({'ok': False, 'error': 'client_unavailable', 'reason': 'Client operation could not complete. Check the connection and OS credential store.'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
