"""One saved installation, from read-only preflight through a verified first merge."""
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import time

from .connection import connection_lock
from .contracts import SetupError, digest, fields, require
from .factory_setup import FactorySetup
from .github import GitHub
from .install import Installation, revision
from .network import Network
from .preflight import observe
from .workflow import Journal
from . import install_report


def intent(value, github):
    value = deepcopy(value)
    smoke_issue = value.pop('smoke_issue', None)
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
    owner = github.registration_owner(value['owner'])
    operators = []
    for login in value['operators']:
        resolved = github.registration_owner(login)
        require(resolved['type'] == 'User', 'invalid_operator')
        operators.append({key: resolved[key] for key in ('id', 'login')})
    repositories = []
    for row in value['repositories']:
        fields(row, ('name', 'merge_policy', 'allow_workflow_edits', *(['github_protection'] if 'github_protection' in row else [])))
        require(row.get('github_protection', 'required') in ('required', 'optional'), 'invalid_intent')
        require(row['merge_policy'] in ('human', 'auto') and type(row['allow_workflow_edits']) is bool, 'invalid_intent')
        repositories.append({'target': github.repository(row['name']), 'merge_policy': row['merge_policy'],
                             'allow_workflow_edits': row['allow_workflow_edits'],
                             **({'github_protection': row['github_protection']} if 'github_protection' in row else {})})
    require(len({row['target']['id'] for row in repositories}) == len(repositories), 'duplicate_repository')
    require(all(row['target']['owner_id'] == owner['id'] for row in repositories),
            'repository_owner_mismatch', 'Choose repositories under the selected GitHub owner for this installation.')
    for provider in value['providers']:
        fields(provider, ('id', 'kind'))
        require(provider['id'] in ('codex', 'claude', 'grok') and provider['kind'] in ('browser', 'api_key', 'token')
                and (provider['kind'] != 'token' or provider['id'] == 'claude'), 'invalid_provider')
    require(len({row['id'] for row in value['providers']}) == len(value['providers']), 'duplicate_provider')
    require(value['smoke_target'] == 'scratch' or value['smoke_target'] in [row['target']['name'] for row in repositories], 'invalid_smoke_target')
    require(smoke_issue is None or type(smoke_issue) is int and smoke_issue > 0
            and value['smoke_target'] != 'scratch', 'invalid_smoke_issue')
    return {**deepcopy(value), 'revision': revision(value['revision']), 'operators': operators,
            'owner': owner, 'repositories': repositories, 'smoke_issue': smoke_issue}


