"""Operator progress and durable continuation of an already approved installation."""
from copy import deepcopy
import time

from forge_client.contracts import digest, fields, require, SetupError
from .installation_smoke import InstallationSmoke
from .onboarding import Onboarding
from .settings import Settings
from .sqlite_identity import identity_effect_lock, audit_in
from .store.configuration import Configuration
from .store.runtime import required, writable


class InstallationWeb:
    def __init__(self, root, *, setup=None, clock=time.time, smoke=None):
        self.root, self.clock = root, clock
        self.setup = setup or Onboarding(root, clock=clock)
        self.store = required(root)
        self.configuration = Configuration(self.store, clock=clock)
        self.smoke = smoke or InstallationSmoke(root, setup=self.setup, clock=clock)

    def row(self, domain, generation):
        return self.configuration.get(domain, generation)

    def call(self, action, binding, **parameters):
        return self.smoke.call(action, parameters, setup_id=binding['setup_id'], generation=binding['generation'])

    def view(self):
        data = self.setup.read(); generation = data['generation']; now = self.clock()
        progress = self.row('installation_progress', generation)
        saved = self.row('installation_smoke', generation)
        request = self.row('installation_web', generation)
        mirror = progress['settings'] if progress else {}
        operation = request['settings'] if request else None
        bound = saved['settings']['binding'] if saved else None
        result = {'available': bool(progress or saved or data.get('installation_consent')), 'generation': generation,
                  'complete': bool(saved and saved['settings']['phase'] == 'complete'), 'observed_at': now,
                  'started_at': bound['started_at'] if bound else mirror.get('started_at'),
                  'updated_at': mirror.get('updated_at'), 'source': 'cli', 'state': mirror.get('state', 'waiting'),
                  'stages': deepcopy(mirror.get('stages', [])), 'smoke': None, 'operation': None, 'actions': []}
        if not mirror and bound:
            result['stages'] = [
                {'id': 'consent', 'label': 'GitHub App registered', 'status': 'complete' if data.get('apps') else 'pending'},
                {'id': 'account', 'label': 'Coding account connected', 'status': 'complete' if data.get('provider_revision') else 'pending'},
                *[{'id': key, 'label': label,
                'status': {'passed': 'complete', 'failed': 'failed', 'checking': 'running'}.get(
                    data.get('checks', {}).get(check), 'pending')}
                for key, label, check in (('github', 'GitHub access checks', 'github'),
                    ('provider', 'Coding account verification', 'provider'),
                    ('runner', 'Worker isolation verification', 'runner'))]]
        if operation:
            result['operation'] = {key: operation[key] for key in ('status', 'updated_at', 'last_seen_at', 'error') if key in operation}
            result.update(source='server', updated_at=operation['updated_at'], state=operation['status'])
        # CLI progress is historical evidence, not a heartbeat or a claim its process is alive.
        result['service'] = self.setup.service_status()
        if not bound:
            if result['available']:
                result['reason'] = 'The CLI is preparing the server and account connections. Its last reported steps are shown below.'
            return result
        observed = self.call('status', bound)
        smoke = {key: observed[key] for key in ('status', 'phase', 'reason', 'operator_status', 'attempts', 'proposal_id') if key in observed}
        target = bound['repository']; prefix = 'https://github.com/' + target['name']
        smoke['issue_url'] = prefix + '/issues/' + str(bound['number'])
        candidate = observed.get('candidate')
        if candidate and candidate.get('pull_number'):
            smoke['pull_url'] = prefix + '/pull/' + str(candidate['pull_number'])
        if observed.get('operator_status'):
            smoke.setdefault('reason', observed['operator_status']['reason'])
        if 'reason' not in smoke:
            smoke['reason'] = {'smoke_merged': 'The test pull request merged. Finish setup to enable your selected repositories.',
                'handover_complete': 'The first merge is verified and your selected repositories are enabled.'}.get(
                    observed['status'], 'Waiting for the first-merge test to reach the next stage.')
        smoke['phase'] = smoke.get('phase') or self.smoke_phase(bound, observed)
        result['smoke'] = smoke
        # The server owns these two stages; a CLI report cannot complete them.
        result['stages'] = [s for s in result['stages'] if s['id'] not in ('smoke', 'handover')]
        result['stages'] += [
            {'id': 'smoke', 'label': 'First merged issue', 'status': 'complete' if observed['status'] in ('smoke_merged', 'handover_complete')
             else 'failed' if observed['status'] == 'smoke_triage_failed' else 'waiting', 'reason': smoke['reason']},
            {'id': 'handover', 'label': 'Enable selected repositories', 'status': 'complete' if result['complete'] else 'pending'}]
        result['repositories'] = [row['target'] for row in bound['repositories']]
        if result['complete']:
            result['state'] = 'complete'
        elif data['phase'] != 'resetting' and (not operation or operation['status'] in ('blocked', 'failed', 'complete')):
            if observed['status'] == 'smoke_triage_failed':
                result['actions'] = [{'id': 'retry', 'label': 'Retry first-merge test', 'proposal_id': observed['proposal_id']}]
            else:
                result['actions'] = [{'id': 'continue', 'label': 'Finish setup' if observed['status'] == 'smoke_merged' else 'Continue setup'}]
        return result

    def smoke_phase(self, binding, observed):
        if observed['status'] == 'handover_complete': return 'complete'
        if observed['status'] == 'smoke_merged': return 'handover'
        with self.store.read_snapshot() as db:
            if observed.get('unit_id'):
                unit = db.execute('SELECT state FROM units WHERE id=?', (observed['unit_id'],)).fetchone()
                if unit: return unit['state']
            run = db.execute('SELECT status FROM repository_triage_runs WHERE repository_id=? AND requested_at>=? '
                             'ORDER BY requested_at DESC LIMIT 1', (binding['repository']['id'], binding['started_at'])).fetchone()
        return 'planning' if run else 'waiting_to_start'

    def request(self, value, *, actor_id):
        require(isinstance(value, dict), 'invalid_request')
        fields(value, ('generation', 'action', 'proposal_id') if value.get('action') == 'retry' else ('generation', 'action'))
        require(value['action'] in ('continue', 'retry'), 'invalid_action')
        with identity_effect_lock(self.store):
            data = self.setup.read(); generation = data['generation']
            require(value['generation'] == generation and data['phase'] in ('setup', 'active'), 'setup_changed')
            require(not Settings(self.root).paused(), 'factory_paused')
            saved = self.row('installation_smoke', generation)
            require(saved and saved['settings']['phase'] != 'complete', 'smoke_binding_required')
            binding = saved['settings']['binding']
            prior = self.row('installation_web', generation)
            if prior and prior['settings']['status'] in ('queued', 'running', 'waiting'):
                return self.view()
            if value['action'] == 'retry':
                observed = self.call('status', binding)
                require(observed['status'] == 'smoke_triage_failed'
                        and value['proposal_id'] == observed['proposal_id'], 'smoke_retry_changed')
            operation = {'status': 'queued', 'updated_at': self.clock(), 'action': value['action'],
                         'proposal_id': value.get('proposal_id'), 'setup_id': binding['setup_id'], 'generation': generation}
            with self.store.write() as db:
                writable(db)
                self.configuration.replace_in(db, 'installation_web', operation, subject=generation,
                                             expected_revision=prior['revision'] if prior else 0)
                audit_in(db, self.clock, 'installation_continue_requested', details={
                    'actor_id': actor_id, 'generation': generation, 'action': value['action']})
        return self.view()

    def tick(self):
        # Match setup/reset and smoke lock order: identity first, controller
        # inside smoke effects. Re-read the generation only after acquiring it.
        with identity_effect_lock(self.store):
            self._tick()

    def _tick(self):
        data = self.setup.read(); generation = data['generation']
        prior = self.row('installation_web', generation)
        if not prior or prior['settings']['status'] not in ('queued', 'running', 'waiting'):
            return
        operation = prior['settings']
        if operation['status'] != 'queued' and self.clock() - operation.get('last_seen_at', operation['updated_at']) < 10:
            return
        saved = self.row('installation_smoke', generation)
        if not saved or data['phase'] == 'resetting' or Settings(self.root).paused(): return
        binding = saved['settings']['binding']
        require(operation['setup_id'] == binding['setup_id']
                and operation['generation'] == binding['generation'] == generation, 'setup_changed')
        try:
            if data.get('operation'):
                result = {'status': 'checking'}
            elif data['phase'] == 'setup':
                if data.get('selected') != binding['repository']:
                    self.setup.select(binding['repository']['id'])
                # A fresh operator request may retry failed checks once. A
                # failure reached by this running request waits for attention.
                require(not data.get('error') or operation['status'] == 'queued', 'readiness_failed')
                view = self.setup.view()
                self.setup.queue('start' if view['can_start'] else 'check', view['revision'])
                result = {'status': 'checking'}
            else:
                if operation['action'] == 'retry':
                    result = self.call('retry', binding, proposal_id=operation['proposal_id'])
                    if result['status'] == 'smoke_retry_queued':
                        operation = {**operation, 'action': 'continue'}
                if operation['action'] == 'continue':
                    result = self.call('launch', binding)
                if result['status'] == 'smoke_merged':
                    result = self.call('handover', binding)
            status = 'complete' if result['status'] == 'handover_complete' else 'blocked' if result['status'] in (
                'smoke_triage_failed', 'smoke_triage_required', 'write_unconfirmed') else 'waiting'
            fingerprint = digest({key: result.get(key) for key in
                                  ('status', 'phase', 'reason', 'operator_status', 'candidate', 'attempts', 'proposal_id')})
            changed = operation.get('progress') != fingerprint or operation['status'] != status
            following = {**operation, 'status': status, 'last_seen_at': self.clock(), 'progress': fingerprint,
                         'updated_at': self.clock() if changed else operation['updated_at']}
        except (ValueError, RuntimeError, OSError) as error:
            code = error.code if isinstance(error, SetupError) else 'installation_failed'
            following = {**operation, 'status': 'failed', 'updated_at': self.clock(),
                         'error': code if code in ('repository_changed', 'readiness_failed', 'setup_changed', 'smoke_retry_changed',
                                                  'merge_unverified', 'repository_not_ready', 'repository_policy_changed') else 'installation_failed'}
        self.configuration.replace('installation_web', following, subject=generation, expected_revision=prior['revision'])
