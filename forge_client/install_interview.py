"""A short human interview that produces the unchanged installation intent."""
import re

from .contracts import NAME, SetupError

PROVIDERS = (('codex', 'Codex'), ('claude', 'Claude Code'), ('grok', 'Grok'))


def choice(prompt, options, *, ask, tell, default=None):
    for number, (_, label) in enumerate(options, 1):
        tell(f'  {number}. {label}')
    while True:
        answer = ask(prompt).strip().lower()
        if not answer and default is not None:
            return default
        if answer.isdigit() and 1 <= int(answer) <= len(options):
            return options[int(answer) - 1][0]
        if answer in [key for key, _ in options]:
            return answer
        tell('Choose a number from the list.')


def repositories(owner, github, *, ask, tell, previous=()):
    tell('Choose repositories under ' + owner + '.')
    try:
        available = github.installation_repositories(owner)
    except SetupError:
        available = []
        tell('Repository discovery is unavailable. Enter repository names; preflight will check access.')
    for number, name in enumerate(available, 1):
        tell(f'  {number}. {name}')
    if available:
        tell('Recent repositories shown. You can also enter names that are not listed.')
    defaults = [row['name'] for row in previous]
    while True:
        entered = ask('Repositories (numbers or names, separated by spaces)' +
                      (' [' + ', '.join(defaults) + ']' if defaults else '') + ': ').strip()
        values = re.split(r'[\s,]+', entered) if entered else defaults
        names = []
        for value in values:
            if value.isdigit():
                if not 1 <= int(value) <= len(available):
                    break
                value = available[int(value) - 1]
            if '/' not in value:
                value = owner + '/' + value
            if not NAME.fullmatch(value) or value.split('/')[0].lower() != owner.lower():
                break
            names.append(value)
        else:
            if 1 <= len(names) <= 20 and len(set(name.lower() for name in names)) == len(names):
                prior = {row['name'].lower(): row for row in previous}
                return [dict(prior.get(name.lower(), {'name': name, 'merge_policy': 'human',
                                                     'allow_workflow_edits': False})) for name in names]
        tell('Choose 1–20 different repositories belonging to ' + owner + '.')


def provider(*, ask, tell, default='codex'):
    tell('Choose a coding account. Provider usage may have a cost.')
    selected = choice('Coding account [1]: ', PROVIDERS, ask=ask, tell=tell, default=default)
    if selected != 'codex':
        tell('This account connects through the CLI before the browser handoff.')
    return {'id': selected, 'kind': 'browser'}


def host_key(ssh, *, ask, tell):
    tell('SSH must use a trusted host key. For a new server, run forge setup host-key --ssh ' + ssh +
         ' and verify the fingerprint with your provider.')
    while True:
        value = ask('Verified ED25519 fingerprint [use known_hosts]: ').strip()
        if not value or re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', value):
            return value or None
        tell('Enter the full SHA256 fingerprint, or leave blank to use an already trusted SSH key.')


def summary(value, tell):
    tell('\nInstallation plan')
    tell('  Host: ' + value['ssh'])
    tell('  Factory: ' + value['name'] + ' (' + value['hostname'] + ')')
    tell('  GitHub owner: ' + value['owner'])
    tell('  Operators: ' + ', '.join(value['operators']))
    for row in value['repositories']:
        policy = 'approval before merge' if row['merge_policy'] == 'human' else 'automatic merge'
        tell('  ' + row['name'] + ': ' + policy + '; workflow edits ' +
             ('allowed' if row['allow_workflow_edits'] else 'off'))
    tell('  Coding accounts: ' + ', '.join(row['id'] + ' (' + row['kind'] + ')' for row in value['providers']))
    tell('  SSH trust: ' + (value['host_key'] or 'existing known_hosts'))
    tell('  First merge: ' + ('private test repository' if value['smoke_target'] == 'scratch' else value['smoke_target']))
    tell('Setup installs Forge on this host, creates one GitHub App and ' +
         ('two private test repositories.' if value['smoke_target'] == 'scratch' else
          'one private publication-test repository and a test issue in ' + value['smoke_target'] + '.'))
    tell('Test repositories remain after setup. Your selected repositories enable after the test merge.')
    tell('Forge requires Review and Audit before its merges and respects existing GitHub rules.')