class Install:
    def __init__(self, ssh, *, github=None, installation=None, journal=None, preflight=None, clock=time.time,
                 interactive=False, notify=None):
        self.ssh = ssh
        self.github = github or GitHub()
        self.installation = installation or Installation(ssh)
        self.journal = journal or Journal('installation', ssh)
        self.preflight_observer, self.clock = preflight or observe, clock
        self.preflight_progress = preflight is None
        self.state = self.journal.read()
        self.key = 'install-' + digest({'ssh': ssh})
        self.interactive = interactive
        self.notify = notify or (lambda event: None)
        self.opened_browser_urls = set()

    def open_browser(self, url):
        if url not in self.opened_browser_urls:
            self.opened_browser_urls.add(url)
            __import__('webbrowser').open(url)

    def save(self):
        self.journal.save(self.state)

    def plan(self, supplied, *, replace=False):
        require(supplied.get('ssh') == self.ssh, 'installation_conflict')
        require(type(replace) is bool, 'invalid_replace_plan')
        if replace:
            require(self.state is not None, 'setup_not_found', 'There is no saved installation plan to replace.')
            self.load()
            preflight_fields = {'version', 'request_digest', 'intent', 'inputs_digest', 'started_at', 'stages', 'preflight', 'waivers'}
            require(not self.state['stages'] and set(self.state) <= preflight_fields,
                    'installation_started', 'Only a plan with no started installation stages can be replaced. Resume this installation instead.')
        if self.state and not replace:
            require(self.state['request_digest'] == digest(supplied), 'installation_conflict', 'The saved installation has different inputs. Use --replace-plan before installation starts, or resume the saved installation.')
            return self.status()
        resolved = intent(supplied, self.github)
        if replace:
            # Preflight pins a second, host-installation journal. The supported
            # new-run path archives it only after observing an untouched host.
            host = self.installation.execute('plan', name=resolved['name'], commit=resolved['revision'],
                                             fingerprint=resolved['host_key'], new_run=True)
            require(host['status'] == 'planned', 'installation_started',
                    'The host has installation state. Resume the saved installation instead.')
            archive = Journal('archived-installation', digest({
                'intent': self.state['inputs_digest'], 'started_at': self.state['started_at']}), root=self.journal.root)
            archive.save(self.state)
        self.state = {'version': 1, 'request_digest': digest(supplied), 'intent': resolved,
                      'inputs_digest': digest(resolved), 'started_at': self.clock(), 'stages': {}, 'preflight': None}
        self.save()
        return self.status()

    def load(self):
        require(self.state and self.state['intent']['ssh'] == self.ssh, 'setup_not_found', 'Start with forge install --intent-file FILE --ssh HOST.')
        require(self.state['inputs_digest'] == digest(self.state['intent']), 'intent_changed', 'The saved installation intent changed.')
        return self.state['intent']

    def preflight(self):
        options = {'progress': self.notify} if self.preflight_progress else {}
        value = self.preflight_observer(self.load(), self.installation, self.github, **options)
        self.state['preflight'] = {'inputs_digest': self.state['inputs_digest'], 'observed_at': self.clock(), 'report': value}
        self.save()
        return value

    def stage(self, name, observe_stage, apply_stage=None, *, complete):
        value = self.load()
        binding = digest({'intent': self.state['inputs_digest'], 'stage': name})
        saved = self.state['stages'].get(name)
        require(saved is None or saved['inputs_digest'] == binding, 'stage_changed')
        row = saved or {'inputs_digest': binding, 'status': 'pending', 'receipt': None, 'uncertain_write': False,
                        'started_at': self.clock()}
        self.state['stages'][name] = row
        try:
            observed = observe_stage()
            install_report.remember_rules(row, observed)
            if complete(observed):
                row.update(status='complete', receipt=observed, uncertain_write=False)
                row.setdefault('completed_at', self.clock())
                self.save(); return observed
            if apply_stage is None or observed.get('status') == 'write_unconfirmed':
                row.update(status='needs_user_action', receipt=observed); self.save(); return observed
            # Existing installer and network actions reconcile their pinned remote
            # journals. Repository preparation separately reconciles each saved
            # administrative write before it permits another one.
            row.update(status='applying', receipt=observed, uncertain_write=True); self.save()
            result = apply_stage(observed)
            install_report.remember_rules(row, result)
            row.update(status='complete' if complete(result) else 'pending', receipt=result,
                       uncertain_write=result.get('status') == 'write_unconfirmed')
            if row['status'] == 'complete': row.setdefault('completed_at', self.clock())
            self.save(); return result
        except SetupError as error:
            row.update(status='needs_user_action', receipt={'error': error.code, 'reason': str(error)})
            self.save()
            return {'status': 'needs_user_action', 'error': error.code, 'reason': str(error)}

    def apply(self, *, waivers=(), retry_smoke=None):
        selected = self.load()
        if self.state['stages'].get('handover', {}).get('status') == 'complete':
            self.write_report()
            return self.status()
        report = self.preflight()  # Never trust a previously passing snapshot.
        known = {row['id']: row for row in report['items']}
        require(all(name in known and known[name]['waivable'] for name in waivers), 'invalid_waiver', 'Only explicitly waivable findings can be waived.')
        blocked = [row for row in report['items'] if row['status'] == 'needs_you' and row['id'] not in waivers]
        self.state['waivers'] = sorted(waivers); self.save()
        if blocked:
            return {**self.status(), 'status': 'preflight_blocked', 'findings': blocked}
        # These local GitHub writes need only the approved intent and passing
        # preflight, not an installed host or a private-network origin.
        from .install_targets import prepare_targets
        targets = self.stage('targets', lambda: prepare_targets(self.state, self.save, self.github),
                             lambda _: prepare_targets(self.state, self.save, self.github, apply=True),
                             complete=lambda row: row.get('status') == 'targets_ready')
        if targets.get('status') != 'targets_ready': return self.status()
        host = self.stage('host', lambda: self.installation.execute('status'),
                          lambda row: self.installation.execute('resume' if row['status'] == 'interrupted' else 'apply'),
                          complete=lambda row: row.get('status') == 'installed')
        if host.get('status') != 'installed': return self.status()
        network = Network(self.installation)
        net = self.stage('network', lambda: network.execute(hostname=selected['hostname']),
                         lambda row: network.execute(hostname=selected['hostname'], apply=True),
                         complete=lambda row: row.get('status') == 'network_ready')
        if net.get('status') != 'network_ready': return self.status()
        from .install_consent import InstallConsent
        from .install_environment import Environment
        from .install_identity import configure as configure_identity
        from .install_publication import qualify
        from .install_smoke import InstallSmoke

        consent = InstallConsent(self.installation, selected, github=self.github,
                                 repositories=targets['repositories'], tell=self.notify,
                                 open_browser=self.open_browser if self.interactive else None)
        apps = self.stage('consent', consent.observe, consent.apply,
                          complete=lambda row: row.get('status') == 'consent_ready')
        if apps.get('status') != 'consent_ready': return self.status()
        factory = FactorySetup(self.installation, github=self.github)
        identity = self.stage('identity', lambda: configure_identity(factory),
                              lambda _: configure_identity(factory, apply=True),
                              complete=lambda row: row.get('status') == 'identity_ready')
        if identity.get('status') != 'identity_ready': return self.status()
        # Finish attended account consent before repository repair and image or
        # publication qualification. Existing login sessions are observed on resume.
        for provider in selected['providers']:
            self.stage('account.' + provider['id'], lambda provider=provider: self.account(factory, provider),
                       lambda _, provider=provider: self.account(factory, provider, apply=True),
                       complete=lambda row: row.get('status') == 'connected')
        if any(self.state['stages']['account.' + provider['id']]['status'] != 'complete'
               for provider in selected['providers']):
            return self.status()
        # Independent repository findings do not prevent preparing other repos.
        for target in targets['repositories']:
            if target['id'] == targets['publication']['id']: continue
            self.stage('repository.' + str(target['id']),
                       lambda target=target: self.repository(factory, target),
                       lambda _, target=target: self.repository(factory, target, apply=True),
                       complete=lambda row: row.get('status') == 'repository_ready')
        if any(row['env_tier'] == 'full' for row in report['repositories']):
            environment = Environment(self.installation)
            self.stage('environment', environment.execute,
                       lambda _: environment.execute(apply=True),
                       complete=lambda row: row.get('status') == 'environment_ready')
        else:
            self.stage('environment', lambda: {'status': 'environment_ready', 'tier': 'standard'},
                       complete=lambda row: True)
        preparation = [name for name in self.state['stages']
                       if name.startswith(('repository.', 'account.')) or name == 'environment']
        if any(self.state['stages'][name]['status'] != 'complete' for name in preparation):
            return self.status()
        # The probe repo is disposable and is never scheduled for work.
        for target in targets['repositories']:
            if target['id'] == targets['publication']['id']: continue
            self.stage('publication.' + str(target['id']),
                       lambda target=target: qualify(factory, self.state, target),
                       lambda _, target=target: qualify(factory, self.state, target, apply=True),
                       complete=lambda row: row.get('status') == 'qualified')
        if any(row['status'] != 'complete' for name, row in self.state['stages'].items()
               if name.startswith('publication.')): return self.status()
        smoke = InstallSmoke(factory, self.state, self.save)
        prepared = self.stage('smoke.prepare', smoke.prepare, lambda _: smoke.prepare(apply=True),
                              complete=lambda row: row.get('status') == 'smoke_repository_ready')
        if prepared.get('status') != 'smoke_repository_ready': return self.status()
        verified = self.stage('smoke', smoke.run, lambda _: smoke.run(apply=True, retry_proposal=retry_smoke),
                              complete=lambda row: row.get('status') in ('smoke_verified', 'handover_complete'))
        if verified.get('status') not in ('smoke_verified', 'handover_complete'): return self.status()
        self.stage('handover', smoke.handover, lambda _: smoke.handover(apply=True),
                   complete=lambda row: row.get('status') == 'handover_complete')
        if self.state['stages']['handover']['status'] == 'complete':
            self.state.setdefault('completed_at', self.clock())
            self.save()
            self.write_report()
        return self.status()

    def write_report(self):
        """Observe versions once after handover; status itself stays offline."""
        saved = install_report.storage(self.journal, self.state)
        if saved.read() is not None:
            return
        from .version import report, client_revision
        try:
            versions = report(self.ssh)
        except (SetupError, OSError, ValueError):
            # A completed factory must remain usable when the final read-only
            # version probe cannot reach the host. Never retain its error text.
            versions = {'client_revision': client_revision(), 'server_revision': None}
        versions['observed_at'] = self.clock()
        saved.save(install_report.build(self.state, versions=versions))

    def repository(self, factory, target, *, apply=False):
        selected = next((row for row in self.load()['repositories'] if row['target']['id'] == target['id']), None)
        mode = selected.get('github_protection') if selected else ('optional' if all(row.get('github_protection') == 'optional' for row in self.load()['repositories']) else None)
        if factory.call('status')['phase'] != 'active':
            result = factory.prepare_repository(target['name'], initial=False, apply=apply,
                                                 repair=self.load()['rules_repair'], github_protection=mode)
            if result.get('status') == 'planned' and not result['plan']['steps'] and not result['plan']['blockers']:
                return {'status': 'repository_ready', 'repository': target}
            return result
        identity = {key: target[key] for key in ('id', 'name')}
        result = factory.call('requirements', repository=identity)
        policy_matches = mode is None or result['baseline'].get('github_protection', 'required') == mode
        if apply:
            if not policy_matches:
                current = factory.call('github_protection', repository=identity, operation='read')
                factory.call('github_protection', repository=identity, operation='set', mode=mode,
                             revision=current['revision'], request_id=digest({
                                 'intent': self.state['inputs_digest'], 'repository': identity,
                                 'mode': mode, 'revision': current['revision']})[:32])
                result = factory.call('requirements', repository=identity)
                policy_matches = result['baseline'].get('github_protection', 'required') == mode
            result = factory.call('verify_repository', repository=identity, revision=result['revision'])
            policy_matches = mode is None or result['baseline'].get('github_protection', 'required') == mode
        ready = policy_matches and result['readiness']['access_ready'] and result['readiness']['protection_ready']
        return {'status': 'repository_ready' if ready else 'repository_changed', 'repository': target,
                'readiness': result['readiness']}

    def account(self, factory, provider, *, apply=False):
        if provider == {'id': 'codex', 'kind': 'browser'}:
            result = factory.call('device_account', provider='codex', apply=apply, retry=False)
            if result.get('url'):
                self.notify({'event': 'provider_login', **result})
            if result.get('status') == 'account_login_failed':
                result['next_action'] = {'command': ['forge', 'account', 'login', 'codex', '--ssh', self.ssh, '--retry', '--json'],
                                         'instruction': 'The device login expired or failed. Start a new login explicitly, then resume installation.'}
            return result
        connected = next((row for row in factory.call('status')['harnesses'] if row['id'] == provider['id']), {})
        if connected.get('connected'): return {'status': 'connected', 'provider': provider['id']}
        if apply and self.interactive:
            secret = None
            if provider['kind'] != 'browser':
                import getpass
                secret = getpass.getpass(provider['id'] + ' credential (hidden): ')
            factory.account(provider['id'], apply=True, kind=provider['kind'],
                            interactive=True, secret=secret)
            return self.account(factory, provider)
        return {'status': 'account_login_required', 'provider': provider['id'],
                'next_action': {'command': ['forge', 'account', 'login', provider['id'], '--ssh', self.ssh,
                                            '--kind', provider['kind']],
                                'instruction': 'Complete the provider login, then resume this installation.'}}

    def status(self):
        self.load()
        stages = deepcopy(self.state['stages'])
        complete = stages.get('handover', {}).get('status') == 'complete'
        report, report_path = None, None
        if complete:
            saved = install_report.storage(self.journal, self.state)
            report = saved.read()
            if report is not None:
                report_path = str(saved.path)
            else:
                report = install_report.build(self.state)
        pending = [{'stage': name, **(row.get('receipt') or {})}
                   for name, row in stages.items() if row['status'] != 'complete']
        for row in pending:
            if row.get('status') == 'smoke_triage_failed':
                row['next_action'] = {'instruction': 'Fix the reported planner problem, then retry this failed smoke proposal:',
                    'command': ['forge', 'install', '--ssh', self.ssh, '--resume', '--retry-smoke', row['proposal_id'], '--wait', '3600']}
        return {'status': 'installation_complete' if complete else 'installation_pending',
                'inputs_digest': self.state['inputs_digest'], 'ssh': self.ssh, 'intent': self.state['intent'],
                'stages': stages, 'pending': pending,
                'started_at': self.state['started_at'], 'completed_at': self.state.get('completed_at'),
                'elapsed_seconds': max(0, self.state.get('completed_at', self.clock()) - self.state['started_at']),
                'next_action': None if complete else ['forge', 'install', '--ssh', self.ssh, '--resume'],
                'handover_complete': complete,
                'origin': (stages.get('identity', {}).get('receipt') or {}).get('origin'),
                'report_path': str(self.journal.path) if complete else None,
                'handover_report': report, 'handover_report_path': report_path}


