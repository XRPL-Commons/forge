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
    for action in ('inspect', 'plan', 'setup', 'verify', 'enable', 'disable', 'activity', 'issues', 'issue', 'delivery', 'policy'):
        properties = {'repository': {'type': 'string', 'description': 'GitHub OWNER/REPO'},
                      'server': {'type': 'string', 'format': 'uri', 'description': 'Previously connected HTTPS origin'}}
        required = ['repository', 'server']
        if action in ('setup', 'verify', 'enable', 'disable'):
            properties.update(resume={'type': 'boolean', 'default': False},
                              wait={'type': 'integer', 'minimum': 0, 'maximum': 600, 'default': 120})
        if action == 'setup':
            properties.update(apply={'type': 'boolean', 'default': False}, enable={'type': 'boolean', 'default': False})
        if action == 'policy':
            properties.update(mode={'enum':['auto','human']},revision={'type':'string','pattern':'^[a-f0-9]{64}$'},request_id={'type':'string','pattern':'^[a-f0-9]{32}$'})
        if action == 'delivery':
            properties['after'] = {'type': 'string', 'default': '', 'description': 'Continue from next_cursor'}
            properties['task_after'] = {'type': 'string', 'default': '', 'description': 'Continue from task_next_cursor; restart when the plan changes'}
        if action == 'issues':
            properties['page'] = {'type': 'integer', 'minimum': 1, 'maximum': 10000, 'default': 1}
        if action in ('issue', 'delivery'):
            properties['number'] = {'type': 'integer', 'minimum': 1}
            required.append('number')
        commands[action] = {
            'usage': 'forge repo ' + action + ' OWNER/REPO' + (' NUMBER' if action in ('issue', 'delivery') else ' [auto|human]' if action == 'policy' else '') + ' --server HTTPS_ORIGIN --json',
            'capability': 'repositories:policy' if action == 'policy' else 'repositories:activate' if action in ('enable', 'disable') else 'repositories:prepare' if action in ('setup', 'verify') else 'repositories:read',
            'input_schema': {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': required},
        }
        if action == 'policy':
            commands[action]['read_capability'] = 'repositories:read'
        if action == 'delivery':
            commands[action]['response_version'] = 2
            commands[action]['response_description'] = ('Approved issue membership is separate from builder tasks. '
                'Each member has its complete contribution set. Tasks can span issues and use separate paging; '
                'task_next_cursor pins the current plan. Job history uses next_cursor. '
                'A ready contribution set does not mean the issue has merged.')
    return commands


def migration_commands():
    from .migration import SERVICE_USER_PATTERN
    properties = {name: {'type': 'string'} for name in ('ssh', 'source', 'root')}
    properties['service_user'] = {'type': 'string', 'pattern': '^'+SERVICE_USER_PATTERN+'$',
        'description': 'Existing Unix account owning Forge; default is the SSH account'}
    properties.update(apply={'type':'boolean'},source_revision={'type':'string','pattern':'^[0-9a-f]{40}$'})
    return {'upgrade': {
        'usage': 'forge migrate upgrade --json',
        'input_schema': {'type': 'object', 'additionalProperties': False, 'properties': properties,
                         'required': ['ssh', 'source', 'root']},
        'maintenance': True,
        'target': 'selected SSH host',
    }}


def verb(commands, name, **options):
    options.setdefault('description', options.get('help'))
    return commands.add_parser(name, **options)


def parser():
    p = argparse.ArgumentParser(prog='forge', description='Install Forge factories and connect local agents.')
    p.add_argument('--json', action='store_true', help='Structured output; never opens a browser or prompts')
    sub = p.add_subparsers(dest='command', required=True)
    install = sub.add_parser('install', help='Save a multi-repository interview, preflight, and resume installation')
    install.add_argument('--ssh', help='Saved SSH destination')
    install.add_argument('--intent-file', help='JSON interview inputs; contains no secrets')
    action = install.add_mutually_exclusive_group()
    action.add_argument('--preflight', action='store_true', help='Re-observe prerequisites without remote writes')
    action.add_argument('--apply', action='store_true', help='Apply the saved intent after a fresh successful preflight')
    action.add_argument('--resume', action='store_true', help='Re-observe saved stages and resume the same intent')
    action.add_argument('--status', action='store_true', help='Read the local stage journal')
    install.add_argument('--waive', action='append', default=[], help='Explicitly waive a finding marked waivable; repeat for each ID')
    install.add_argument('--wait', type=int, default=0, help='Follow automated stages for up to 3600 seconds; browser actions remain explicit')
    install.add_argument('--reset', action='store_true', help='Plan a clean Forge reinstall on this VM; archives the old installation and retains SSH/Tailscale')
    install.add_argument('--reset-revision', help='Exact reset plan digest; required with --reset --apply')
    install.add_argument('--retry-smoke', metavar='FAILED_PROPOSAL_ID', help='With --resume, explicitly request new smoke triage after fixing a failed proposal')
    install.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    version = sub.add_parser('version', help='Show this client revision and compare an installed server')
    version.add_argument('--ssh', help='Saved SSH destination; read the actual installed server revision')
    version.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    update = sub.add_parser('self-update', help='Reinstall a pinned client revision using uv')
    update.add_argument('--revision', help='Full commit SHA; defaults to main resolved once')
    update.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    account = sub.add_parser('account', help='Connect or renew a coding account, including on active factories').add_subparsers(dest='account_command', required=True)
    login = account.add_parser('login', help='Sign in to a provider on the selected server')
    login.add_argument('provider', choices=('codex', 'claude', 'grok'))
    login.add_argument('--ssh', required=True)
    login.add_argument('--kind', choices=('browser', 'api_key', 'token'), default='browser')
    login.add_argument('--secret-stdin', action='store_true', help='Read the provider credential privately from stdin')
    login.add_argument('--retry', action='store_true', help='Start a new Codex device login after the previous attempt failed or expired')
    login.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    fresh = sub.add_parser('setup', help='Install a fresh factory from this computer').add_subparsers(dest='setup_command', required=True)
    qualification = verb(fresh, 'qualify-boundary', help='Run disposable native worker isolation canaries on the selected host')
    qualification.add_argument('--ssh', required=True)
    qualification.add_argument('--image', help='Exact sha256 image ID; defaults to the installed worker image')
    qualification.add_argument('--apply', action='store_true', help='Create and remove disposable qualification resources')
    qualification.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    signing = verb(fresh, 'qualify-publication', help='Verify signed publication using an explicitly selected scratch repository')
    signing.add_argument('--ssh', required=True)
    signing.add_argument('--service-user', default='forge', help='Existing Unix service account; defaults to forge')
    signing.add_argument('--source', default='/home/forge/forge', help='Installed Forge checkout on the selected host')
    signing.add_argument('--root', default='/home/forge/.local/share/forge', help='Existing Forge runtime directory on the selected host')
    signing.add_argument('--repo', required=True, help='Connected target owner/repository')
    signing.add_argument('--probe-repo', required=True, help='Existing private scratch repository in the same Controller installation')
    signing.add_argument('--target-branch', help='Target branch; defaults to the repository default')
    signing.add_argument('--probe-ref', help='Existing scratch branch; defaults to the scratch default')
    signing.add_argument('--check-workflow', action='store_true', help='Test workflow-file publication with an existing qualified receipt and current grants only')
    signing.add_argument('--receipt', help='Re-read an existing host probe receipt; never repeats GitHub writes')
    signing.add_argument('--apply', action='store_true', help='Write only the selected scratch ref and host qualification evidence')
    signing.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    roles = verb(fresh, 'qualify-roles', help='Qualify selected roles through the installed provider without publishing work')
    roles.add_argument('--ssh', required=True)
    roles.add_argument('--repo', required=True, help='Existing OWNER/REPO to read through the mirror')
    roles.add_argument('--issue', required=True, type=int)
    roles.add_argument('--role', action='append', choices=('build','review','audit','recovery','triage','strategist','discussion','steward'),
                       help='Role to qualify; repeat to select several. Defaults to all eight roles')
    roles.add_argument('--apply', action='store_true', help='Run one bounded provider invocation per selected role with disposable work')
    roles.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    diagnostic = verb(fresh, 'qualification-diagnostic', help='Export one private qualification diagnostic to a new local file')
    diagnostic.add_argument('--ssh', required=True)
    diagnostic.add_argument('--id', required=True, help='Opaque diagnostic_id from a failed qualification report')
    diagnostic.add_argument('--output', required=True, help='New local file; created with permissions 0600, never printed')
    diagnostic.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    for action in ('new', 'plan', 'apply', 'status', 'resume', 'host-key', 'network', 'identity'):
        command = verb(fresh, action, aliases=['sign-in'] if action == 'identity' else [],
                                   help={'new':'Walk through installation on a fresh VM', 'plan':'Observe the host and save a pinned installation plan', 'apply':'Apply the saved host installation plan', 'status':'Read saved installation progress', 'resume':'Resume the saved installation without repeating completed stages', 'host-key':'Discover an SSH fingerprint for you to verify', 'network':'Configure private Tailscale HTTPS access', 'identity':'Configure GitHub operator sign-in'}[action])
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
        command = verb(fresh, action, help={'continue':'Resume guided setup from its saved step', 'inspect':'Read factory setup, App and account status', 'apps':'Register or install the selected GitHub role App', 'account':'Connect or renew a coding account', 'repository':'Prepare and verify the initial repository', 'check':'Verify accounts, isolation and repository readiness', 'start':'Start the factory after successful verification', 'pilot':'Track an existing issue through a verified merge'}[action])
        command.add_argument('--ssh', required=True, help='The saved administrative SSH destination')
        command.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        command.add_argument('--wait', type=int, default=0, help='Watch for at most 0–600 seconds')
        if action not in ('continue', 'inspect'):
            command.add_argument('--apply', action='store_true', help='Apply this explicit setup step')
        if action == 'apps':
            command.add_argument('--role', choices=('build', 'review', 'audit', 'controller'))
            command.add_argument('--owner', help='GitHub organization or user that owns the Forge App')
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
    connect.add_argument('--policy', action='store_true', help='Request permission to set repository merge policy; requires explicit browser approval')
    connect.add_argument('--triage', action='store_true', help='Request explicit approval to plan issue changes and compose, change, dissolve or resume deliveries')
    connect.add_argument('--resume', action='store_true')
    connect.add_argument('--wait', type=int, default=0)
    auth = sub.add_parser('auth', help='Check this computer’s connection to a Forge server').add_subparsers(dest='auth_command', required=True)
    auth.add_parser('status', help='Show whether the saved connection is still valid').add_argument('server')
    sub.add_parser('disconnect', help='Forget a saved Forge server connection').add_argument('server')
    requirements = sub.add_parser('requirements', help='Show what a repository needs before Forge can work on it')
    requirements.add_argument('server'); requirements.add_argument('repository')
    repo = sub.add_parser('repo', help='Inspect, prepare and enable a repository').add_subparsers(dest='repo_command', required=True)
    for action in ('inspect', 'plan', 'setup', 'verify', 'enable', 'disable', 'activity', 'issues', 'issue', 'delivery', 'policy'):
        command = verb(repo, action, help={'inspect':'Read repository readiness and effective policy', 'plan':'Preview repository preparation without writes', 'setup':'Prepare repository access and verify rules', 'verify':'Refresh access and verify effective branch protection', 'enable':'Enable automation after verification', 'disable':'Disable repository automation', 'activity':'Read repository activity', 'issues':'List repository issues', 'issue':'Read one issue', 'delivery':'Read issue delivery and task history', 'policy':'Read or set the repository merge policy'}[action])
        command.add_argument('repository', help='GitHub OWNER/REPO')
        command.add_argument('--server', required=True, help='Previously connected HTTPS Forge origin')
        command.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
        if action == 'policy':
            command.add_argument('mode',nargs='?',choices=('auto','human'),help='Omit to inspect the effective policy')
            command.add_argument('--revision',help='Exact runtime revision to compare before writing')
            command.add_argument('--request-id',help='Stable 32-character hexadecimal ID for replaying an exact policy decision')
        if action in ('issue', 'delivery'):
            command.add_argument('number', type=int)
        if action == 'delivery':
            command.add_argument('--after', default='', help='Continue execution history from its next_cursor')
            command.add_argument('--task-after', default='', help='Continue from task_next_cursor; restart when the plan changes')
        if action == 'issues':
            command.add_argument('--page', type=int, default=1)
        if action in ('setup', 'verify', 'enable', 'disable'):
            command.add_argument('--resume', action='store_true', help='Resume the saved operation with the same options')
            command.add_argument('--wait', type=int, default=120, help='Bounded wait for each Forge operation (0–600 seconds)')
        if action == 'setup':
            command.add_argument('--apply', action='store_true', help='Apply the local preparation plan')
            command.add_argument('--enable', action='store_true', help='Enable automation after verification')
    triage = sub.add_parser('triage', help='Request a read-only proposal for every open issue in a repository')
    triage.add_argument('repository'); triage.add_argument('--server', required=True)
    triage.add_argument('--request-id', required=True, help='Stable request identity for safe replay')
    triage.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    planning = sub.add_parser('planning', help='Inspect repository notes, edit proposals and explicitly approve changes')
    planning.add_argument('repository'); planning.add_argument('--server', required=True)
    planning.add_argument('--proposal'); planning.add_argument('--row'); planning.add_argument('--after')
    planning.add_argument('--action-file', help='JSON operation: rules, note, edit or accept; preview with planning first')
    planning.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    from .delivery import add_parser as delivery_parser
    delivery_parser(sub)
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
    identity = server.add_parser('identity', help='Migrate an existing installation to one App and trusted operator sign-in')
    identity.add_argument('identity_command', choices=('plan','apps','sign-in','tailscale'))
    for name in ('ssh','source','root','service-user'):
        identity.add_argument('--'+name, required=True)
    identity.add_argument('--revision', help='Exact revision returned by the identity plan; required for apps')
    identity.add_argument('--mode', choices=('tailscale','github'), default='tailscale')
    identity.add_argument('--secret-stdin', action='store_true', help='Read the same-App sign-in secret privately from stdin')
    identity.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    migration = sub.add_parser('migrate', help='Apply a schema upgrade to an installation that already uses SQLite')
    migration.add_argument('migration_command', choices=('upgrade',))
    for name in ('ssh','source','root'):
        migration.add_argument('--'+name)
    migration.add_argument('--apply', action='store_true', help='Authorize the maintenance window and schema upgrade')
    migration.add_argument('--service-user', help='Run remote maintenance as this existing Unix service account; default is the SSH account')
    migration.add_argument('--source-revision', help='Full commit SHA of the release to upgrade to')
    migration.add_argument('--json', action='store_true', default=argparse.SUPPRESS)
    sub.add_parser('commands', help='List every command as JSON for agents')
    sub.add_parser('skill', help='Print the bundled Forge repository agent skill')
    return p


def execute(args):
    if args.command == 'install':
        if args.reset:
            require(args.ssh and not (args.resume or args.status or args.preflight or args.intent_file or getattr(args, 'retry_smoke', None)),
                    'invalid_reset', 'Use --reset --ssh HOST, then --reset --apply --reset-revision DIGEST.')
            from .installer_reset import execute as reset
            return reset(args)
        require(args.reset_revision is None, 'invalid_reset', '--reset-revision requires --reset.')
        from .installation import execute as install
        return install(args)
    if args.command == 'version':
        from .version import report
        return report(args.ssh)
    if args.command == 'self-update':
        from .version import update
        return update(revision=args.revision)
    if args.command == 'account':
        if args.provider == 'codex' and args.kind == 'browser' and (args.json or args.retry):
            from .factory_setup import FactorySetup
            from .install import Installation
            require(not args.secret_stdin, 'invalid_account', 'Browser sign-in does not accept a secret.')
            workflow = Installation(args.ssh)
            with connection_lock(workflow.key, workflow.journal.root):
                return FactorySetup(workflow).call('device_account', provider='codex', apply=True, retry=args.retry)
        require(not args.retry, 'invalid_account', '--retry is for Codex browser sign-in.')
        from .factory_setup import execute as account_login
        args.setup_command = 'account'
        args.apply = True
        return account_login(args)
    if args.command == 'setup':
        if args.setup_command == 'qualification-diagnostic':
            from .qualification_diagnostics import execute as export_diagnostic
            return export_diagnostic(args)
        if args.setup_command == 'qualify-roles':
            from .role_qualification import execute
            return execute(args)
        if args.setup_command == 'qualify-publication':
            from .publication_qualification import execute as qualify_publication
            return qualify_publication(args)
        if args.setup_command == 'qualify-boundary':
            from .boundary_qualification import execute as qualify_boundary
            return qualify_boundary(args)
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
            if not any((args.name, args.revision, args.host_key, args.new_run)):
                from .installation import execute as install_factory
                from types import SimpleNamespace
                return install_factory(SimpleNamespace(ssh=args.ssh, json=False, intent_file=None,
                    status=False, resume=False, preflight=False, apply=False, wait=0, waive=[]))
            return wizard(args)
        return install(args)
    if args.command == 'migrate':
        from .migration import execute as migrate
        return migrate(args)

    if args.command == 'skill':
        from importlib.resources import files
        return {'skill': files('forge_client').joinpath('skills/forge-repositories/SKILL.md').read_text()}
    if args.command == 'server':
        if args.server_command == 'identity':
            from .server_identity import execute as identity_migration
            return identity_migration(args)
        from .bootstrap import setup
        return setup(args)
    if args.command == 'commands':
        from .delivery import commands as delivery_commands
        from forge.planning_contract import commands as planning_commands
        from .install import schemas
        from .installation import schemas as installation_schemas
        return {'commands': ['install', 'install --preflight', 'install --apply', 'install --resume', 'install --status', 'version', 'self-update', 'account login', 'setup qualify-boundary', 'setup qualify-publication', 'setup qualify-roles', 'setup qualification-diagnostic', 'setup new', 'setup plan', 'setup apply', 'setup status', 'setup resume', 'setup host-key', 'setup network', 'setup identity', 'setup sign-in', 'setup continue', 'setup inspect', 'setup apps', 'setup account', 'setup repository', 'setup check', 'setup start', 'setup pilot', 'connect', 'connect --resume', 'migrate upgrade', 'auth status', 'disconnect', 'requirements', 'repo', 'triage', 'planning', 'delivery', 'skill', 'server setup', 'server setup --apply', 'server setup --resume', 'server identity plan', 'server identity apps', 'server identity sign-in', 'server identity tailscale'],
                'installation_commands': installation_schemas(),
                'schema_version': 1, 'repository_commands': repository_commands(), 'factory_setup_commands': schemas(), 'delivery_commands':delivery_commands(),
                'planning_commands': planning_commands(),
                'migration_commands': migration_commands(),
                'publication_qualification': {'command':'setup qualify-publication', 'authority':'explicit SSH host',
                    'required':['ssh','repo','probe_repo'], 'optional':['target_branch','probe_ref','receipt','check_workflow','service_user','source','root'],
                    'default':'read-only plan', 'apply':'scratch-only probe or read-only receipt import',
                    'repository_creation':False, 'permission_grants':False, 'automation_activation':False},
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
            require(not (args.read_only and (args.policy or args.triage)), 'invalid_scope', 'Choose read-only access or request policy or triage writes.')
            requested = list(CAPABILITIES[:1] if args.read_only else CAPABILITIES[:3])
            if args.policy: requested.append('repositories:policy')
            if args.triage: requested.append('repositories:triage')
            value = client.begin(args.name, requested, scope)
            if not args.json:
                print('Approve code ' + value['user_code'] + ' at ' + value['verification_uri_complete'])
                webbrowser.open(value['verification_uri_complete'])
                return client.resume(args.wait or 600)
            return {'status': 'authorization_pending', **value}
        if args.command == 'auth':
            return client.status()
        if args.command == 'disconnect':
            return client.disconnect()
        if args.command in ('triage','planning'):
            from pathlib import Path
            target = {k:v for k,v in GitHub().repository(args.repository).items() if k in ('id','name')}
            if args.command == 'triage':
                return client.request('planning',target,operation='action',payload={'action':'triage','request_id':args.request_id})
            if args.action_file:
                payload=json.loads(Path(args.action_file).read_text())
                require(isinstance(payload,dict),'invalid_planning_operation','Provide a JSON planning operation.')
                return client.request('planning',target,operation='action',payload=payload)
            payload={k:v for k,v in {'proposal_id':args.proposal,'row_id':args.row,'after':args.after}.items() if v is not None}
            return client.request('planning',target,operation='read',payload=payload)
        if args.command == 'delivery':
            from .delivery import execute as delivery
            target={k:v for k,v in GitHub().repository(args.repository).items() if k in ('id','name')}
            return delivery(client,target,args)
        if args.command == 'repo':
            from .workflow import Workflow
            github = GitHub()
            target = {k: v for k, v in github.repository(args.repository).items() if k in ('id', 'name')}
            if args.repo_command == 'policy':
                from .merge_policy import execute as merge_policy
                return merge_policy(client,target,args.mode,revision=args.revision,request_id=args.request_id)
            if args.repo_command == 'delivery':
                return client.read(target, 'delivery', args.number, after=args.after, task_after=args.task_after)
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
        if args.command == 'install' and not args.json:
            from .installation import display
            display(result)
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
        from .role_qualification import RoleQualificationFailed
        details = {'report': error.report} if isinstance(error, RoleQualificationFailed) else {}
        print(json.dumps({'ok': False, 'error': error.code, 'reason': str(error), **details}))
        return 1
    except (Exception, KeyboardInterrupt):
        # Raw network/keyring/subprocess exceptions may contain credentials.
        print(json.dumps({'ok': False, 'error': 'client_unavailable', 'reason': 'Client operation could not complete. Check the connection and OS credential store.'}))
        return 1


if __name__ == '__main__':
    sys.exit(main())
