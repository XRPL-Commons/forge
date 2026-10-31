"""Read-only installation observations, with explicit unresolved prerequisites."""
from importlib.resources import files
import json
import shutil
from urllib.parse import quote

from .contracts import SetupError, require
from .github import run_bounded
from .rules import branch_rules, optional
from .version import client_revision
from .tailnet import executable as tailscale_executable, environment as tailscale_environment


def finding(identifier, status, detail, *, command=None, repository=None, waivable=False):
    return {'id': identifier, 'status': status, 'detail': detail, 'command': command,
            'repository': repository, 'waivable': waivable}


def repository(github, target, *, allow_workflow_edits=False, github_protection='required'):
    """Pin source evidence while reusing Forge's non-executing dependency scan."""
    from forge.factory.environments import probe
    current = github.repository(target['name'])
    require(current['id'] == target['id'] and current['name'] == target['name'] and current['branch'] == target['branch'],
            'repository_changed', 'The selected repository or default branch changed; record a new installation intent.')
    class LocalSource:
        def target(self, item): return current['branch']
        def call(self, role, item, method, suffix):
            require(method == 'GET')
            return github._request('GET', '/repos/' + current['name'] + suffix)
    environment = probe(LocalSource(), {'repo_id': current['id'], 'repo': current['name']})
    require(github_protection in ('required', 'optional'))
    def protection_read(operation, absent):
        try:
            return operation()
        except SetupError as error:
            if github_protection != 'optional' or error.code != 'github_plan_required':
                raise
            return absent
    effective = protection_read(lambda: branch_rules(github, current), [])
    classic = protection_read(lambda: optional(github, '/repos/' + current['name'] + '/branches/' + quote(current['branch'], safe='') + '/protection'), None)
    require(classic is None or isinstance(classic, dict), 'github_invalid_response')
    signatures = optional(github, '/repos/' + current['name'] + '/branches/' + quote(current['branch'], safe='') + '/protection/required_signatures') if classic else None
    workflows = optional(github, '/repos/' + current['name'] + '/contents/.github/workflows?ref=' + environment['head'])
    require(workflows is None or isinstance(workflows, list), 'github_invalid_response')
    ready = github._request('GET', '/repos/' + current['name'] + '/issues?state=open&labels=ready&per_page=100')
    require(isinstance(ready, list), 'github_invalid_response')
    sources = []
    seen = set()
    for rule in effective:
        key = (rule.get('ruleset_source_type'), rule.get('ruleset_id'))
        if key in seen: continue
        seen.add(key)
        kind, identifier = key
        editable = current['admin'] if kind == 'Repository' else False
        if kind == 'Organization':
            membership = optional(github, '/user/memberships/orgs/' + current['name'].split('/')[0]) or {}
            editable = membership.get('state') == 'active' and membership.get('role') == 'admin'
        details = github._request('GET', '/repos/' + current['name'] + '/rulesets/' + str(identifier))
        require(isinstance(details, dict), 'github_invalid_response')
        sources.append({'type': kind, 'id': identifier, 'editable': editable,
                        'conditions': details.get('conditions'), 'enforcement': details.get('enforcement')})
    return {'target': current, 'environment': environment, 'env_tier': 'full' if environment['services'] else 'standard',
            'signed_commits_required': any(row['type'] == 'required_signatures' for row in effective)
                or bool((signatures or {}).get('enabled')),
            'rule_sources': sources, 'classic_protection': classic is not None,
            'workflow_files': [row['path'] for row in workflows or [] if row.get('type') == 'file'],
            'workflow_edits_requested': allow_workflow_edits,
            'ready_issues': [row['number'] for row in ready if not row.get('pull_request')]}