def interview(ssh, github, *, ask=input, tell=None):
    tell = tell or (lambda line: print(line, flush=True))
    actor = github.identity()
    ssh = ssh or ask('SSH destination (configured alias or user@host): ').strip()
    suggested = ssh.split('@')[-1]
    name = ask('Factory name [' + suggested + ']: ').strip() or suggested
    tell('Forge creates one private GitHub App. Its owner must be the same personal account or organization that owns all repositories selected here.')
    owner = ask('GitHub App owner [' + actor['login'] + ']: ').strip() or actor['login']
    operators = ask('Operator logins [' + actor['login'] + ']: ').strip().split() or [actor['login']]
    repos = ask('Repositories (OWNER/REPO, separated by spaces): ').strip().split()
    rows = []
    tell('Human merge policy waits for your approval after Forge Review and Audit. Auto merges when Forge checks and existing GitHub rules pass.')
    tell('Required protection installs GitHub branch rules; private personal repositories need GitHub Pro for this. Optional keeps Forge Review and Audit without requiring GitHub enforcement for other writers. Existing rules remain in force.')
    for name_in_repo in repos:
        mode = ask(name_in_repo + ' merge policy [human/auto; human]: ').strip() or 'human'
        workflows = ask(name_in_repo + ' allow workflow edits? [y/N]: ').strip().lower() in ('y', 'yes')
        protection = ask(name_in_repo + ' GitHub branch protection [required/optional; required]: ').strip() or 'required'
        rows.append({'name': name_in_repo, 'merge_policy': mode, 'allow_workflow_edits': workflows, 'github_protection': protection})
    providers = ask('Providers (codex, claude, grok; space-separated) [codex]: ').strip().split() or ['codex']
    accounts = []
    for provider in providers:
        default = 'token' if provider == 'claude' and shutil.which('claude') else 'browser'
        kind = ask(provider + ' login kind [browser/api_key/token; ' + default + ']: ').strip() or default
        accounts.append({'id': provider, 'kind': kind})
    hostname = re.sub('[^a-z0-9-]+', '-', name.lower()).strip('-')[:63]
    hostname = ask('Tailnet hostname [' + hostname + ']: ').strip() or hostname
    repair = 'skip' if rows and all(row['github_protection'] == 'optional' for row in rows) else (
        ask('Rules repair [apply-if-admin/print-commands/skip; apply-if-admin]: ').strip() or 'apply-if-admin')
    tell('The first-merge test runs a small issue through Build, Review, Audit and merge. Scratch creates a private test repository; choosing a project runs the test there. A separate private repository tests signed publication.')
    smoke = ask('Smoke target [scratch or chosen OWNER/REPO; scratch]: ').strip() or 'scratch'
    tell('Known SSH host keys are required. For a new VM, use forge setup host-key and verify its fingerprint with the provider.')
    fingerprint = ask('Verified ED25519 fingerprint (leave empty to use known_hosts): ').strip() or None
    return {'version': 1, 'ssh': ssh, 'name': name, 'revision': None, 'host_key': fingerprint, 'hostname': hostname,
            'owner': owner, 'operators': operators, 'repositories': rows, 'providers': accounts,
            'rules_repair': repair, 'smoke_target': smoke, 'codex_parallel_auth': True}


