"""Saved installation interview and resumable coordination of existing CLI stages.

The consent and smoke stages remain explicit boundaries until the single-App
workflow ships. This coordinator never starts or enables a repository.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import time

from .connection import connection_lock
from .contracts import NAME, SetupError, digest, fields, require
from .factory_setup import FactorySetup
from .github import GitHub
from .install import Installation, revision
from .network import Network
from .preflight import observe
from .workflow import Journal


def intent(value, github):
    fields(value, ('version', 'ssh', 'name', 'revision', 'host_key', 'hostname', 'owner', 'operators',
                   'repositories', 'providers', 'rules_repair', 'smoke_target', 'codex_parallel_auth'))
    require(value['version'] == 1 and isinstance(value['ssh'], str), 'invalid_intent')
    require(isinstance(value['name'], str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}', value['name']), 'invalid_intent')
    require(value['host_key'] is None or isinstance(value['host_key'], str) and re.fullmatch(r'SHA256:[A-Za-z0-9+/]{43}', value['host_key']), 'invalid_host_key')
    require(isinstance(value['hostname'], str) and re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', value['hostname']), 'invalid_hostname')
    require(value['rules_repair'] in ('apply-if-admin', 'print-commands', 'skip') and type(value['codex_parallel_auth']) is bool, 'invalid_intent')
    require(isinstance(value['operators'], list) and 1 <= len(value['operators']) <= 20, 'invalid_intent')
    require(isinstance(value['repositories'], list) and 1 <= len(value['repositories']) <= 20, 'invalid_intent')
    require(isinstance(value['providers'], list) and 1 <= len(value['providers']) <= 3, 'invalid_intent')
    operators = []
    for login in value['operators']:
        resolved = github.registration_owner(login)
        require(resolved['type'] == 'User', 'invalid_operator')
        operators.append({key: resolved[key] for key in ('id', 'login')})
    repositories = []
    for row in value['repositories']:
        fields(row, ('name', 'merge_policy', 'allow_workflow_edits'))
        require(row['merge_policy'] in ('human', 'auto') and type(row['allow_workflow_edits']) is bool, 'invalid_intent')
        repositories.append({'target': github.repository(row['name']), 'merge_policy': row['merge_policy'],
                             'allow_workflow_edits': row['allow_workflow_edits']})
    require(len({row['target']['id'] for row in repositories}) == len(repositories), 'duplicate_repository')
    for provider in value['providers']:
        fields(provider, ('id', 'kind'))
        require(provider['id'] in ('codex', 'claude', 'grok') and provider['kind'] in ('browser', 'api_key', 'token')
                and (provider['kind'] != 'token' or provider['id'] == 'claude'), 'invalid_provider')
    require(len({row['id'] for row in value['providers']}) == len(value['providers']), 'duplicate_provider')
    require(value['smoke_target'] == 'scratch' or value['smoke_target'] in [row['target']['name'] for row in repositories], 'invalid_smoke_target')
    return {**deepcopy(value), 'revision': revision(value['revision']), 'operators': operators,
            'owner': github.registration_owner(value['owner']), 'repositories': repositories}


class Install:
    def __init__(self, ssh, *, github=None, installation=None, journal=None, preflight=observe, clock=time.time):
        self.ssh = ssh
        self.github = github or GitHub()
        self.installation = installation or Installation(ssh)
        self.journal = journal or Journal('installation', ssh)
        self.preflight_observer, self.clock = preflight, clock
        self.state = self.journal.read()
        self.key = 'install-' + digest({'ssh': ssh})

    def save(self):
        self.journal.save(self.state)

    def plan(self, supplied):
        require(supplied.get('ssh') == self.ssh, 'installation_conflict')
        if self.state:
            require(self.state['request_digest'] == digest(supplied), 'installation_conflict', 'The saved installation has different inputs; resume it or choose a new host.')
            return self.status()
        resolved = intent(supplied, self.github)
        self.state = {'version': 1, 'request_digest': digest(supplied), 'intent': resolved,
                      'inputs_digest': digest(resolved), 'started_at': self.clock(), 'stages': {}, 'preflight': None}
        self.save()
        return self.status()

    def load(self):
        require(self.state and self.state['intent']['ssh'] == self.ssh, 'setup_not_found', 'Start with forge install --intent-file FILE --ssh HOST.')
        require(self.state['inputs_digest'] == digest(self.state['intent']), 'intent_changed', 'The saved installation intent changed.')
        return self.state['intent']

    def preflight(self):
        value = self.preflight_observer(self.load(), self.installation, self.github)
        self.state['preflight'] = {'inputs_digest': self.state['inputs_digest'], 'observed_at': self.clock(), 'report': value}
        self.save()
        return value

    def stage(self, name, observe_stage, apply_stage=None, *, complete):
        value = self.load()
        binding = digest({'intent': self.state['inputs_digest'], 'stage': name})
        saved = self.state['stages'].get(name)
        require(saved is None or saved['inputs_digest'] == binding, 'stage_changed')
        row = saved or {'inputs_digest': binding, 'status': 'pending', 'receipt': None, 'uncertain_write': False}
        self.state['stages'][name] = row
        try:
            observed = observe_stage()
            if complete(observed):
                row.update(status='complete', receipt=observed, uncertain_write=False); self.save(); return observed
            if apply_stage is None or observed.get('status') == 'write_unconfirmed':
                row.update(status='needs_user_action', receipt=observed); self.save(); return observed
            # Existing installer and network actions reconcile their pinned remote
            # journals. Repository preparation separately reconciles each saved
            # administrative write before it permits another one.
            row.update(status='applying', receipt=observed, uncertain_write=True); self.save()
            result = apply_stage(observed)
            row.update(status='complete' if complete(result) else 'pending', receipt=result,
                       uncertain_write=result.get('status') == 'write_unconfirmed')
            self.save(); return result
        except SetupError as error:
            row.update(status='needs_user_action', receipt={'error': error.code, 'reason': str(error)})
            self.save()
            return {'status': 'needs_user_action', 'error': error.code, 'reason': str(error)}

    def apply(self, *, waivers=()):
        selected = self.load()
        report = self.preflight()  # Never trust a previously passing snapshot.
        known = {row['id']: row for row in report['items']}
        require(all(name in known and known[name]['waivable'] for name in waivers), 'invalid_waiver', 'Only explicitly waivable findings can be waived.')
        blocked = [row for row in report['items'] if row['status'] == 'needs_you' and row['id'] not in waivers]
        self.state['waivers'] = sorted(waivers); self.save()
        if blocked:
            return {**self.status(), 'status': 'preflight_blocked', 'findings': blocked}
        host = self.stage('host', lambda: self.installation.execute('status'),
                          lambda row: self.installation.execute('resume' if row['status'] == 'interrupted' else 'apply'),
                          complete=lambda row: row.get('status') == 'installed')
        if host.get('status') != 'installed': return self.status()
        network = Network(self.installation)
        net = self.stage('network', lambda: network.execute(hostname=selected['hostname']),
                         lambda row: network.execute(hostname=selected['hostname'], apply=True),
                         complete=lambda row: row.get('status') == 'network_ready')
        if net.get('status') != 'network_ready': return self.status()
        # The single-App consent receiver and smoke handover belong to Run 5.
        # Existing installations may already have their identity and Apps.
        try:
            factory = FactorySetup(self.installation, github=self.github)
            view = factory.call('status')
            apps_ready = all(row['registered'] for row in view['roles'].values())
        except SetupError as error:
            view = {'status': 'needs_user_action', 'error': error.code}
            apps_ready = False
        if not apps_ready:
            self.stage('consent', lambda: {'status': 'consent_required', 'reason': 'Single-App consent is a later installation stage. Existing forge setup commands remain available.'},
                       complete=lambda row: False)
            return self.status()
        self.stage('consent', lambda: {'status': 'existing_identity_and_apps', 'instance': view['instance']}, complete=lambda row: True)
        for row in selected['repositories']:
            target = row['target']
            name = 'repository.' + str(target['id'])
            self.stage(name, lambda target=target: factory.prepare_repository(target['name'], initial=False),
                       lambda observed, target=target: factory.prepare_repository(target['name'], apply=True, initial=False,
                           repair=selected['rules_repair']),
                       complete=lambda result: result.get('status') == 'repository_ready')
        for provider in selected['providers']:
            connected = next((row for row in factory.call('status')['harnesses'] if row['id'] == provider['id']), {})
            self.stage('account.' + provider['id'], lambda connected=connected, provider=provider:
                       {'status': 'connected' if connected.get('connected') else 'account_login_required',
                        'command': ['forge', 'account', 'login', provider['id'], '--ssh', self.ssh, '--kind', provider['kind']]},
                       complete=lambda row: row['status'] == 'connected')
        if any(row['env_tier'] == 'full' for row in report['repositories']):
            self.stage('environment', lambda: {'status': 'environment_required', 'tier': 'full',
                'reason': 'Full database environment installation is not yet available through forge install. This stage remains pending; no repositories are enabled.',
                'command': ['forge', 'install', '--ssh', self.ssh, '--status']}, complete=lambda row: False)
        self.stage('handover', lambda: {'status': 'smoke_required', 'reason': 'Single-App smoke verification and policy application are deferred. Real repositories remain disabled.',
                                       'repositories': [{'repository': row['target']['name'], 'merge_policy': row['merge_policy']} for row in selected['repositories']]},
                   complete=lambda row: False)
        return self.status()

    def status(self):
        self.load()
        return {'status': 'installation_pending', 'inputs_digest': self.state['inputs_digest'],
                'ssh': self.ssh, 'intent': self.state['intent'], 'stages': deepcopy(self.state['stages']),
                'next_action': ['forge', 'install', '--ssh', self.ssh, '--resume'],
                'handover_complete': False}


def interview(ssh, github, *, ask=input, tell=print):
    actor = github.identity()
    ssh = ssh or ask('SSH destination (configured alias or user@host): ').strip()
    suggested = ssh.split('@')[-1]
    name = ask('Factory name [' + suggested + ']: ').strip() or suggested
    owner = ask('GitHub App owner [' + actor['login'] + ']: ').strip() or actor['login']
    operators = ask('Operator logins [' + actor['login'] + ']: ').strip().split() or [actor['login']]
    repos = ask('Repositories (OWNER/REPO, separated by spaces): ').strip().split()
    rows = []
    for name_in_repo in repos:
        mode = ask(name_in_repo + ' merge policy [human/auto; human]: ').strip() or 'human'
        workflows = ask(name_in_repo + ' allow workflow edits? [y/N]: ').strip().lower() in ('y', 'yes')
        rows.append({'name': name_in_repo, 'merge_policy': mode, 'allow_workflow_edits': workflows})
    providers = ask('Providers (codex, claude, grok; space-separated) [codex]: ').strip().split() or ['codex']
    accounts = []
    for provider in providers:
        default = 'token' if provider == 'claude' and shutil.which('claude') else 'browser'
        kind = ask(provider + ' login kind [browser/api_key/token; ' + default + ']: ').strip() or default
        accounts.append({'id': provider, 'kind': kind})
    hostname = re.sub('[^a-z0-9-]+', '-', name.lower()).strip('-')[:63]
    hostname = ask('Tailnet hostname [' + hostname + ']: ').strip() or hostname
    repair = ask('Rules repair [apply-if-admin/print-commands/skip; apply-if-admin]: ').strip() or 'apply-if-admin'
    smoke = ask('Later smoke target [scratch or chosen OWNER/REPO; scratch]: ').strip() or 'scratch'
    tell('Known SSH host keys are required. For a new VM, use forge setup host-key and verify its fingerprint with the provider.')
    fingerprint = ask('Verified ED25519 fingerprint (leave empty to use known_hosts): ').strip() or None
    return {'version': 1, 'ssh': ssh, 'name': name, 'revision': None, 'host_key': fingerprint, 'hostname': hostname,
            'owner': owner, 'operators': operators, 'repositories': rows, 'providers': accounts,
            'rules_repair': repair, 'smoke_target': smoke, 'codex_parallel_auth': True}


def execute(args):
    import sys
    github = GitHub()
    supplied = None
    if args.intent_file:
        path = Path(args.intent_file)
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536, 'invalid_intent')
        supplied = json.loads(path.read_text())
    elif not (args.status or args.resume or args.preflight or args.apply):
        require(not args.json and sys.stdin.isatty(), 'intent_required', 'Use --intent-file for a deterministic installation plan.')
        supplied = interview(args.ssh, github)
    ssh = args.ssh or (supplied or {}).get('ssh')
    require(ssh, 'ssh_required', 'Select --ssh HOST.')
    workflow = Install(ssh, github=github)
    with connection_lock(workflow.key, workflow.journal.root):
        if supplied is not None: workflow.plan(supplied)
        if args.status: return workflow.status()
        if args.resume or args.apply: return workflow.apply(waivers=args.waive)
        return workflow.preflight()


def schemas():
    return {
        'usage': 'forge install --ssh HOST --intent-file FILE --json',
        'resume': 'forge install --ssh HOST --resume --json',
        'status': 'forge install --ssh HOST --status --json',
        'preflight': 'forge install --ssh HOST --preflight --json',
        'apply': 'forge install --ssh HOST --apply --json',
        'authority': 'explicit saved host identity and local GitHub administrator; no repository activation',
        'intent_schema': {
            'type': 'object', 'additionalProperties': False,
            'required': ['version', 'ssh', 'name', 'revision', 'host_key', 'hostname', 'owner', 'operators',
                         'repositories', 'providers', 'rules_repair', 'smoke_target', 'codex_parallel_auth'],
            'properties': {
                'version': {'const': 1}, 'ssh': {'type': 'string'}, 'name': {'type': 'string'},
                'revision': {'type': ['string', 'null'], 'description': 'Exact commit, or null to resolve main once'},
                'host_key': {'type': ['string', 'null'], 'description': 'Verified ED25519 fingerprint; null uses known_hosts'},
                'hostname': {'type': 'string'}, 'owner': {'type': 'string'},
                'operators': {'type': 'array', 'minItems': 1, 'items': {'type': 'string'}},
                'repositories': {'type': 'array', 'minItems': 1, 'maxItems': 20, 'items': {
                    'type': 'object', 'additionalProperties': False,
                    'required': ['name', 'merge_policy', 'allow_workflow_edits'],
                    'properties': {'name': {'type': 'string'}, 'merge_policy': {'enum': ['human', 'auto']},
                                   'allow_workflow_edits': {'type': 'boolean'}}}},
                'providers': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['id', 'kind'],
                    'properties': {'id': {'enum': ['codex', 'claude', 'grok']},
                                   'kind': {'enum': ['browser', 'api_key', 'token']}}}},
                'rules_repair': {'enum': ['apply-if-admin', 'print-commands', 'skip']},
                'smoke_target': {'type': 'string', 'description': 'scratch or a selected OWNER/REPO; executed at the later smoke stage'},
                'codex_parallel_auth': {'type': 'boolean', 'description': 'Saved preference for the later account consent stage'}
            }},
        'deferred': ['single_app_consent', 'full_environment_remote_install', 'smoke', 'merge_policy_handover', 'enable'],
        'secrets': 'Never accepted in the intent. Use forge account login with hidden input or private stdin.'}
