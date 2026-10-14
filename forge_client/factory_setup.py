"""CLI coordination after host/network/identity setup; all operations resumable.

Only the initial factory setup uses administrative SSH. Further repositories
use the existing scoped client connection and repository workflow.
"""
from datetime import datetime
import json
import shlex
import shutil
import subprocess
import sys
import time
import webbrowser

from .connection import connection_lock
from .contracts import SetupError, require, connection_identity
from .github import GitHub
from .install import Installation, ROOT, SOURCE
from .network import payload
from .preparation import Preparation
from .ssh_host import environment, pinned_options


class FactorySetup:
    def __init__(self, workflow, *, github=None):
        self.workflow = workflow
        self.github = github or GitHub()
        self.state = workflow.journal.read()
        require(self.state and self.state.get('origin'), 'setup_not_found', 'Finish host, network and sign-in setup first.')
        self.workflow.host.fingerprint = self.state.get('host_key')
        if not self.state.get('factory_actor'):
            self.state['factory_actor'] = self.github.identity()
            self.save()

    def save(self):
        self.workflow.journal.save(self.state)

    def call(self, action, **parameters):
        binding = self.state.get('factory_binding')
        if not binding and action != 'status':
            self.call('status'); binding = self.state['factory_binding']
        reply = self.workflow.host.rpc(payload('_factory_host.py'), {
            'version': 1, 'intent': self.state['intent'], 'setup': {
                'version': 1, 'setup_id': self.state['intent']['id'], 'actor_id': self.state['factory_actor']['id'],
                'generation': binding['generation'] if binding else None, 'action': action, 'parameters': parameters}}, timeout=270)
        require(set(reply) == {'ok', 'result'} and reply['ok'] is True and isinstance(reply['result'], dict),
                'invalid_host_response', 'The host returned an invalid setup result.')
        result = reply['result']
        if 'instance' in result:
            observed = {k: result[k] for k in ('instance', 'generation')}
            require(binding is None or binding == observed, 'setup_changed', 'The installation or setup generation changed.')
            self.state['factory_binding'] = observed
            self.state['factory_status'] = {k: result[k] for k in ('status', 'revision', 'phase')}
            if result['phase'] == 'active': self.state['milestones'].setdefault('factory_started', self.workflow.clock())
            self.save()
        return result

    def apps(self, *, role=None, owner=None, apply=False):
        view = self.call('status')
        require(view['phase'] == 'setup', 'setup_complete', 'Use the scoped repository commands for an active factory.')
        role = role or next((r for r in ('build', 'review', 'audit', 'controller') if not view['roles'][r]['registered']), None)
        if not role:
            return self.call('refresh') if apply else view
        require(role in view['roles'], 'invalid_role', 'Choose build, review, audit or controller.')
        chosen = self.github.registration_owner(owner) if owner else self.state.get('factory_owner')
        require(chosen, 'owner_required', 'Choose --owner with the GitHub organization or user that will own all four Apps.')
        require(not self.state.get('factory_owner') or self.state['factory_owner'] == chosen,
                'owner_changed', 'Continue with the same GitHub App owner.')
        self.state['factory_owner'] = chosen; self.save()
        if not apply:
            return {'status': 'planned', 'role': role, 'owner': chosen,
                    'next_action': {'instruction': 'Use --apply to prepare the signed-in GitHub registration handoff.'}}
        return self.call('app', role=role, owner=chosen)

    def prepare_repository(self, name, *, apply=False, initial=True, repair='apply-if-admin'):
        view = self.call('status')
        require(view['phase'] == 'setup', 'setup_complete', 'Use forge repo setup after initial setup.')
        resolved = self.github.repository(name)
        target = {k: resolved[k] for k in ('id', 'name')}
        if initial:
            require(not self.state.get('first_repository') or self.state['first_repository'] == target,
                    'repository_changed', 'Resume with the same first repository.')
            self.state['first_repository'] = target
        self.save()
        remote = SetupRepository(self)
        if apply:
            self.call('refresh')
            current = remote.requirements(target)
            remote.verify(target, current['revision'])
        observed = remote.requirements(target)
        binding = self.state['factory_binding']
        preparation = Preparation(self.github, remote, instance=binding['instance'], generation=binding['generation'],
                                  apps=connection_identity(observed), repair=repair,
                                  progress=lambda phase, step: self.repository_progress(phase, step, target=target))
        writes = self.state.setdefault('repository_writes', {})
        uncertain = writes.get(str(target['id'])) or (self.state.get('repository_write') if self.state.get('first_repository') == target else None)
        if uncertain:
            if uncertain['kind'] in ('github.ruleset.repair_bypass', 'github.classic.repair_bypass'):
                from .rules import confirmed
                ready = confirmed(self.github, resolved, uncertain)
            else:
                ready = observed['readiness']['protection_ready'] if uncertain['kind'] == 'github.ruleset.ensure_baseline' else (
                    observed['roles'][uncertain['role']]['access'] == 'ready')
            if not ready:
                return {'status': 'write_unconfirmed', 'next_action': {'instruction': 'Inspect the previous GitHub write, then resume. It will not be repeated blindly.'}}
            writes.pop(str(target['id']), None)
            if self.state.get('first_repository') == target: self.state.pop('repository_write', None)
            self.save()
        result = preparation.setup(name, apply=apply)
        if apply and result['status'] in ('ready', 'enabled'):
            if initial: self.call('select', repository=target)
            self.state['milestones'].setdefault('repository_prepared', self.workflow.clock()); self.save()
            result.update(status='repository_ready', next_action={'instruction': 'Verify coding accounts and repository, then explicitly Start Forge.'})
        return result

    def repository_progress(self, phase, step, *, target=None):
        if target is None or self.state.get('first_repository') == target:
            self.state['repository_write'] = step if phase == 'started' else None
        if target is not None:
            self.state.setdefault('repository_writes', {})[str(target['id'])] = step if phase == 'started' else None
        self.save()

    def account(self, provider, *, apply=False, kind='browser', secret=None, interactive=False):
        require(provider in ('codex', 'claude', 'grok'), 'invalid_provider', 'Choose codex, claude or grok.')
        require(kind in ('browser', 'api_key', 'token'))
        view = self.call('status')
        require(view['phase'] in ('setup', 'active'), 'setup_busy', 'Finish the current factory transition before signing in.')
        if not apply or kind == 'browser' and not interactive:
            return {'status': 'account_login_required', 'provider': provider,
                    'next_action': {'command': ['forge', 'account', 'login', provider, '--ssh', self.workflow.ssh],
                                    'instruction': 'Run the guided account login in a terminal, or use --kind api_key --secret-stdin --apply for a private noninteractive credential.'}}
        if kind == 'browser':
            self.login_terminal(provider)
            result = self.call('status')
            require(any(h['id'] == provider and h['connected'] for h in result['harnesses']),
                    'account_login_incomplete', 'Provider login did not complete; resume this account step.')
        else:
            result = self.call('account', provider=provider, kind=kind, secret=secret)
        self.state['milestones'].setdefault('account_connected', self.workflow.clock()); self.save()
        return result

    def login_terminal(self, provider):
        # This is a fixed command owned by the CLI, never a shell line from server output.
        ssh = shutil.which('ssh')
        require(ssh, 'ssh_missing', 'Install OpenSSH before connecting a provider.')
        args = ['runuser', '-u', 'forge', '--', 'env', '-i', 'PATH=/usr/sbin:/usr/bin:/sbin:/bin',
                'HOME=/home/forge', 'LANG=C.UTF-8', 'XDG_RUNTIME_DIR=/run/user/$(id -u forge)',
                SOURCE + '/bin/forge', 'setup', 'login', provider, '--root', ROOT]
        # The UID is resolved by a fixed host expression; no user input is shell code.
        command = shlex.join(args).replace("'XDG_RUNTIME_DIR=/run/user/$(id -u forge)'", '"XDG_RUNTIME_DIR=/run/user/$(id -u forge)"')
        remote = 'if [ "$(id -u)" = 0 ]; then exec ' + command + '; else exec sudo -n ' + command + '; fi'
        with pinned_options(self.workflow.ssh, self.state.get('host_key')) as options:
            result = subprocess.run([ssh, '-tt', '-a', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', 'ClearAllForwardings=yes', '-o', 'ForwardX11=no', '-o', 'PermitLocalCommand=no',
                *options, self.workflow.ssh, remote], env=environment(), timeout=900)
        require(result.returncode == 0, 'account_login_incomplete', 'Provider login did not complete; resume this account step.')

    def check(self, *, start=False, apply=False, wait=0):
        require(type(wait) is int and 0 <= wait <= 600)
        view = self.call('status')
        if view['phase'] == 'active': return view
        if not apply:
            return {**view, 'next_action': {'instruction': 'Use --apply to Start Forge.' if start else 'Use --apply to verify accounts, isolation and repository; this uses small real model calls.'}}
        if start:
            require(view['can_start'], 'verification_required', 'Run forge setup check --apply and resolve its checks before Start.')
        if not view['operation']:
            view = self.call('start' if start else 'check', revision=view['revision'])
        deadline = time.monotonic() + wait
        while view['operation'] and time.monotonic() < deadline:
            self.workflow.sleep(min(2, max(0, deadline - time.monotonic())))
            view = self.call('status')
        return view


    def pilot(self, *, name=None, number=None, apply=False, wait=0):
        require(type(wait) is int and 0 <= wait <= 600)
        view = self.call('status')
        require(view['phase'] == 'active', 'factory_not_started', 'Explicitly Start Forge before selecting the first merge issue.')
        target = self.state.get('first_repository')
        if name:
            observed = self.github.repository(name)
            chosen = {k: observed[k] for k in ('id', 'name')}
            require(target is None or target == chosen, 'repository_changed', 'The first repository changed.')
            target = chosen
        require(target, 'repository_required', 'Choose the first repository.')
        prior = self.state.get('pilot')
        number = number or (prior or {}).get('number')
        require(type(number) is int and number > 0, 'issue_required', 'Choose --issue with the issue to complete normally.')
        binding = {'repository': target, 'number': number}
        require(prior is None or prior == binding, 'pilot_changed', 'Resume the same pilot issue; retries keep the original timer.')
        self.state['first_repository'] = target
        self.state['pilot'] = binding; self.save()
        if self.state.get('first_merge'):
            return {'status': 'first_merge_complete', **self.state['first_merge']}
        self.github.pilot_issue(target, number, ready_label=view['ready_label'] if apply else None)
        deadline = time.monotonic() + wait
        while True:
            proof = self.call('pilot', repository=target, number=number, started_at=self.state['started_at'])
            if proof['status'] == 'merged':
                candidate = proof['candidate']
                remote = self.github.merged_pull(target, candidate['pull_number'])
                require(remote['merged'] and remote['head_sha'] == candidate['head_sha']
                        and remote['merge_sha'] == candidate['merge_sha'],
                        'merge_unverified', 'GitHub does not confirm the exact candidate and merge receipt yet.')
                merged_at = datetime.fromisoformat(remote['merged_at'].replace('Z', '+00:00')).timestamp()
                require(self.state['started_at'] <= merged_at <= self.workflow.clock() + 60,
                        'merge_unverified', 'The merge timestamp does not belong to this setup run.')
                from .metrics import first_merge_timing
                receipt = {'repository': target, 'number': number, 'candidate': candidate,
                           'evidence': proof['evidence'], 'attempts': proof['attempts'],
                           'timing': first_merge_timing(self.state, merged_at)}
                self.state['first_merge'] = receipt
                self.state['milestones']['first_merge'] = merged_at
                self.save()
                return {'status': 'first_merge_complete', **receipt}
            if time.monotonic() >= deadline:
                return {**proof, 'status': 'first_merge_pending', 'setup_id': self.state['intent']['id'],
                        'setup_elapsed_seconds': max(0, self.workflow.clock() - self.state['started_at']),
                        'next_action': {'instruction': 'Follow the issue in Forge. Resolve requested decisions or retries there, then resume this same pilot command; its timer and issue stay fixed.'}}
            self.workflow.sleep(min(2, max(0, deadline - time.monotonic())))