def execute(args):
    import sys
    require(type(args.wait) is int and 0 <= args.wait <= 3600, 'invalid_wait', 'Use --wait between 0 and 3600 seconds.')
    replace = getattr(args, 'replace_plan', False)
    require(not replace or not (args.apply or args.resume or args.status or getattr(args, 'reset', False)),
            'invalid_replace_plan', '--replace-plan only replaces an unstarted plan and runs preflight; do not combine it with --apply, --resume, --status or --reset.')
    retry_smoke = getattr(args, 'retry_smoke', None)
    require(not retry_smoke or args.resume and not getattr(args, 'reset', False), 'invalid_retry',
            'Use --retry-smoke FAILED_PROPOSAL_ID with --resume after fixing the planner failure.')
    github = GitHub()
    supplied = None
    if args.intent_file:
        path = Path(args.intent_file)
        require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 65536, 'invalid_intent')
        supplied = json.loads(path.read_text())
    elif replace or not (args.status or args.resume or args.preflight or args.apply):
        require(not args.json and sys.stdin.isatty(), 'intent_required', 'Use --intent-file for a deterministic installation plan.')
        supplied = interview(args.ssh, github)
    ssh = args.ssh or (supplied or {}).get('ssh')
    require(ssh, 'ssh_required', 'Select --ssh HOST.')
    interactive = not args.json and sys.stdin.isatty()
    notified = set()
    def event(value):
        encoded = json.dumps(value, sort_keys=True)
        if encoded in notified: return
        notified.add(encoded)
        if args.json:
            print(encoded, file=sys.stderr, flush=True)
        else:
            lines = [value] if isinstance(value, str) else [
                value[key] for key in ('instruction', 'url') if value.get(key)]
            if isinstance(value, dict) and value.get('code'):
                lines.append('One-time code: ' + value['code'])
            print('\n'.join(lines) if lines else encoded, file=sys.stderr, flush=True)
    workflow = Install(ssh, github=github, interactive=interactive, notify=event)
    with connection_lock(workflow.key, workflow.journal.root):
        if supplied is not None: workflow.plan(supplied, replace=replace)
        if args.status: return workflow.status()
        if interactive and supplied is not None and not (args.resume or args.apply or args.preflight or replace):
            report = workflow.preflight()
            blocked = [row for row in report['items'] if row['status'] == 'needs_you' and row['id'] not in args.waive]
            if blocked:
                return report
            display(report, tell=lambda line: print(line, file=sys.stderr, flush=True))
            if input('Apply this installation, including its smoke issue and configured handover? [y/N] ').strip().lower() not in ('y', 'yes'):
                args._installation_report_displayed = True
                return report
            args.apply = True
            args.wait = args.wait or 3600
        if args.resume or args.apply:
            deadline = time.monotonic() + args.wait
            previous = None
            while True:
                result = workflow.apply(waivers=args.waive, retry_smoke=retry_smoke)
                progress = {key: row['status'] for key, row in result.get('stages', {}).items()}
                if progress != previous:
                    event({'event': 'installation_progress', 'stages': progress})
                    previous = progress
                pending = result.get('pending', [])
                waiting = {'installing', 'network_installing', 'environment_installing', 'checking',
                           'smoke_pending', 'first_merge_pending', 'account_login_pending', 'account_login_saved',
                           'app_installation_required'}
                def can_wait(row):
                    return (row.get('status') in waiting or
                            row.get('status') == 'account_login_required' and bool(row.get('url') and row.get('code')))
                if (result.get('handover_complete') or result['status'] == 'preflight_blocked'
                        or time.monotonic() >= deadline
                        or not pending or any(not can_wait(row) for row in pending)):
                    return result
                time.sleep(min(5, max(0, deadline - time.monotonic())))
        return workflow.preflight()


