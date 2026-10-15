"""Installation-bound smoke work and atomic handover to selected repositories."""
from copy import deepcopy
from datetime import datetime
import math
import re
import time

from forge_client.contracts import digest, fields, repository, require
from .factory.config import load, validate
from .factory.control import ControllerBusy, controller_lock
from .factory.first_merge import proof
from .factory.github import GitHub
from .install_scope import accepted_triage_in, triage_in, triage_run_in
from .onboarding import Onboarding
from .repository_setup import RepositorySetup
from .settings import Settings, validate as validate_settings
from .sqlite_identity import identity_effect_lock
from .store.configuration import Configuration
from .store.runtime import required, writable


class InstallationSmoke:
    def __init__(self, root, *, setup=None, github=None, clock=time.time, prove=proof):
        self.root, self.setup, self.clock, self.prove = root, setup or Onboarding(root), clock, prove
        self.store = required(root)
        self.configuration = Configuration(self.store, clock=clock)
        self.supplied_github = github

    def github(self):
        return self.supplied_github or GitHub(Settings(self.root, scope='active'), load(self.root, scope='active'))

    def call(self, action, parameters, *, setup_id, generation):
        require(action in {'issue', 'bind', 'launch', 'status', 'handover', 'retry'}, 'invalid_action')
        require(isinstance(setup_id, str) and 0 < len(setup_id) <= 256, 'invalid_setup')
        data = self.setup.read()
        require(data['generation'] == generation and data['phase'] in ('setup', 'active'), 'setup_changed')
        self.generation, self.setup_id = generation, setup_id
        if action == 'status':
            fields(parameters, ())
            return self.status()
        try:
            with identity_effect_lock(self.store), controller_lock(self.root):
                require(self.setup.read()['generation'] == generation, 'setup_changed')
                if action == 'issue':
                    return self.issue(parameters)
                if action == 'bind':
                    fields(parameters, ('binding',))
                    return self.bind(parameters['binding'])
                if action == 'retry':
                    fields(parameters, ('proposal_id',))
                    return self.retry(parameters['proposal_id'])
                fields(parameters, ())
                return self.launch() if action == 'launch' else self.handover()
        except ControllerBusy:
            return {'status': 'smoke_pending', 'phase': 'controller',
                    'reason': 'The supervisor is completing its current cycle. Resume installation to continue the same smoke work.'}

    def saved(self, domain='installation_smoke'):
        return self.configuration.get(domain, self.generation)

    def save(self, value, prior, domain='installation_smoke'):
        self.configuration.replace(domain, value, subject=self.generation,
                                   expected_revision=prior['revision'] if prior else 0)

    def _time(self, started_at):
        require(type(started_at) in (int, float) and math.isfinite(started_at)
                and 0 < started_at <= self.clock(), 'invalid_smoke_start')

    def issue(self, params):
        fields(params, ('intent_digest', 'repository', 'number', 'started_at'))
        target = repository(params['repository'])
        require(isinstance(params['intent_digest'], str) and re.fullmatch('[a-f0-9]{64}', params['intent_digest']), 'invalid_intent')
        self._time(params['started_at'])
        require(params['number'] is None or type(params['number']) is int and params['number'] > 0, 'invalid_smoke_issue')
        binding = dict(params, setup_id=self.setup_id, generation=self.generation)
        prior = self.saved('installation_smoke_issue')
        require(prior is None or prior['settings']['binding'] == binding, 'smoke_changed')
        github = self.github()
        item = {'repo': target['name'], 'repo_id': target['id']}
        require(github.call('controller', item, 'GET', '').get('id') == target['id'], 'repository_changed')
        number = params['number'] or ((prior or {}).get('settings') or {}).get('number')
        marker = '<!-- forge-install-smoke:' + digest(binding) + ' -->'
        if number is None:
            matches = [row for row in github.pages('controller', item, '/issues?state=all')
                       if 'pull_request' not in row and marker in (row.get('body') or '')]
            require(len(matches) <= 1, 'smoke_issue_ambiguous')
            if matches:
                require((matches[0].get('user') or {}).get('login') == github.publisher('controller'), 'smoke_issue_changed')
                number = matches[0]['number']
            elif prior:
                return {'status': 'write_unconfirmed', 'repository': target,
                        'reason': 'The smoke issue creation was sent but its marker is not observable. It will not be repeated.'}
            else:
                self.save({'binding': binding, 'number': None}, None, 'installation_smoke_issue')
                prior = self.saved('installation_smoke_issue')
                body = (marker + '\n\nCreate `.forge/smoke/' + params['intent_digest'][:12] + '.md` documenting that this '
                        'Forge installation completed its normal issue-to-merge pipeline. Keep this change documentation-only. '
                        'Acceptance: the file exists, explains the smoke check in a short paragraph, and contains no credentials. '
                        'Use the repository\'s normal checks, independent Review and Audit, and configured merge approval policy.')
                number = github.file_issue(item, 'Verify Forge installation with a documentation change', body, [])
        remote = github.call('controller', item, 'GET', '/issues/' + str(number))
        require(remote.get('number') == number and not remote.get('pull_request')
                and remote.get('state') in ('open', 'closed'), 'smoke_issue_changed')
        if params['number'] is None:
            require(marker in (remote.get('body') or '') and (remote.get('user') or {}).get('login') == github.publisher('controller'),
                    'smoke_issue_changed')
        if prior is None or prior['settings'].get('number') != number:
            self.save({'binding': binding, 'number': number}, prior, 'installation_smoke_issue')
        return {'status': 'smoke_issue_ready', 'repository': target, 'number': number}

    def bind(self, binding):
        fields(binding, ('intent_digest', 'repository', 'number', 'started_at', 'repositories', 'smoke_policy'))
        repository(binding['repository'])
        self._time(binding['started_at'])
        require(isinstance(binding['intent_digest'], str) and re.fullmatch('[a-f0-9]{64}', binding['intent_digest'])
                and type(binding['number']) is int and binding['number'] > 0
                and binding['smoke_policy'] in ('auto', 'human'), 'invalid_smoke_binding')
        require(isinstance(binding['repositories'], list) and 1 <= len(binding['repositories']) <= 20, 'invalid_smoke_binding')
        for row in binding['repositories']:
            fields(row, ('target', 'merge_policy', 'allow_workflow_edits'))
            repository(row['target'])
            require(row['merge_policy'] in ('auto', 'human') and type(row['allow_workflow_edits']) is bool, 'invalid_smoke_binding')
        require(len({r['target']['id'] for r in binding['repositories']}) == len(binding['repositories']), 'invalid_smoke_binding')
        prior = self.saved()
        pinned = dict(binding, setup_id=self.setup_id, generation=self.generation)
        if prior:
            require(prior['settings']['binding'] == pinned, 'smoke_changed')
            self._configure_smoke(pinned)
            return self.status()
        require(self.setup.read()['phase'] == 'setup', 'smoke_requires_setup', 'Bind the smoke issue before starting this factory.')
        issue = self.saved('installation_smoke_issue')
        require(issue and issue['settings']['number'] == binding['number']
                and all(issue['settings']['binding'][key] == pinned[key] for key in
                        ('intent_digest', 'repository', 'started_at', 'setup_id', 'generation')), 'smoke_issue_required')
        with self.store.read_snapshot() as db:
            require(not db.execute("SELECT 1 FROM units WHERE state NOT IN ('merged','closed','dissolved') LIMIT 1").fetchone()
                    and not db.execute('SELECT 1 FROM jobs WHERE cleanup=0 LIMIT 1').fetchone()
                    and not db.execute('SELECT 1 FROM auxiliary_executions WHERE cleanup=0 LIMIT 1').fetchone(),
                    'smoke_requires_idle', 'Finish or reset existing work before starting installation smoke work.')
        self.save({'binding': pinned, 'phase': 'pending', 'verified': None}, None)
        self._configure_smoke(pinned)
        return self.status()

    def _configure_smoke(self, binding):
        if self.setup.read()['phase'] == 'setup':
            runtime = self.setup.runtime()
            runtime['repository_merge_policies'][str(binding['repository']['id'])] = binding['smoke_policy']
            selected = next((row for row in binding['repositories'] if row['target'] == binding['repository']), None)
            runtime['repository_workflow_edits'][str(binding['repository']['id'])] = bool(selected and selected['allow_workflow_edits'])
            self.setup.save_runtime(runtime, active=False)

    def launch(self):
        saved = self.saved()
        require(saved and saved['settings']['binding']['setup_id'] == self.setup_id, 'smoke_binding_required')
        binding = saved['settings']['binding']
        require(self.setup.read()['phase'] == 'active', 'factory_not_started')
        if saved['settings']['phase'] == 'complete':
            return self.status()
        failure = self.triage_failure(binding)
        if failure:
            return failure
        target = binding['repository']
        github = self.github()
        item = {'repo': target['name'], 'repo_id': target['id']}
        issue = github.call('controller', item, 'GET', '/issues/' + str(binding['number']))
        require(issue.get('number') == binding['number'] and not issue.get('pull_request'), 'smoke_issue_changed')
        if issue.get('state') == 'closed':
            return self.status()
        require(issue.get('state') == 'open', 'smoke_issue_changed')
        runtime = load(self.root)
        # Preserve hold labels and policy: marking ready never grants merge approval.
        if not saved['settings'].get('ready_marked'):
            if runtime['ready_label'] not in {label['name'] for label in issue.get('labels', [])}:
                if saved['settings'].get('ready_requested'):
                    return {'status': 'write_unconfirmed', 'reason': 'The smoke ready-label write is not confirmed. '
                            'Inspect the issue labels before resuming; an operator removal will not be overwritten.'}
                self.save({**saved['settings'], 'ready_requested': True}, saved)
                saved = self.saved()
                github.call('controller', item, 'POST', '/issues/' + str(binding['number']) + '/labels', {'labels': [runtime['ready_label']]})
            self.save({**saved['settings'], 'ready_requested': True, 'ready_marked': True}, saved)
        from .factory.store_inventory import StoreInventory
        remote, requirements = github.issue({**item, 'number': binding['number']})
        StoreInventory(self.store, Settings(self.root), github, config=runtime, clock=self.clock).record(
            dict(target, enabled=True), remote, requirements)
        from .store.repository_planning import RepositoryPlanning
        plans = RepositoryPlanning(self.store, clock=self.clock)
        with self.store.read_snapshot() as db:
            completed = triage_run_in(db, binding)
            active = db.execute("SELECT id,requested_at FROM repository_triage_runs WHERE repository_id=? "
                                "AND status IN ('queued','running','captured')", (target['id'],)).fetchone()
            interrupted = db.execute("SELECT r.id FROM repository_triage_runs r "
                "JOIN auxiliary_executions a ON a.id=r.execution_id "
                "JOIN auxiliary_runtime x ON x.execution_id=a.id WHERE r.repository_id=? "
                "AND a.domain='triage' AND a.owner_id=r.id AND a.fenced=1 AND a.cleanup=1 AND x.capture_digest IS NULL "
                "AND r.status='failed' AND r.error='maintenance_interrupted' AND r.requested_at>=? "
                "AND (SELECT count(*) FROM triage_proposal_inputs p WHERE p.proposal_id=r.id)=1 "
                "AND EXISTS(SELECT 1 FROM triage_proposal_inputs p JOIN issues i ON i.id=p.issue_id "
                "WHERE p.proposal_id=r.id AND i.repository_id=? AND i.github_number=?) "
                "ORDER BY r.requested_at DESC,r.rowid DESC LIMIT 1",
                (target['id'], binding['started_at'], target['id'], binding['number'])).fetchone()
            if active:
                members = list(db.execute('SELECT i.repository_id,i.github_number FROM triage_proposal_inputs p '
                    'JOIN issues i ON i.id=p.issue_id WHERE p.proposal_id=?', (active['id'],)))
                exact = (active['requested_at'] >= binding['started_at'] and len(members) == 1
                         and tuple(members[0]) == (target['id'], binding['number']))
        # Automatic repository planning may win the race after activation. Its
        # exact smoke run is the same work; do not queue a competing proposal.
        # Other captures are never adopted as smoke evidence.
        if active and not exact:
            return {'status': 'smoke_pending', 'phase': 'triage',
                    'reason': 'Waiting for the existing repository triage to finish before planning the exact smoke issue.'}
        if not active and not completed:
            request_id = 'installation-smoke:' + digest(binding)
            if interrupted:
                request_id += ':after:' + interrupted['id']
            plans.queue(target['id'], request_id=request_id, actor_id='operator:installation',
                        base_branch=github.target(item))
        waiting = self.accept_keep(binding, github, runtime)
        return waiting or self.status()

    def triage_wait(self, run, reason):
        return {'status': 'smoke_triage_required', 'proposal_id': run['id'], 'reason': reason,
                'next_action': {'instruction': 'Review the smoke issue’s proposal in Forge. Accept a runnable disposition; '
                    'parked, closed or unanswered work cannot pass installation smoke. Then resume forge install.',
                    'path': '/issues'}}

    def triage_failure(self, binding):
        """A finished failed proposal needs repair and an explicit new request."""
        with self.store.read_snapshot() as db:
            if triage_run_in(db, binding) or db.execute(
                    "SELECT 1 FROM repository_triage_runs WHERE repository_id=? "
                    "AND status IN ('queued','running','captured')", (binding['repository']['id'],)).fetchone():
                return None
            run = db.execute("SELECT r.id,r.status,r.error FROM repository_triage_runs r "
                "JOIN triage_proposal_inputs p ON p.proposal_id=r.id JOIN issues i ON i.id=p.issue_id "
                "WHERE r.repository_id=? AND i.repository_id=r.repository_id AND r.requested_at>=? AND i.github_number=? "
                "AND NOT EXISTS(SELECT 1 FROM triage_proposal_inputs other "
                "WHERE other.proposal_id=r.id AND other.issue_id!=p.issue_id) "
                "ORDER BY r.requested_at DESC,r.rowid DESC LIMIT 1",
                (binding['repository']['id'], binding['started_at'], binding['number'])).fetchone()
        if not run or run['status'] != 'failed' or run['error'] == 'maintenance_interrupted':
            return None
        reasons = {
            'steward_cycle_limit: request a new proposal': ('steward_cycle_limit', 'The smoke planner reached its three-attempt limit.'),
            'triage_result_missing': ('triage_result_missing', 'The smoke planner returned no result.'),
            'triage_result_invalid': ('triage_result_invalid', 'The smoke planner returned an invalid result.'),
            'triage_worker_unsuccessful': ('triage_worker_unsuccessful', 'The smoke planner could not complete its task.'),
            'triage_proposal_invalid': ('triage_proposal_invalid', 'The smoke planner returned an invalid proposal.'),
            'triage_inputs_changed': ('triage_inputs_changed', 'The smoke planning inputs changed before launch.'),
        }
        error, reason = reasons.get(run['error'], ('triage_failed', 'The smoke planner stopped with a failed proposal.'))
        return {'status': 'smoke_triage_failed', 'proposal_id': run['id'], 'error': error, 'reason': reason,
                'next_action': {'instruction': 'Inspect the failed proposal in Forge and fix the reported problem. '
                    'Request new repository triage, then resume forge install.', 'path': '/issues'}}

    def retry(self, proposal_id):
        saved = self.saved()
        require(saved and saved['settings']['binding']['setup_id'] == self.setup_id, 'smoke_binding_required')
        require(self.setup.read()['phase'] == 'active' and saved['settings']['phase'] != 'complete', 'smoke_not_pending')
        require(isinstance(proposal_id, str) and 0 < len(proposal_id) <= 256, 'invalid_proposal')
        binding = saved['settings']['binding']
        request_id = 'installation-smoke-retry:' + digest({'binding': binding, 'failed_proposal': proposal_id})
        from .store.repository_planning import RepositoryPlanning
        with self.store.read_snapshot() as db:
            prior = db.execute('SELECT 1 FROM repository_planning_requests WHERE request_id=?', (request_id,)).fetchone()
            proposal = db.execute('SELECT base_branch FROM triage_proposals WHERE id=? AND repository_id=?',
                                  (proposal_id, binding['repository']['id'])).fetchone()
            unclean = db.execute("SELECT 1 FROM auxiliary_executions WHERE domain='triage' AND owner_id=? AND cleanup=0",
                                 (proposal_id,)).fetchone()
        if not prior:
            failure = self.triage_failure(binding)
            require(failure and failure['proposal_id'] == proposal_id and not unclean, 'smoke_retry_changed',
                    'Retry requires the current failed smoke proposal with all its workers cleaned up.')
        require(proposal, 'smoke_retry_changed')
        queued = RepositoryPlanning(self.store, clock=self.clock).queue(binding['repository']['id'],
            request_id=request_id, actor_id='operator:installation', base_branch=proposal['base_branch'])
        return {**queued, 'status': 'smoke_retry_queued', 'failed_proposal_id': proposal_id}

    def accept_keep(self, binding, github, runtime):
        """Installation consent covers exactly one unchanged keep disposition.

        This uses the ordinary acceptance API and its fresh GitHub evidence. It
        cannot authorize a group, closure, label edit, question or dependency.
        """
        with self.store.read_snapshot() as db:
            if triage_in(db, binding):
                return None
            run = triage_run_in(db, binding)
            # The usual effect controller finishes accepted label/environment writes.
            # Waiting for that evidence needs no second operator approval.
            if accepted_triage_in(db, binding):
                return None
        if not run:
            return None
        from .factory.planning_service import PlanningService
        from .store.repository_planning import RepositoryPlanning
        from .store.models import TransitionError
        proposal = RepositoryPlanning(self.store, clock=self.clock).proposal(run['id'])
        rows = proposal['rows']
        bounded = (proposal['status'] == 'proposed' and proposal['total'] == 1 and len(rows) == 1
                   and rows[0]['status'] == 'proposed' and rows[0]['action'] == 'keep'
                   and rows[0]['parameters'] == {} and rows[0]['member_count'] == 1
                   and rows[0]['members'][0]['github_number'] == binding['number'])
        if not bounded:
            return self.triage_wait(run, 'The smoke issue has no accepted runnable triage disposition with confirmed effects.')
        service = PlanningService(self.root, actor_id='operator:installation:' + self.setup_id, clock=self.clock,
                                  parts={'store': self.store, 'github': github, 'config': runtime})
        try:
            service.action(binding['repository']['id'], {
                'action': 'accept', 'proposal_id': run['id'], 'expected_version': proposal['version'],
                'accept_rows': [rows[0]['id']], 'reject_rows': [],
                'request_id': 'installation-smoke-keep:' + digest({'binding': binding, 'proposal': run['id'],
                                                               'version': proposal['version']})})
        except TransitionError as error:
            return self.triage_wait(run, 'The smoke proposal needs review: ' + str(error))
        return None

    def status(self):
        saved = self.saved()
        if not saved:
            return {'status': 'smoke_binding_required'}
        value = saved['settings']; binding = value['binding']
        require(binding['setup_id'] == self.setup_id, 'smoke_changed')
        if value['phase'] == 'complete':
            return {'status': 'handover_complete', **deepcopy(value['verified'])}
        failure = self.triage_failure(binding)
        if failure:
            return failure
        observed = self.prove(self.root, repository=binding['repository'], number=binding['number'],
                              started_at=binding['started_at'], clock=self.clock)
        with self.store.read_snapshot() as db:
            triage = triage_in(db, binding)
        return {**observed, 'status': 'smoke_merged' if observed['status'] == 'merged' and triage else 'smoke_pending',
                'binding': binding, 'triage': triage}

    def handover(self):
        observed = self.status()
        if observed['status'] == 'handover_complete':
            return observed
        if observed['status'] != 'smoke_merged':
            return observed
        saved = self.saved(); binding = saved['settings']['binding']
        target = binding['repository']; candidate = observed['candidate']
        github = self.github(); item = {'repo': target['name'], 'repo_id': target['id']}
        metadata = github.call('controller', item, 'GET', '')
        pull = github.pr(item, candidate['pull_number'])
        issue = github.call('controller', item, 'GET', '/issues/' + str(binding['number']))
        require(metadata.get('id') == target['id'] and pull.get('number') == candidate['pull_number']
                and pull.get('merged') is True and pull.get('state') == 'closed'
                and (pull.get('head') or {}).get('sha') == candidate['head_sha']
                and (pull.get('head') or {}).get('repo', {}).get('id') == target['id']
                and (pull.get('base') or {}).get('repo', {}).get('id') == target['id']
                and pull.get('merge_commit_sha') == candidate['merge_sha']
                and issue.get('number') == binding['number'] and not issue.get('pull_request')
                and issue.get('state') == 'closed' and issue.get('state_reason') == 'completed',
                'merge_unverified', 'GitHub has not confirmed this exact smoke merge and completed issue.')
        try:
            merged_at = datetime.fromisoformat(pull['merged_at'].replace('Z', '+00:00')).timestamp()
        except (KeyError, TypeError, ValueError):
            require(False, 'merge_unverified')
        require(binding['started_at'] <= merged_at <= self.clock() + 60, 'merge_unverified')
        settings = Settings(self.root)
        require(settings.refresh(), 'setup_busy')
        revision = settings.view()['revision']
        checks = RepositorySetup(settings)
        for selected in binding['repositories']:
            require(checks.check(selected['target']['id'], revision), 'setup_busy')
        view = settings.view()
        rows = {row['id']: row for row in view['repositories']}
        for selected in binding['repositories']:
            current = rows.get(selected['target']['id'])
            require(current and current['name'] == selected['target']['name'] and current['access_ready']
                    and current['protection_ready'], 'repository_not_ready', 'Verify every selected repository before handover.')
        config, config_revision = settings.config_snapshot()
        configured = {row['id']: deepcopy(row) for row in config['repositories']}
        for row in configured.values():
            row['enabled'] = False
        for selected in binding['repositories']:
            target = selected['target']
            configured[target['id']] = {**configured.get(target['id'], {}), **target, 'enabled': True}
        config['repositories'] = list(configured.values())
        validate_settings(config)
        runtime_row = self.configuration.get('runtime.json')
        runtime = validate(runtime_row['settings'] if runtime_row else {})
        for selected in binding['repositories']:
            runtime['repository_merge_policies'][str(selected['target']['id'])] = selected['merge_policy']
            runtime['repository_workflow_edits'][str(selected['target']['id'])] = selected['allow_workflow_edits']
        validate(runtime)
        receipt = {key: deepcopy(observed[key]) for key in ('candidate', 'evidence', 'attempts', 'triage')}
        receipt.update(repository=binding['repository'], number=binding['number'], started_at=binding['started_at'],
                       merged_at=merged_at, elapsed_seconds=max(0, merged_at-binding['started_at']),
                       verified_at=self.clock(), setup_id=self.setup_id, generation=self.generation,
                       intent_digest=binding['intent_digest'], repositories=deepcopy(binding['repositories']))
        with self.store.write() as db:
            writable(db)
            current = db.execute("SELECT generation,phase FROM setup_instances WHERE scope='live'").fetchone()
            require(current and tuple(current) == (self.generation, 'active'), 'setup_changed')
            # Scope release, policies, repository activation and receipt commit together.
            self.configuration.replace_in(db, 'installation_smoke', {'binding': binding, 'phase': 'complete', 'verified': receipt},
                                          subject=self.generation, expected_revision=saved['revision'])
            self.configuration.replace_in(db, 'runtime.json', runtime, expected_revision=runtime_row['revision'] if runtime_row else 0)
            self.configuration.replace_in(db, 'factory.json', config, expected_revision=config_revision)
            db.execute('UPDATE repositories SET enabled=0')
            for target in config['repositories']:
                db.execute('INSERT INTO repositories(id,full_name,enabled) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET '
                           'full_name=excluded.full_name,enabled=excluded.enabled', (target['id'], target['name'], int(target['enabled'])))
        return {'status': 'handover_complete', **receipt}