class SetupRepository:
    """Preparation's standard contract, scoped to this initial SSH-authorized setup."""
    def __init__(self, factory): self.factory = factory

    def authorize(self, instance, generation, target, capability):
        require(self.factory.state['factory_binding'] == {'instance': instance, 'generation': generation}, 'setup_changed')
        return self.factory.call('authorize', instance=instance, repository=target, capability=capability)

    def requirements(self, target):
        return self.factory.call('requirements', repository=target)

    def verify(self, target, revision):
        return self.factory.call('verify_repository', repository=target, revision=revision)


def execute(args, *, workflow=None, github=None):
    workflow = workflow or Installation(args.ssh)
    with connection_lock(workflow.key, workflow.journal.root):
        factory = FactorySetup(workflow, github=github)
        action = args.setup_command
        if action == 'inspect': return factory.call('status')
        if action == 'apps': return factory.apps(role=args.role, owner=args.owner, apply=args.apply)
        if action == 'repository':
            name = args.repository or (factory.state.get('first_repository') or {}).get('name')
            require(name, 'repository_required', 'Choose --repository OWNER/REPO.')
            return factory.prepare_repository(name, apply=args.apply)
        if action == 'account':
            require(not args.secret_stdin or args.kind != 'browser' and args.apply,
                    'invalid_account', '--secret-stdin needs --apply and --kind api_key or token.')
            secret = sys.stdin.read(16385).strip() if args.secret_stdin else None
            if args.apply and args.kind != 'browser' and not args.secret_stdin:
                require(not args.json and sys.stdin.isatty(), 'secret_required', 'Use --secret-stdin for noninteractive account credentials.')
                import getpass
                secret = getpass.getpass('Provider credential (hidden): ')
            return factory.account(args.provider, apply=args.apply, kind=args.kind, secret=secret,
                                   interactive=not args.json and sys.stdin.isatty())
        if action in ('check', 'start'):
            return factory.check(start=action == 'start', apply=args.apply, wait=args.wait)
        if action == 'pilot':
            return factory.pilot(name=args.repository, number=args.issue, apply=args.apply, wait=args.wait)
        raise SetupError('invalid_action', 'Unknown factory setup action.')