def display(result, *, tell=None):
    """Human presentation of the same structured results used by agents."""
    import shlex
    tell = tell or (lambda line: print(line, flush=True))
    if result['status'] == 'preflight':
        for row in result['items']:
            label = row['id'].split('.')[0] + ' ' + row['repository'] if row.get('repository') else row['id']
            tell(row['status'].replace('_', ' ').ljust(23) + label + ': ' + row['detail'])
            if row['status'] == 'needs_you' and row.get('command'): tell('  ' + shlex.join(row['command']))
        if any(row['status'] == 'needs_you' for row in result['items']):
            tell('Plan saved. Resolve the prerequisites above, then rerun: forge install --ssh ' + result['host']['ssh'] + ' --preflight')
        else:
            tell('Plan saved. Apply with: forge install --ssh ' + result['host']['ssh'] + ' --apply --wait 3600')
        return
    if result['status'] == 'preflight_blocked':
        tell('Installation is waiting for these prerequisites:')
        for row in result['findings']:
            tell('  ' + row['id'] + ': ' + row['detail'])
            if row.get('command'): tell('  ' + shlex.join(row['command']))
    else:
        if result['status'].startswith('reset_'):
            tell('Forge reset complete.' if result['status'] == 'reset_complete' else 'Forge reset ' + result['status'].removeprefix('reset_') + '.')
            for effect in result.get('effects', []): tell('  ' + effect)
            if result.get('archive'): tell('Private archives: ' + json.dumps(result['archive'], sort_keys=True))
            if result.get('waiting'): tell(result['waiting'])
        else:
            tell('Installation complete.' if result.get('handover_complete') else 'Installation progress saved.')
        for name, row in result.get('stages', {}).items():
            tell('  ' + name + ': ' + row['status'].replace('_', ' '))
        for row in result.get('pending', []):
            action = row.get('next_action') or {}
            if row.get('reason'): tell(row['reason'])
            if isinstance(action, dict):
                if action.get('instruction'): tell(action['instruction'])
                if action.get('url'): tell(action['url'])
                if action.get('command'): tell(shlex.join(action['command']))
            if row.get('url'): tell(row['url'])
            if row.get('code'): tell('One-time code: ' + row['code'])
        if result.get('handover_report'):
            install_report.display(result['handover_report'], tell=tell)
        elif result.get('origin'):
            tell('Forge: ' + result['origin'])
        if result.get('handover_report_path'):
            tell('Private report: ' + result['handover_report_path'])
        if result.get('report_path'): tell('Installation journal: ' + result['report_path'])
    if result.get('next_action'): tell(shlex.join(result['next_action']))


