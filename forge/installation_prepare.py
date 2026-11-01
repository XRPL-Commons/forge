"""Server-owned preparation after privileged installation consent.

The approved plan contains identities and policy choices, never personal GitHub
credentials. Repository reads run in bounded parallel batches; configuration and
observation writes remain serialized and generation-bound. GitHub administration
stays an explicit operator task.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import math
import re
import time

from forge_client.contracts import digest, fields, repository, require
from .factory.config import load as runtime_config
from .factory.github import GitHub, GitHubUnavailable
from .install_environment import Environment
from .installation_publication import qualify
from .installation_smoke import InstallationSmoke
from .onboarding import Onboarding
from .repository_policy import protection_ready
from .repository_setup import RepositorySetup, observation_key
from .settings import Settings
from .sqlite_identity import identity_effect_lock
from .store.configuration import Configuration
from .store.runtime import required, writable

DOMAIN = 'installation_prepare'
PARALLEL_READS = 3


def validate_plan(plan, now):
    fields(plan, ('intent_digest', 'started_at', 'repositories', 'smoke_repository', 'smoke_issue',
                  'publication_repository', 'environment', 'providers'))
    require(isinstance(plan['intent_digest'], str) and re.fullmatch('[a-f0-9]{64}', plan['intent_digest']), 'invalid_intent')
    require(type(plan['started_at']) in (int, float) and math.isfinite(plan['started_at'])
            and 0 < plan['started_at'] <= now, 'invalid_installation_start')
    require(plan['environment'] in ('standard', 'full'), 'invalid_environment')
    require(isinstance(plan['repositories'], list) and 1 <= len(plan['repositories']) <= 20, 'invalid_repositories')
    for row in plan['repositories']:
        fields(row, {'target', 'merge_policy', 'allow_workflow_edits'} | ({'github_protection'} if 'github_protection' in row else set()))
        repository(row['target'])
        require(row['merge_policy'] in ('auto', 'human') and type(row['allow_workflow_edits']) is bool
                and row.get('github_protection', 'optional') in ('required', 'optional'), 'invalid_repository_policy')
    selected = [row['target'] for row in plan['repositories']]
    require(len({row['id'] for row in selected}) == len(selected)
            and len({row['name'].lower() for row in selected}) == len(selected), 'invalid_repositories')
    repository(plan['smoke_repository']); repository(plan['publication_repository'])
    for special in (plan['smoke_repository'], plan['publication_repository']):
        require(all((special['id'] == row['id']) == (special['name'].lower() == row['name'].lower())
                    for row in selected), 'repository_changed')
    require(plan['publication_repository']['id'] not in {row['id'] for row in selected + [plan['smoke_repository']]},
            'invalid_publication_probe')
    require(plan['smoke_issue'] is None or type(plan['smoke_issue']) is int and plan['smoke_issue'] > 0, 'invalid_smoke_issue')
    require(isinstance(plan['providers'], list) and 1 <= len(plan['providers']) <= 3, 'invalid_providers')
    for provider in plan['providers']:
        fields(provider, ('id', 'kind'))
        require(provider['id'] in ('codex', 'claude', 'grok') and provider['kind'] in ('browser', 'api_key', 'token'),
                'invalid_provider')
    require(len({row['id'] for row in plan['providers']}) == len(plan['providers']), 'invalid_providers')
    return deepcopy(plan)


class InstallationPrepare:
    def __init__(self, root, *, setup=None, clock=time.time):
        self.root, self.clock = root, clock
        self.setup = setup or Onboarding(root, clock=clock)
        self.store = required(root)
        self.configuration = Configuration(self.store, clock=clock)

    def saved(self, generation=None):
        generation = generation or self.setup.read()['generation']
        return self.configuration.get(DOMAIN, generation)

    @staticmethod
    def targets(plan):
        targets = [row['target'] for row in plan['repositories']]
        if plan['smoke_repository'] not in targets:
            targets.append(plan['smoke_repository'])
        return targets

    def approve(self, plan, *, setup_id, generation):
        plan = validate_plan(plan, self.clock())
        require(isinstance(setup_id, str) and re.fullmatch('[a-f0-9]{32}', setup_id), 'invalid_setup')
        with identity_effect_lock(self.store):
            data = self.setup.read()
            require(data['generation'] == generation, 'setup_changed')
            prior = self.saved(generation)
            binding = {'setup_id': setup_id, 'generation': generation, 'plan': plan}
            if prior:
                require(prior['settings']['binding'] == binding, 'preparation_changed')
                return self.view()
            require(data['phase'] == 'setup', 'setup_complete')
            require(not any(row.get('enabled') for row in self.setup.settings().load()['repositories']),
                    'repositories_already_enabled')
            consent = data.get('installation_consent') or {}
            allowed = consent.get('binding') or {}
            require(consent.get('generation') == generation and allowed.get('setup_id') == setup_id, 'consent_required')
            require(all(target in allowed.get('repositories', []) for target in
                        self.targets(plan) + [plan['publication_repository']]), 'repository_not_consented')
            require(not any(row['allow_workflow_edits'] for row in plan['repositories']) or allowed.get('workflow_edits') is True,
                    'workflow_permission_not_consented')
            stages = {'environment': {'label': 'Worker environment', 'status': 'pending'},
                      'settings': {'label': 'Confirm approved repositories', 'status': 'pending'},
                      'access': {'label': 'Refresh GitHub App access', 'status': 'pending'}}
            for provider in plan['providers']:
                stages['account.' + provider['id']] = {'label': 'Connect ' + provider['id'], 'status': 'pending'}
            for target in self.targets(plan):
                stages['repository.' + str(target['id'])] = {'label': 'Check ' + target['name'], 'status': 'pending'}
                stages['publication.' + str(target['id'])] = {'label': 'Verify publication for ' + target['name'], 'status': 'pending'}
            stages['smoke.prepare'] = {'label': 'Prepare first-merge test', 'status': 'pending'}
            value = {'binding': binding, 'status': 'pending', 'stages': stages, 'updated_at': self.clock()}
            with self.store.write() as db:
                writable(db)
                self.configuration.replace_in(db, DOMAIN, value, subject=generation, expected_revision=0)
        return self.view()

    def view(self):
        generation = self.setup.read()['generation']; saved = self.saved(generation)
        if not saved:
            return {'available': False, 'generation': generation, 'ready': False, 'stages': [], 'needs_operator': [], 'providers': []}
        value = saved['settings']; binding = value['binding']; plan = binding['plan']
        stages = [{'id': name, **deepcopy(stage)} for name, stage in value['stages'].items()]
        connected = {row['id']: row['connected'] for row in self.setup.harnesses(self.setup.read())}
        providers = [{**row, 'connected': bool(connected.get(row['id']))} for row in plan['providers']]
        if value['status'] != 'complete':
            # Account consent can finish while a repository waits for operator
            # repair. Show current readiness without claiming a recorded finish.
            for stage in stages:
                if stage['id'].startswith('account.'):
                    ready = bool(connected.get(stage['id'].removeprefix('account.')))
                    stage['status'] = 'complete' if ready else 'waiting'
                    if ready:
                        stage.pop('reason', None)
                    else:
                        stage.pop('completed_at', None)
                        stage['reason'] = 'Connect this coding account to continue the first-merge test.'
        needs = [{'repository': target, 'reason': value['stages']['repository.' + str(target['id'])].get('reason'),
                  'path': '/settings/repositories/' + str(target['id']) + '?setup=1'}
                 for target in self.targets(plan) if value['stages']['repository.' + str(target['id'])]['status'] == 'needs_operator']
        ready = value['status'] == 'complete'
        smoke = None
        saved_smoke = self.configuration.get('installation_smoke', generation)
        if ready and saved_smoke:
            approved = saved_smoke['settings']['binding']
            require(all(approved[key] == binding[key] for key in ('setup_id', 'generation'))
                    and approved['intent_digest'] == plan['intent_digest']
                    and approved['repository'] == plan['smoke_repository']
                    and approved['repositories'] == plan['repositories'], 'smoke_changed')
            smoke = {key: deepcopy(approved[key]) for key in ('repository', 'number', 'smoke_policy')}
        return {'available': True, 'generation': generation, 'setup_id': binding['setup_id'], 'ready': ready,
                'intent_digest': plan['intent_digest'], 'smoke': smoke,
                'status': 'preparation_ready' if ready else 'preparation_needs_operator' if needs or any(
                    row['status'] == 'needs_operator' for row in stages) else 'preparation_pending',
                'started_at': plan['started_at'], 'updated_at': value['updated_at'], 'stages': stages,
                'needs_operator': needs, 'providers': providers, 'repositories': deepcopy(plan['repositories']),
                'smoke_repository': deepcopy(plan['smoke_repository'])}

    def _guard(self, value):
        binding = value['binding']; data = self.setup.read()
        require(data['generation'] == binding['generation'] and data['phase'] == 'setup', 'setup_changed')
        require(not Settings(self.root).paused(), 'factory_paused')
        prior = self.saved(binding['generation'])
        require(prior and prior['settings']['binding'] == binding
                and prior['revision'] == value['_revision'], 'preparation_changed')
        return prior

    def _save(self, value):
        with identity_effect_lock(self.store):
            prior = self._guard(value)
            following = {key: part for key, part in value.items() if key != '_revision'}
            if prior['settings'] == following:
                return
            value['updated_at'] = following['updated_at'] = self.clock()
            value['_revision'] = self.configuration.replace(DOMAIN, following,
                subject=value['binding']['generation'], expected_revision=prior['revision'])

    def _stage(self, value, name, status, reason=None):
        row = value['stages'][name]
        row.update(status=status)
        if reason: row['reason'] = reason
        else: row.pop('reason', None)
        if status == 'running': row.setdefault('started_at', self.clock())
        if status == 'complete': row.setdefault('completed_at', self.clock())
        self._save(value)

    def retry(self, *, generation):
        with identity_effect_lock(self.store):
            require(self.setup.read()['generation'] == generation, 'setup_changed')
            saved = self.saved(generation)
            require(saved, 'preparation_required')
            value = saved['settings']; value['_revision'] = saved['revision']
            if value['status'] != 'complete':
                for row in value['stages'].values():
                    if row['status'] == 'needs_operator':
                        row['status'] = 'pending'; row.pop('reason', None)
                # Fresh App discovery follows an operator permission repair.
                value['stages']['access']['status'] = 'pending'
                self._save(value)
        return self.view()

    def tick(self):
        data = self.setup.read(); saved = self.saved(data['generation'])
        if not saved or saved['settings']['status'] == 'complete': return self.view()
        value = saved['settings']; value['_revision'] = saved['revision']
        binding = value['binding']; plan = binding['plan']
        require(data['generation'] == binding['generation'] and data['phase'] == 'setup', 'setup_changed')
        if Settings(self.root).paused(): return self.view()
        # The environment's own durable systemd worker builds while accounts
        # or repository checks are pending. No image switch runs in a reader.
        if value['stages']['environment']['status'] not in ('complete', 'needs_operator'):
            previous_environment = value['stages']['environment']['status']
            if previous_environment == 'pending': self._stage(value, 'environment', 'running')
            if plan['environment'] == 'standard':
                self._stage(value, 'environment', 'complete')
            else:
                environment = Environment(self.root, binding['setup_id'])
                observed = environment.view()
                if observed['status'] == 'environment_interrupted' and previous_environment != 'pending':
                    self._stage(value, 'environment', 'needs_operator', 'The database environment build stopped. Inspect the host build before retrying.')
                else:
                    if observed['status'] in ('environment_required', 'environment_interrupted'):
                        with identity_effect_lock(self.store):
                            self._guard(value)
                            observed = environment.apply()
                    self._stage(value, 'environment', 'complete' if observed['status'] == 'environment_ready' else 'waiting',
                                None if observed['status'] == 'environment_ready' else 'Building and verifying the database test environment.')
        connected = {row['id']: row['connected'] for row in self.setup.harnesses(data)}
        for provider in plan['providers']:
            ready = bool(connected.get(provider['id']))
            self._stage(value, 'account.' + provider['id'], 'complete' if ready else 'waiting',
                        None if ready else 'Connect this coding account to continue the first-merge test.')
        settings = self.setup.settings()
        if value['stages']['settings']['status'] != 'complete':
            self._stage(value, 'settings', 'running')
            self._stage(value, 'settings', 'complete')
        if value['stages']['access']['status'] != 'complete':
            self._stage(value, 'access', 'running')
            if not settings.refresh():
                self._stage(value, 'access', 'waiting', 'Waiting for the current GitHub access refresh.')
                return self.view()
            self._stage(value, 'access', 'complete')
        pending = [target for target in self.targets(plan)
                   if value['stages']['repository.' + str(target['id'])]['status'] in ('pending', 'running', 'waiting')]
        if pending:
            self._check_batch(settings, value, pending[:PARALLEL_READS])
            return self.view()
        if any(stage['status'] != 'complete' for key, stage in value['stages'].items()
               if key.startswith(('repository.', 'account.')) or key == 'environment'):
            return self.view()
        for target in self.targets(plan):
            name = 'publication.' + str(target['id'])
            if value['stages'][name]['status'] == 'needs_operator': continue
            if value['stages'][name]['status'] == 'complete': continue
            self._stage(value, name, 'running')
            with identity_effect_lock(self.store):
                self._guard(value)
                result = qualify(self.root, setup=self.setup, repository=target,
                    probe_repository=plan['publication_repository'],
                    request_id=digest({'installation': plan['intent_digest'], 'publication': target['id']})[:32], apply=True)
            self._stage(value, name, 'complete' if result['status'] == 'qualified' else 'needs_operator',
                        None if result['status'] == 'qualified' else 'Publication qualification needs attention. Check the App permissions and repository rules before retrying.')
            return self.view()
        if any(stage['status'] != 'complete' for key, stage in value['stages'].items()
               if key.startswith(('publication.', 'account.'))):
            return self.view()
        if value['stages']['smoke.prepare']['status'] == 'needs_operator': return self.view()
        self._stage(value, 'smoke.prepare', 'running')
        smoke = InstallationSmoke(self.root, setup=self.setup, clock=self.clock)
        def call(action, parameters):
            with identity_effect_lock(self.store):
                self._guard(value)
                return smoke.call(action, parameters,
                    setup_id=binding['setup_id'], generation=binding['generation'])
        issue = call('issue', {'intent_digest': plan['intent_digest'], 'repository': plan['smoke_repository'],
                              'number': plan['smoke_issue'], 'started_at': plan['started_at']})
        if issue['status'] != 'smoke_issue_ready':
            self._stage(value, 'smoke.prepare', 'waiting' if issue.get('phase') == 'controller' else 'needs_operator',
                        'Waiting for the supervisor.' if issue.get('phase') == 'controller' else 'The first-merge issue could not be confirmed. Inspect its repository before retrying.')
            return self.view()
        selected = next((r for r in plan['repositories'] if r['target'] == plan['smoke_repository']), None)
        result = call('bind', {'binding': {'intent_digest': plan['intent_digest'], 'repository': plan['smoke_repository'],
            'number': issue['number'], 'started_at': plan['started_at'], 'repositories': plan['repositories'],
            'smoke_policy': selected['merge_policy'] if selected else 'auto'}})
        if result.get('phase') == 'controller':
            self._stage(value, 'smoke.prepare', 'waiting', 'Waiting for the supervisor.')
            return self.view()
        self._stage(value, 'smoke.prepare', 'complete')
        value['status'] = 'complete'; self._save(value)
        return self.view()


    def _check_batch(self, settings, value, targets):
        config = settings.load(); generation = value['binding']['generation']
        settings_revision = digest(config)
        views = {row['id']: row for row in settings.view()['repositories']}
        selected = []
        for target in targets:
            name = 'repository.' + str(target['id'])
            row = views.get(target['id'])
            if not row or row['name'] != target['name'] or not row['access_ready']:
                self._stage(value, name, 'needs_operator', 'Grant the Forge App access to this repository, then retry preparation.')
            else:
                self._stage(value, name, 'running'); selected.append(target)
        def observe(target):
            # Per-reader settings/client and request deadline; readers never save
            # shared configuration or observations from their worker threads.
            reader = Settings(self.root, scope='setup', clock=self.clock)
            github = GitHub(reader, runtime_config(self.root, scope='setup'), clock=self.clock)
            github.deadline = time.monotonic() + 60
            item = {'repo_id': target['id'], 'repo': target['name']}
            try:
                branch = github.target(item)
                report = github.protection_report(item, {'base_ref': branch})
                require(report.get('repo_id') == target['id'] and report.get('branch') == branch, 'repository_changed')
                return report
            except (GitHubUnavailable, ValueError, OSError):
                return {'repo_id': target['id'], 'branch': None, 'available': False,
                        'observed_at': self.clock(), 'requirements': [], 'errors': [{'detail': 'Repository rules could not be verified.'}]}
        with ThreadPoolExecutor(max_workers=PARALLEL_READS) as pool:
            observations = list(pool.map(observe, selected))
        policies = RepositorySetup(settings)
        for target, report in zip(selected, observations):
            ready = protection_ready(report)
            row = {'name': target['name'], 'revision': observation_key(config, target),
                   'status': 'ready' if ready else 'needs_setup' if report.get('available') else 'unknown',
                   'started_at': self.clock(), 'report': report}
            with identity_effect_lock(self.store):
                self._guard(value)
                policies.save_observation(target['id'], {'repositories': {str(target['id']): row}},
                                          expected_generation=generation, expected_config=settings_revision)
                self._stage(value, 'repository.' + str(target['id']), 'complete' if ready else 'needs_operator',
                            None if ready else 'GitHub access or repository rules need an operator change. Forge will not change permissions or branch rules automatically.')