def wizard(workflow, *, ask=input, tell=print, open_browser=webbrowser.open):
    with connection_lock(workflow.key, workflow.journal.root):
        return _wizard(workflow, ask=ask, tell=tell, open_browser=open_browser)


def _wizard(workflow, *, ask, tell, open_browser):
    tell('Continue factory setup. Progress and the first-merge timer are saved.')
    factory = FactorySetup(workflow)
    view = factory.call('status')
    if view['phase'] != 'active':
        view = factory.call('refresh')
        pending_roles = [role for role in ('build', 'review', 'audit', 'controller')
                         if not (view['roles'][role]['registered'] and view['roles'][role]['status'] == 'ok')]
        owner = ((factory.state.get('factory_owner') or {}).get('login')
                 or ask('GitHub organization or user that will own the four Apps: ').strip()) if pending_roles else None
        for role in pending_roles:
            if view['roles'][role]['registered'] and view['roles'][role]['status'] == 'ok':
                continue
            _open_app_step(factory, role, owner, tell=tell, open_browser=open_browser)
            while True:
                choice = ask(role.title() + ' App: [c]heck, [r]eopen browser, [s]ave and exit [c]: ').strip().lower()
                if choice in ('s', 'save', 'exit'):
                    tell('Progress is saved. Resume with forge setup continue --ssh ' + shlex.quote(workflow.ssh))
                    return view
                if choice in ('r', 'reopen'):
                    _open_app_step(factory, role, owner, tell=tell, open_browser=open_browser)
                elif choice in ('', 'c', 'check'):
                    view = factory.call('refresh')
                    observed = view['roles'][role]
                    if observed['registered'] and observed['status'] == 'ok':
                        tell(role.title() + ' App registration and installation access verified.')
                        break
                    tell(role.title() + ' App is not ready yet. '
                         + ('Registration is saved; complete installation on your chosen repository.'
                            if observed['registered'] else 'Registration has not completed.'))
                else:
                    tell('Choose check, reopen browser, or save and exit.')
        view = factory.call('refresh')
        if not all(r['status'] == 'ok' for r in view['roles'].values()):
            tell('Some Apps are missing installation access. Complete their GitHub installation, then resume.')
            return view
        if not any(h['connected'] for h in view['harnesses']):
            provider = ask('Coding provider (codex, claude or grok): ').strip()
            tell('The CLI will run the provider login on the host; follow its browser instructions.')
            factory.account(provider, apply=True, interactive=True)
        repo = (factory.state.get('first_repository') or {}).get('name') or ask('First repository (OWNER/REPO): ').strip()
        plan = factory.prepare_repository(repo)
        tell(json.dumps(plan, indent=2))
        if ask('Apply this repository setup using your local GitHub access? [y/N] ').strip().lower() not in ('y', 'yes'):
            return plan
        prepared = factory.prepare_repository(repo, apply=True)
        if prepared['status'] != 'repository_ready': return prepared
        tell('Verification makes small real model calls and checks worker isolation.')
        if ask('Verify now? [y/N] ').strip().lower() not in ('y', 'yes'): return prepared
        view = factory.check(apply=True, wait=600)
        if not view['can_start']: return view
        if ask('Start Forge and enable automation for this repository? [y/N] ').strip().lower() not in ('y', 'yes'): return view
        view = factory.check(start=True, apply=True, wait=120)
        if view['phase'] != 'active': return view
    tell('Forge is active. The final milestone is a normal issue reaching a verified merge.')
    prior = factory.state.get('pilot')
    name = None if factory.state.get('first_repository') else ask('First repository (OWNER/REPO): ').strip()
    number = prior['number'] if prior else int(ask('Issue number to complete (existing open issue): ').strip())
    if not prior and ask('Mark this issue ready for Forge? [y/N] ').strip().lower() not in ('y', 'yes'):
        return view
    return factory.pilot(name=name, number=number, apply=not bool(prior), wait=120)