def schemas():
    return {
        'usage': 'forge install --ssh HOST --intent-file FILE --json',
        'resume': 'forge install --ssh HOST --resume --json',
        'retry_smoke': {'command': 'forge install --ssh HOST --resume --retry-smoke FAILED_PROPOSAL_ID --wait 3600 --json',
                        'effect': 'After repair, explicitly requests one new smoke proposal for the current cleaned terminal failure. Replays reuse the same successor; prior attempts and receipts remain intact.'},
        'status': 'forge install --ssh HOST --status --json',
        'preflight': 'forge install --ssh HOST --preflight --json',
        'replace_plan': {'command': 'forge install --ssh HOST --replace-plan --intent-file FILE --preflight --json',
                         'effect': 'Archives an unstarted saved plan and its host plan after confirming the host is untouched; resolves the replacement inputs and reruns preflight without applying installation.'},
        'apply': 'forge install --ssh HOST --apply --json',
        'reset': {'plan': 'forge install --ssh HOST --reset --json',
                  'apply': 'forge install --ssh HOST --reset --apply --reset-revision DIGEST --json',
                  'effect': 'Archives the pinned Forge installation on the existing VM; retains SSH, Tailscale and GitHub objects.'},
        'device_login': {'start_or_observe': 'forge account login codex --ssh HOST --json',
                         'retry': 'forge account login codex --ssh HOST --retry --json',
                         'effect': 'Returns a browser URL and one-time code; retry starts a new attempt only after failure, expiry or disconnection.'},
        'authority': 'explicit saved host identity and local GitHub administrator; smoke-only activation, then configured handover',
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
                                   'allow_workflow_edits': {'type': 'boolean'},
                                   'github_protection': {'enum': ['required', 'optional'], 'default': 'required'}}}},
                'providers': {'type': 'array', 'minItems': 1, 'maxItems': 3, 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['id', 'kind'],
                    'properties': {'id': {'enum': ['codex', 'claude', 'grok']},
                                   'kind': {'enum': ['browser', 'api_key', 'token']}}}},
                'rules_repair': {'enum': ['apply-if-admin', 'print-commands', 'skip']},
                'smoke_target': {'type': 'string', 'description': 'scratch or a selected OWNER/REPO'},
                'smoke_issue': {'type': ['integer', 'null'], 'minimum': 1, 'description': 'Optional existing issue in the selected smoke repo; omitted creates a smoke issue'},
                'codex_parallel_auth': {'type': 'boolean', 'description': 'Allow parallel jobs with the host-managed Codex login'}
            }},
        'wait': 'Use --wait SECONDS to follow automated stages; resume retains the original installation and first-merge timer.',
        'secrets': 'Never accepted in the intent. Use forge account login with hidden input or private stdin.'}