def advanced(value, *, ask, tell):
    options = (('hostname', 'Network hostname'), ('operators', 'Additional operators'),
               ('policies', 'Merge approval and workflow edits'), ('providers', 'More coding accounts or login methods'),
               ('smoke', 'First-merge test repository'), ('done', 'Back to summary'))
    while True:
        selected = choice('Advanced [6]: ', options, ask=ask, tell=tell, default='done')
        if selected == 'done':
            return
        if selected == 'hostname':
            value['hostname'] = ask('Network hostname [' + value['hostname'] + ']: ').strip() or value['hostname']
        elif selected == 'operators':
            value['operators'] = ask('Operator GitHub logins [' + ' '.join(value['operators']) + ']: ').split() or value['operators']
        elif selected == 'smoke':
            options_smoke = [('scratch', 'Private test repository (recommended)')]
            options_smoke.extend((row['name'], row['name']) for row in value['repositories'])
            tell('Choosing a project creates the first-merge test issue there.')
            value['smoke_target'] = choice('Test repository [1]: ', options_smoke, ask=ask, tell=tell, default='scratch')
        elif selected == 'policies':
            for row in value['repositories']:
                tell(row['name'])
                row['merge_policy'] = choice('Merge policy [' + row['merge_policy'] + ']: ',
                    (('human', 'Wait for your approval'), ('auto', 'Merge automatically after checks pass')),
                    ask=ask, tell=tell, default=row['merge_policy'])
                row['allow_workflow_edits'] = choice('Allow Forge to change GitHub Actions workflows? [' +
                    ('yes' if row['allow_workflow_edits'] else 'no') + ']: ',
                    (('no', 'No'), ('yes', 'Yes — grant workflow-write permission')),
                    ask=ask, tell=tell, default='yes' if row['allow_workflow_edits'] else 'no') == 'yes'
        elif selected == 'providers':
            tell('Current accounts: ' + ', '.join(row['id'] for row in value['providers']))
            account = provider(ask=ask, tell=tell)
            kinds = [('browser', 'Browser sign-in'), ('api_key', 'API key')]
            if account['id'] == 'claude':
                kinds.append(('token', 'Claude token'))
            account['kind'] = choice('Login method [1]: ', kinds, ask=ask, tell=tell, default='browser')
            value['providers'] = [row for row in value['providers'] if row['id'] != account['id']] + [account]


def interview(ssh, github, *, ask=input, tell=None):
    tell = tell or (lambda line: print(line, flush=True))
    actor = github.identity()
    ssh = ssh or ask('SSH destination (alias or user@host): ').strip()
    suggested = ssh.split('@')[-1]
    name = ask('Factory name [' + suggested + ']: ').strip() or suggested
    owner = ask('GitHub owner [' + actor['login'] + ']: ').strip() or actor['login']
    rows = repositories(owner, github, ask=ask, tell=tell)
    account = provider(ask=ask, tell=tell)
    fingerprint = host_key(ssh, ask=ask, tell=tell)
    value = {'version': 1, 'ssh': ssh, 'name': name, 'revision': None, 'host_key': fingerprint,
             'hostname': re.sub('[^a-z0-9-]+', '-', name.lower()).strip('-')[:63],
             'owner': owner, 'operators': [actor['login']], 'repositories': rows, 'providers': [account],
             'smoke_target': 'scratch', 'codex_parallel_auth': True}
    while True:
        summary(value, tell)
        selected = choice('Enter to check this plan, or choose a field to edit: ',
            (('name', 'Factory name'), ('owner', 'GitHub owner'), ('repositories', 'Repositories'),
             ('provider', 'Coding account'), ('advanced', 'Advanced'), ('host', 'Host identity')),
            ask=ask, tell=tell, default='check')
        if selected == 'check':
            return value
        if selected == 'name':
            old_hostname = re.sub('[^a-z0-9-]+', '-', value['name'].lower()).strip('-')[:63]
            value['name'] = ask('Factory name [' + value['name'] + ']: ').strip() or value['name']
            if value['hostname'] == old_hostname:
                value['hostname'] = re.sub('[^a-z0-9-]+', '-', value['name'].lower()).strip('-')[:63]
        elif selected in ('owner', 'repositories'):
            previous = value['repositories']
            if selected == 'owner':
                value['owner'] = ask('GitHub owner [' + value['owner'] + ']: ').strip() or value['owner']
                previous = [row for row in previous if row['name'].split('/')[0].lower() == value['owner'].lower()]
            value['repositories'] = repositories(value['owner'], github, ask=ask, tell=tell, previous=previous)
            if value['smoke_target'] not in [row['name'] for row in value['repositories']]:
                value['smoke_target'] = 'scratch'
        elif selected == 'provider':
            value['providers'] = [provider(ask=ask, tell=tell)]
        elif selected == 'host':
            value['host_key'] = host_key(ssh, ask=ask, tell=tell)
        else:
            advanced(value, ask=ask, tell=tell)
