"""Read-only investigations of persisted SQLite assignment failures.

Recovery runs in the existing disposable worker driver with its own role token.
Its report can propose a retry approach; only independently observed GitHub
publication can correct a failure charge or select a candidate.
"""
from copy import deepcopy
import json
import time
import uuid

from ..store.artifacts import ArtifactCorrupt
from ..store.db import StoreError
from ..store.executions import ExecutionQueries, Launch, provider_available
from ..store.models import Candidate, Command, TransitionError
from ..store.queries import Queries
from ..store.recovery import Recovery, RecoveryQueries
from ..store.transitions import manifest_hash
from .config import policy, validate
from .contracts import BUILD_CANDIDATE_OUTCOMES, digest, result
from .github import GitHubUnavailable
from .ownership import ownership, publication_context
from .result_controller import ResultController
from .runner import ContainerRunner, RunnerError


class RecoveryController:
    def __init__(self, store, runner, github, *, config, admission, clock=time.time):
        self.store, self.runner, self.github = store, runner, github
        self.config, self.admission, self.clock = validate(config), admission, clock
        self.commands = Recovery(store, clock=clock)
        self.queries, self.jobs = RecoveryQueries(store), ExecutionQueries(store)

    @staticmethod
    def _command(job):
        return Command(str(uuid.uuid4()), 'supervisor:recovery', job['unit_id'], job['unit_version'])

    def _active(self):
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot investigate live failures')

    def _provider(self):
        pool = self.config['provider_pool'] or [dict(id=None, **{key: self.config[key] for key in
                ('provider', 'model', 'provider_environment', 'provider_auth_file_env')})]
        with self.store.read_snapshot() as db:
            row = db.execute("SELECT cursor FROM runtime_controls WHERE name='provider_cursor'").fetchone()
            start = int(row[0]) % len(pool) if row and row[0] else 0
            for offset in range(len(pool)):
                entry = pool[(start + offset) % len(pool)]
                if self.config['provider_ready'] is not None and entry['id'] not in self.config['provider_ready']:
                    continue
                config = {**deepcopy(self.config), **{key: value for key, value in entry.items() if key != 'id'}}
                if provider_available(db, entry['provider'], entry['id'], config, self.clock()):
                    return entry, config
        return None, None

    def schedule(self, failed_job_id):
        self._active()
        if not self.config['enabled']:
            return {'failed_job_id': failed_job_id, 'pending': 'runtime_disabled'}
        # A job without an immutable launch cannot be turned into an assignment.
        try:
            original = self.jobs.job(failed_job_id)
        except KeyError:
            return {'failed_job_id': failed_job_id, 'pending': 'launch_unavailable'}
        from ..store.execution_problems import ExecutionProblems
        repaired=ExecutionProblems(self.store,clock=self.clock).recheck_recovery_memory(self._command(original),failed_job_id,self.config['memory_tiers'])
        if repaired.get('resolved'):original=self.jobs.job(failed_job_id)
        if not original['cleanup']:
            return {'failed_job_id': failed_job_id, 'pending': 'cleanup'}
        if self.admission(original) is not True:
            return {'failed_job_id': failed_job_id, 'pending': 'repository_setup'}
        if not self._fresh_members(original, current=True, diagnostic=True):
            return {'failed_job_id': failed_job_id, 'pending': 'issue_reconciliation'}
        with self.store.read_snapshot() as db:
            occupied = {row[0] for row in db.execute('SELECT slot FROM job_leases UNION SELECT slot FROM discussion_leases')}
        slot = next((f'recovery-{index + 1}' for index in range(self.config['slots']['recovery'])
                     if f'recovery-{index + 1}' not in occupied), None)
        if slot is None:
            return {'failed_job_id': failed_job_id, 'pending': 'role_capacity'}
        entry, config = self._provider()
        if entry is None:
            return {'failed_job_id': failed_job_id, 'pending': 'provider_unavailable'}
        from ..store.runtime_requirements import _memory
        with self.store.read_snapshot() as db:
            retries = db.execute('SELECT m.allocation FROM recovery_memory_retries m JOIN recovery_assignments r ON r.job_id=m.job_id '
                'WHERE r.failed_job_id=?',(failed_job_id,)).fetchall()
        if retries:
            minimum=max(_memory(row[0]) for row in retries)
            chosen = next((tier for tier in config['memory_tiers'] if _memory(tier)>=minimum),None)
            if chosen is None:return {'failed_job_id':failed_job_id,'pending':'memory_configuration'}
            config['memory'] = chosen
        runner = ContainerRunner(config, environ=self.runner.environ) if isinstance(self.runner, ContainerRunner) else self.runner
        runner.ready()
        if runner.capacity().get('allowed') is not True:
            return {'failed_job_id': failed_job_id, 'pending': 'capacity_unavailable'}
        publisher = self.github.publisher('recovery')
        if self.admission(original) is not True:
            return {'failed_job_id': failed_job_id, 'pending': 'repository_setup'}
        context = {key: original[key] for key in ('review_policy', 'verification_checks', 'ci_evidence') if key in original}
        launch = Launch(original['branch'], publisher, entry['provider'],
                        self.clock() + config['timeout_seconds']['recovery'], config, policy(config, original['repo_id'], repository_name=original['repo']),
                        entry['id'], entry['model'], context)
        answer = self.commands.reserve(self._command(original), failed_job_id, slot=slot,
                                       execution_fence=str(uuid.uuid4()), launch=launch)
        return {'failed_job_id': failed_job_id, 'reserved': True, **answer}

    def _fresh_members(self, job, *, current=False, diagnostic=False):
        members = job.get('members') or [{'issue_number': job['issue_number'], 'issue_key': job['issue_key'], 'requirements': job['requirements']}]
        pulls = self.github.pulls(job)
        for member in members:
            wanted = Queries(self.store).issue(member['issue_key'])['requirements'] if current else member['requirements']
            item = dict(repo=job['repo'], repo_id=job['repo_id'], number=member['issue_number'],
                        unit_state=job['unit_state'], reviewers_requested=job.get('reviewers_requested'), candidate=job['candidate'])
            item = publication_context(self.store, job['unit_id'], item)
            remote, requirements = self.github.issue(item)
            states = {'open', 'closed'} if diagnostic else {'open'}
            if (remote.get('state') not in states or requirements != wanted
                    or not self._remote_admitted(remote, item, pulls)):
                return False
        return True

    @staticmethod
    def _incomplete(reason):
        return dict(summary=reason, pattern='', evidence=[], uncertainty=reason, alternative=None,
                    headline='The investigation could not finish',
                    plain='Forge could not establish the cause of this failure. Its existing failure budget is unchanged.')

    def _remote_admitted(self, issue, item, pulls=None):
        labels = {row['name'] if isinstance(row, dict) else row for row in issue.get('labels', [])}
        return (not labels.intersection(self.config['hold_labels'])
                and ownership(issue, item, item['repo'], pulls) is None)

    def finish(self, recovery_job_id):
        self._active()
        job = self.jobs.job(recovery_job_id)
        if job['role'] != 'recovery':
            raise TransitionError('recovery classifier requires a recovery assignment')
        if job['outcome'] is not None:
            return {'job_id': recovery_job_id, 'outcome': job['outcome']}
        if job['captured_at'] is None or not job['cleanup']:
            return {'job_id': recovery_job_id, 'pending': 'cleanup'}
        capture = self.jobs.capture(recovery_job_id)
        stale = (not job['capture_authorized'] or job['result_revoked']
                 or job['unit_state'] in {'merged', 'closed'}
                 or job['assignment_generation'] != job['fencing_generation']
                 or job['assigned_membership'] != job['membership_generation']
                 or job['assigned_budget'] != job['budget_generation']
                 or job['candidate_id'] is not None and job['candidate_id'] != job['current_candidate_id'])
        if stale:
            answer = self.commands.finish(self._command(job), recovery_job_id,
                execution_fence=job['execution_fence'], input_manifest=job['input_manifest'],
                findings=self._incomplete('Investigation assignment was superseded'), incomplete=True)
            return {'job_id': recovery_job_id, **answer}
        from .preparation_problems import problem as preparation_problem
        preparation = preparation_problem(self.store, job)
        if preparation is not None:
            return {'job_id': recovery_job_id, **self.commands.defer_job(self._command(job), recovery_job_id,
                execution_fence=job['execution_fence'], input_manifest=job['input_manifest'],
                evidence=preparation, code=preparation['gate'], details=preparation)}
        from .execution_problems import classify, contract_problem
        from ..store.execution_problems import ExecutionProblems
        execution = classify(job, capture, self.clock())
        if execution and execution['kind'] in {'forge_problem','provider_error','oom'}:
            detail = execution['details']
            args = dict(execution_fence=job['execution_fence'], input_manifest=job['input_manifest'])
            if execution['kind'] == 'oom':
                retried = ExecutionProblems(self.store, clock=self.clock).retry_recovery_memory(self._command(job),
                    recovery_job_id, **args, tiers=self.config['memory_tiers'])
                if retried is not None:return {'job_id':recovery_job_id, **retried}
            if execution['kind'] == 'forge_problem':
                answer = ExecutionProblems(self.store, clock=self.clock).defer(self._command(job), recovery_job_id,
                    **args, details=detail)
            else:
                code = 'provider_unavailable' if execution['kind'] == 'provider_error' else 'environment'
                answer = self.commands.defer_job(self._command(job), recovery_job_id,
                    **args, evidence=detail, code=code, details=detail)
            return {'job_id':recovery_job_id, **answer}
        if execution and execution['kind']=='timeout':
            return {'job_id':recovery_job_id,**self.commands.finish(self._command(job),recovery_job_id,
                execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],
                findings=self._incomplete('Investigation exceeded its execution deadline'),incomplete=True,timeout=True)}
        try:
            report = result(capture.get('result'), job)
        except (ValueError, KeyError, TypeError):
            report = None
        if report is None:
            fault = contract_problem(job, capture)
            if fault is not None:
                return {'job_id':recovery_job_id, **ExecutionProblems(self.store, clock=self.clock).defer(
                    self._command(job), recovery_job_id, execution_fence=job['execution_fence'],
                    input_manifest=job['input_manifest'], details=fault)}
        incomplete = report is None or report['outcome'] != 'pass' or len(json.dumps(report).encode()) > 60000
        findings = report if not incomplete else self._incomplete(
            'Investigation exceeded its execution deadline' if capture.get('timed_out')
            else 'Investigation did not return usable findings')
        args = dict(execution_fence=job['execution_fence'], input_manifest=job['input_manifest'], findings=findings)
        original = self.jobs.job(job['input_manifest']['failed_job_id'])
        # Publication proof cannot turn unfinished work into a candidate. The
        # original build's immutable result must establish content completion.
        original_report = None
        if original['role'] == 'build':
            try:
                original_report = result(self.jobs.capture(original['id']).get('result'), original)
            except (ValueError, KeyError, TypeError):
                pass
        verification = (report is not None and report['outcome'] == 'needs_human_verification'
                        and len(json.dumps(report).encode()) <= 60000)
        policy_current = original['input_manifest'].get('policy_hash') == digest(policy(self.config, original['repo_id'], repository_name=original['repo']))
        from .delegation_contracts import read_only_build
        if (original_report and original_report['outcome'] in BUILD_CANDIDATE_OUTCOMES and not read_only_build(original)
                and policy_current and job['unit_state'] in {'building', 'publishing'}
                and job['current_candidate_id'] is None
                and (original.get('builder_stage') == 'integration' or
                     job['input_manifest'].get('requirements_revision_id') == original['input_manifest'].get('requirements_revision_id'))):
            item = dict(repo=original['repo'], repo_id=original['repo_id'], number=original['issue_number'],
                        unit_state=original['unit_state'],reviewers_requested=original.get('reviewers_requested'),candidate=original.get('candidate'))
            requirements = original['requirements']
            if self._fresh_members(original):
                publication = self.github.publication(original, requirements, original['input_manifest']['policy_hash'])
                if publication and publication['base'] == original['input_manifest']['base_oid']:
                    identity = Candidate(publication['pr'], publication['branch'], publication['head'], publication['base'],
                        original['input_manifest']['policy_hash'], original['input_manifest'].get('requirements_hash', manifest_hash(requirements)), manifest_hash(original['input_manifest']))
                    evidence = dict(verified=True, head_oid=identity.head_oid, base_oid=identity.base_oid,
                                    pull_number=identity.pull_number, failed_job_id=original['id'], observed_at=self.clock(),
                                    policy_hash=identity.policy_hash, requirements_hash=identity.requirements_hash,
                                    input_manifest_hash=identity.input_manifest_hash)
                    inherited = ResultController._verification(original, original_report,
                                                               recovery=report if verification else None)
                    gates = [inherited] if inherited else []
                    if verification:
                        args['findings'] = report
                    answer = self.commands.recover_publication(self._command(job), recovery_job_id,
                        **args, candidate=identity, evidence=evidence, incomplete=incomplete and not verification, gates=gates)
                    return {'job_id': recovery_job_id, **answer}
        gates = []
        if (verification and policy_current and original['role'] in {'review', 'audit'}
                and original['candidate_id'] == job['candidate_id'] == job['current_candidate_id']):
            item = dict(repo=original['repo'], repo_id=original['repo_id'], number=original['issue_number'],
                        unit_state=original['unit_state'],reviewers_requested=original.get('reviewers_requested'),candidate=original.get('candidate'))
            if (self._fresh_members(original)
                    and ResultController._current_pr(self.github.pr(item, original['candidate']['pr']), original)):
                args['findings'] = report
                incomplete = False
                gates = [ResultController._verification(original, report)]
        answer = self.commands.finish(self._command(job), recovery_job_id, **args, incomplete=incomplete, gates=gates)
        return {'job_id': recovery_job_id, **answer}

    def cycle(self, *, after=None, limit=50):
        # Separate cursors prevent a slow report page from starving scheduling.
        after = after or {}
        if not isinstance(after, dict) or set(after) - {'results', 'failures'}:
            raise ValueError('recovery cursor must name result and failure positions')
        self._active()
        results = self.queries.results(after=after.get('results', ''), limit=limit)
        failures = self.queries.pending_failures(after=after.get('failures', ''), limit=limit)
        items = []
        for kind, page, operation in [('result', results, self.finish), ('failure', failures, self.schedule)]:
            for job_id in page['items']:
                try:
                    items.append(operation(job_id))
                except (GitHubUnavailable, RunnerError, TransitionError, StoreError, ArtifactCorrupt, OSError) as error:
                    items.append({'job_id': job_id, 'kind': kind, 'error': getattr(error, 'code', type(error).__name__)})
        next_cursor = dict(results=results['next_cursor'] or '', failures=failures['next_cursor'] or '')
        return {'items': items, 'next_cursor': next_cursor if any(next_cursor.values()) else None}