def _open_app_step(factory, role, owner, *, tell, open_browser):
    result = factory.apps(role=role, owner=owner, apply=True)
    if result.get('install_url'):
        tell(role.title() + ': registration is saved. Install this existing App on your chosen repository.')
    else:
        tell(role.title() + ': sign in to Forge as ' + factory.state['factory_actor']['login']
             + ', approve App registration on GitHub, and install it on your chosen repository.')
        tell('If passkey verification leaves a blank GitHub registration form, finish verification, '
             'then choose reopen browser here. Do not fill in the blank form manually.')
    tell('The CLI handles setup and verification; you complete the browser approvals. '
         'Return here and choose check when finished.')
    link = result.get('url') or result.get('install_url')
    if link:
        tell(link); open_browser(link)


def schemas():
    result = {}
    for action in ('continue', 'inspect', 'apps', 'account', 'repository', 'check', 'start', 'pilot'):
        properties = {'ssh': {'type': 'string'}}
        if action in ('check', 'start', 'pilot'):
            properties['wait'] = {'type': 'integer', 'minimum': 0, 'maximum': 600, 'default': 0}
        required = ['ssh']
        if action not in ('continue', 'inspect'): properties['apply'] = {'type': 'boolean', 'default': False}
        if action == 'apps':
            properties.update(role={'enum': ['build', 'review', 'audit', 'controller']}, owner={'type': 'string'})
        if action == 'account':
            properties.update(provider={'enum': ['codex', 'claude', 'grok']}, kind={'enum': ['browser', 'api_key', 'token'], 'default': 'browser'},
                              secret_stdin={'type': 'boolean', 'default': False, 'description': 'Private stdin, never a credential argument'})
            required.append('provider')
        if action in ('repository', 'pilot'): properties['repository'] = {'type': 'string'}
        if action == 'pilot': properties['issue'] = {'type': 'integer', 'minimum': 1}
        result[action] = {'usage': 'forge setup ' + action + ' --ssh HOST --json',
                          'authority': 'saved installation SSH authority; GitHub admin credentials stay local',
                          'input_schema': {'type': 'object', 'additionalProperties': False, 'properties': properties, 'required': required},
                          'interactive': action == 'continue', 'json_prompts': False}
    return result