def observe(intent, installation, github, *, run=run_bounded, progress=None):
    def announce(instruction, **details):
        if progress:
            progress({'event': 'preflight_progress', 'instruction': instruction, **details})
    announce('Checking local tools, GitHub sign-in and Tailscale…')
    items, repos = [], []
    for name in ('git', 'ssh', 'gh'):
        available = bool(shutil.which(name))
        items.append(finding('local.' + name, 'pass' if available else 'needs_you', name + (' is installed.' if available else ' must be installed locally.')))
    actor = github.identity()
    approved = actor in intent['operators']
    items.append(finding('local.github', 'pass' if approved else 'needs_you', 'Local GitHub login must be one of the saved operators.', command=['gh', 'auth', 'login']))
    installed = client_revision()
    items.append(finding('local.revision', 'pass' if installed == intent['revision'] else 'needs_you',
                         'Client must match the pinned installation revision.', command=['forge', 'self-update', '--revision', intent['revision']]))
    tailnet_command = None
    tailnet_detail = 'Join this computer to the intended private tailnet.'
    try:
        tailscale = tailscale_executable()
        tailnet_command = [tailscale, 'up']
        raw = run([tailscale, 'status', '--json'], timeout=15, environ=tailscale_environment())
        tailnet = json.loads(raw)
        joined = tailnet.get('BackendState') == 'Running'
    except (SetupError, ValueError, OSError) as error:
        tailnet, joined = {}, False
        if isinstance(error, SetupError) and error.code == 'tailnet_cli_missing':
            tailnet_detail = str(error)
    items.append(finding('local.tailnet', 'pass' if joined else 'needs_you', tailnet_detail, command=tailnet_command))
    collisions = [row for row in (tailnet.get('Peer') or {}).values() if row.get('HostName') == intent['hostname']]
    owner = intent['owner']
    authority = owner['type'] == 'User' and owner['id'] == actor['id']
    if owner['type'] == 'Organization':
        membership = optional(github, '/user/memberships/orgs/' + owner['login']) or {}
        authority = membership.get('state') == 'active' and membership.get('role') == 'admin'
    items.append(finding('github.owner', 'pass' if authority else 'needs_you', 'The installing operator must own the selected user account or administer the organization.',
                         command=['gh', 'api', '/user/memberships/orgs/' + owner['login']] if owner['type'] == 'Organization' else None))
    if owner['type'] == 'Organization':
        # GitHub has no complete read endpoint for all enterprise App policy decisions.
        items.append(finding('github.app_policy', 'needs_you', 'Confirm organization and enterprise policy permits creating and installing the Forge App.',
                             command=['open', 'https://github.com/organizations/' + owner['login'] + '/settings/apps'], waivable=True))
    for selected in intent['repositories']:
        name = selected['target']['name']
        announce('Checking ' + name + ': access, branch rules and test dependencies…', repository=name)
        try:
            row = repository(github, selected['target'], allow_workflow_edits=selected['allow_workflow_edits'], github_protection=selected.get('github_protection', 'required'))
            repos.append(row)
            items.append(finding('repository.' + str(row['target']['id']), 'pass' if row['target']['admin'] else 'needs_you',
                                 'Repository administration is required for preparation.', repository=name))
            items.append(finding('signing.' + str(row['target']['id']), 'pass',
                                 'Verified signatures are required; the publisher must satisfy this rule. No signing bypass is planned.' if row['signed_commits_required'] else 'No effective signed-commit requirement observed.', repository=name))
            items.append(finding('workflows.' + str(row['target']['id']), 'will_fix_automatically' if selected['allow_workflow_edits'] else 'pass',
                                 'Workflow write permission is recorded for the later App consent batch.' if selected['allow_workflow_edits'] else
                                 'Workflow edits remain disabled; ' + str(len(row['workflow_files'])) + ' workflow files observed.', repository=name))
            if row['ready_issues']:
                items.append(finding('ready.' + str(row['target']['id']), 'pass',
                                     'Already-ready issues: ' + ', '.join(map(str, row['ready_issues'])) + '. This install does not enable them.', repository=name))
        except (SetupError, ValueError) as error:
            items.append(finding('repository.' + str(selected['target']['id']), 'needs_you',
                                 str(error) if isinstance(error, SetupError) else 'Repository dependency evidence was incomplete.', repository=name))
    announce('Checking the SSH host and available resources…')
    host = installation.execute('plan', name=intent['name'], commit=intent['revision'], fingerprint=intent['host_key'])
    facts = host['host']
    full = any(row['env_tier'] == 'full' for row in repos)
    # Both fresh worker tiers use one 4 GiB cgroup: databases share that limit.
    # Keep the existing 7.5 GiB installation floor (worker, 512 MiB gateway,
    # 2 GiB host reserve and 1 GiB setup headroom). Only image disk usage grows.
    minimum = {'cpus': 2, 'memory_mib': 7680, 'free_disk_mib': 40960 if full else 20480}
    for key, needed in minimum.items():
        items.append(finding('host.' + key, 'pass' if facts[key] >= needed else 'needs_you',
                             f'{facts[key]} available; {needed} required for one worker slot and the selected environment tiers.'))
    items.append(finding('host.platform', 'pass', 'Verified Ubuntu 24.04, AppArmor, systemd, SSH identity and root/noninteractive sudo.'))
    announce('Checking server network access and private HTTPS prerequisites…')
    remote = installation.host.rpc(files('forge_client').joinpath('_preflight_host.py').read_text(), {'version': 1}, timeout=45)
    require(set(remote) == {'ok', 'userns_restricted', 'egress', 'tailnet'}, 'invalid_host_response')
    items.append(finding('host.userns', 'will_fix_automatically' if remote['userns_restricted'] == '1' else 'pass',
                         'Installer qualifies the dedicated AppArmor profile for restricted user namespaces.'))
    for name, reachable in remote['egress'].items():
        items.append(finding('egress.' + name, 'pass' if reachable else 'needs_you', 'Host TLS egress to ' + name))
    tailnet = remote['tailnet']
    joined = tailnet and tailnet.get('state') == 'Running'
    # The SSH plan verifies the target before this read-only host observation.
    # Reset preserves its Tailscale node but archives the installer journal, so
    # bind collisions to the live node identity instead of a saved origin URL.
    same_host = bool(joined and tailnet.get('node_id') and tailnet.get('dns_name')
                     and all(row.get('ID') == tailnet['node_id']
                             and row.get('DNSName', '').rstrip('.') == tailnet['dns_name'].rstrip('.')
                             for row in collisions))
    items.append(finding('network.hostname', 'pass' if not collisions or same_host else 'needs_you',
                         'Tailnet hostname is available or belongs to the verified SSH target.'))
    items.append(finding('network.certificates', 'pass' if joined and tailnet.get('cert_domains') else 'needs_you' if joined else 'will_fix_automatically',
                         'HTTPS certificate domains must be available after tailnet join.', command=['open', 'https://login.tailscale.com/admin/dns']))
    for provider in intent['providers']:
        items.append(finding('account.' + provider['id'], 'will_fix_automatically',
                             'The account login step validates the provider credential; no secret is stored in this intent.'))
    announce('Preflight checks finished.')
    return {'status': 'preflight', 'items': items, 'repositories': repos, 'minimum_resources': minimum, 'host': host}
