"""Verify captured SQLite worker claims against fresh GitHub facts.

No external operation runs in a SQL transaction. The lifecycle rechecks the
assignment and version at commit; an observation or operator decision racing
with GitHub reads therefore cannot advance a superseded candidate.
"""
import json
import re
import subprocess
import time
import uuid

from ..store.artifacts import Artifacts, ArtifactCorrupt, ArtifactExpired
from ..store.db import StoreError
from ..store.executions import Executions, ExecutionQueries
from ..store.models import Candidate, Command, TransitionError, InvalidDelegationPlan
from ..store.transitions import Lifecycle, manifest_hash
from .contracts import digest, result
from .github import GitHubUnavailable
from .ownership import ownership, publication_context


class ResultController:
    def __init__(self, store, github, *, policy, verdicts=None, clock=time.time,
                 availability_retry_seconds=300, hold_labels=('hold', 'blocked'), requirements=None):
        self.store, self.github = store, github
        self.requirements = requirements
        self.policy, self.verdicts, self.clock = policy, verdicts, clock
        self.availability_retry_seconds = availability_retry_seconds
        self.hold_labels = frozenset(hold_labels)
        self.commands = Lifecycle(store, clock=clock)
        self.queries = ExecutionQueries(store)

    def _command(self, job):
        return Command(str(uuid.uuid4()), 'supervisor:results', job['unit_id'], job['unit_version'])

    @staticmethod
    def _args(job, evidence):
        return dict(execution_fence=job['execution_fence'], input_manifest=job['input_manifest'], evidence=evidence)

    def _failure(self, job, evidence, *, charge=True):
        return self.commands.record_failure(self._command(job), job['id'], **self._args(job, evidence), charge=charge)

    def _defer(self, job, evidence, code, details):
        return self.commands.defer_job(self._command(job), job['id'], **self._args(job, evidence), code=code, details=details)

    @staticmethod
    def _availability(capture, logs):
        """Only failed CLI error channels can establish provider unavailability."""
        if (capture.get('execution') or {}).get('exit_code', 0) == 0:
            return None
        for line in logs.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict) or event.get('type') not in {'error', 'turn.failed'}:
                continue
            error = event.get('error') or event
            message = error.get('message', '') if isinstance(error, dict) else ''
            if not isinstance(message, str):
                continue
            message = message.lower()
            if any(word in message for word in ('insufficient_quota', 'credit balance', 'usage limit',
                                                'out of credits', 'exhausted credits')):
                return 'blocked_quota'
            if any(word in message for word in ('authentication_error', 'invalid_api_key', 'not logged in',
                                                'please login', 'please log in', 'token expired', 'unauthorized')):
                return 'blocked_auth'
        return None

    @staticmethod
    def _current_pr(pr, job):
        selected = job['candidate']
        return (pr.get('number') == selected['pr'] and pr.get('state') == 'open'
                and pr.get('draft') is False and not pr.get('merged')
                and pr.get('head', {}).get('sha') == selected['head']
                and pr.get('base', {}).get('sha') == selected['base']
                and pr.get('base', {}).get('ref') == job['target_branch']
                and pr.get('base', {}).get('repo', {}).get('id') == job['repo_id']
                and pr.get('head', {}).get('repo', {}).get('id') == job['repo_id'])

    @staticmethod
    def _verification(job, report):
        requested = report['verification']
        allowed = {row['name']: row for row in job.get('verification_checks', [])}
        wanted = requested.get('checks', [])
        checks = [allowed[name] for name in wanted] if wanted and all(name in allowed for name in wanted) else []
        return {'code': 'verification_ci' if checks else 'verification_human',
                'details': {**requested, 'ci': checks, 'job_id': job['id']}}

    def step(self, job_id):
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot classify worker results')
        job = self.queries.job(job_id)
        if job['role'] == 'recovery':
            return {'job_id': job_id, 'pending': 'recovery_controller'}
        if job['outcome'] is not None:
            return {'job_id': job_id, 'outcome': job['outcome']}
        if job['captured_at'] is None or not job['cleanup']:
            return {'job_id': job_id, 'pending': 'cleanup'}
        capture = self.queries.capture(job_id)
        stale = (not job['capture_authorized'] or job.get('result_revoked')
                 or job['unit_state'] in {'merged', 'closed', 'dissolved'}
                 or job['assignment_generation'] != job['fencing_generation']
                 or job['assigned_membership'] != job['membership_generation']
                 or job['assigned_budget'] != job['budget_generation']
                 or job['candidate_id'] is not None and job['candidate_id'] != job['current_candidate_id'])
        if stale:
            return {'job_id': job_id, **self._failure(job, {'reason': 'assignment_superseded'}, charge=False)}
        try:
            report = result(capture.get('result'), job)
        except (ValueError, TypeError, KeyError):
            report = None
        evidence = {'report': report, 'capture_job_id': job_id}
        item = publication_context(self.store, job['unit_id'], dict(repo=job['repo'], repo_id=job['repo_id'], number=job['issue_number']))
        members = job.get('members') or [{'issue_number': job['issue_number'], 'requirements': job['requirements']}]
        remote_members = []
        for member in members:
            remote, pinned = self.github.issue({**item, 'number': member['issue_number']})
            if remote.get('state') != 'open' or pinned != member['requirements']:
                return {'job_id': job_id, 'pending': 'issue_reconciliation'}
            labels = {row.get('name') if isinstance(row, dict) else row for row in remote.get('labels', [])}
            if labels & self.hold_labels or ownership(remote, {**item, 'number': member['issue_number'],
                    'candidate': job['candidate'], 'reviewers_requested': job.get('reviewers_requested'),
                    'unit_state': job['unit_state']}, job['repo']):
                return {'job_id': job_id, 'pending': 'issue_reconciliation'}
            remote_members.append(remote)
        issue = remote_members[0] if len(remote_members) == 1 else None
        requirements = job['requirements']
        current_policy = self.policy(job)
        if digest(current_policy) != job['input_manifest']['policy_hash']:
            # Candidate invalidation belongs to observation. A build has no
            # selected candidate yet, so revoke its captured launch directly.
            if job['role'] == 'build':
                Executions(self.store, clock=self.clock).revoke_captured(self._command(job), job_id,
                    execution_fence=job['execution_fence'], input_manifest=job['input_manifest'],
                    policy_hash=digest(current_policy))
            return {'job_id': job_id, 'pending': 'policy_reconciliation'}
        if self.requirements is not None:
            retry = self.requirements.classify(job, capture, report)
            if retry is not None:
                return {'job_id': job_id, **retry}
        if (capture.get('execution') or {}).get('oom_killed'):
            return {'job_id': job_id, **self._defer(job, evidence, 'environment',
                {'reason': 'resources', 'job_id': job_id, 'oom_killed': True})}
        if report and report['outcome'] == 'blocked_environment':
            return {'job_id': job_id, **self._defer(job, evidence, 'environment',
                {'reason': report['blocker'], 'summary': report['summary'], 'operator_summary': report['operator_summary'],
                 'required_services': report.get('required_services', []), 'job_id': job_id})}
        if job['role'] == 'build':
            if report and report['outcome'] == 'delegate' and job.get('builder_stage') == 'planning':
                from ..store.delivery import Delivery
                try:
                    planned = Delivery(self.store, clock=self.clock).save_plan(self._command(job), job_id,
                        **self._args(job, evidence), plan=report['delegation_plan'])
                except InvalidDelegationPlan:
                    return {'job_id': job_id, **self._failure(job, {**evidence, 'reason': 'invalid_delegation_plan'})}
                return {'job_id': job_id, **planned}
            if report and report['outcome'] == 'pass' and job.get('delegation'):
                from .delegation_artifacts import verify_bundle
                from ..store.delivery import Delivery
                try:
                    bundle = self.queries.bundle(job_id)
                    inputs = [job['input_manifest']['base_oid']] + [row['commit_oid'] for row in job['input_manifest']['prerequisite_outputs']]
                    verified = verify_bundle(bundle, report['output_commit'], inputs)
                except (ValueError, OSError, subprocess.SubprocessError, TransitionError):
                    return {'job_id': job_id, **self._failure(job, {**evidence, 'reason': 'invalid_child_output'})}
                evidence.update(verified=True, commit_oid=report['output_commit'], base_oid=job['input_manifest']['base_oid'],
                                input_manifest_hash=manifest_hash(job['input_manifest']), bundle_digest=verified['digest'])
                return {'job_id': job_id, **Delivery(self.store, clock=self.clock).accept_part(self._command(job), job_id,
                    **self._args(job, evidence), commit_oid=report['output_commit'])}
            if report and report['outcome'] == 'needs_decision':
                return {'job_id': job_id, **self._defer(job, evidence, 'decision_required',
                    {**report['decision'], 'job_id': job_id})}
            if report and report['outcome'] == 'waiting_on_issues' and issue is not None:
                quote = report['evidence'].strip()
                if quote not in (issue.get('body') or '') or not all(
                        re.search(rf'(?<![\w/])#{number}\b', quote) for number in report['issues']):
                    return {'job_id': job_id, **self._failure(job, {**evidence, 'reason': 'ungrounded_dependencies'})}
                blocked = []
                for number in report['issues']:
                    prerequisite, _ = self.github.issue({**item, 'number': number})
                    if prerequisite.get('state') != 'closed':
                        blocked.append(number)
                return {'job_id': job_id, **self._defer(job, evidence, 'dependency',
                    {'issues': sorted(report['issues']), 'blocked': blocked, 'evidence': quote,
                     'requirements': requirements, 'job_id': job_id})}
            publication = (None if job.get('delegation') or job.get('builder_stage') == 'planning' else
                           self.github.publication(job, requirements, job['input_manifest']['policy_hash']))
            if publication:
                if publication['base'] != job['input_manifest']['base_oid']:
                    Executions(self.store, clock=self.clock).revoke_captured_base(self._command(job), job_id,
                        execution_fence=job['execution_fence'], input_manifest=job['input_manifest'],
                        publication=dict(pull_number=publication['pr'], branch=publication['branch'],
                            head_oid=publication['head'], base_oid=publication['base'], repository_id=job['repo_id'],
                            base_branch=job['target_branch'], author=publication['author']))
                    return {'job_id': job_id, 'pending': 'base_reconciliation'}
                identity = Candidate(publication['pr'], publication['branch'], publication['head'], publication['base'],
                    job['input_manifest']['policy_hash'], job['input_manifest'].get('requirements_hash', manifest_hash(requirements)), manifest_hash(job['input_manifest']))
                evidence.update(verified=True, head_oid=identity.head_oid, base_oid=identity.base_oid, pull_number=identity.pull_number)
                gates = [self._verification(job, report)] if report and report['outcome'] == 'needs_human_verification' else []
                if job.get('builder_stage') == 'integration':
                    from ..store.delivery import Delivery
                    finish = Delivery(self.store, clock=self.clock).publish_candidate
                else:
                    finish = self.commands.finish_job
                return {'job_id': job_id, **finish(self._command(job), job_id,
                    **self._args(job, evidence), candidate=identity, gates=gates)}
        elif job['role'] in {'review', 'audit'}:
            pr = self.github.pr(item, job['candidate']['pr'])
            if not self._current_pr(pr, job):
                return {'job_id': job_id, 'pending': 'candidate_reconciliation'}
            if report and report['outcome'] == 'needs_human_verification':
                gate = self._verification(job, report)
                return {'job_id': job_id, **self._defer(job, evidence, gate['code'], gate['details'])}
            if report and report['outcome'] in {'pass', 'reject'}:
                if self.verdicts is None:
                    return {'job_id': job_id, 'pending': 'verdict_effect'}
                receipt = self.verdicts.ensure(job, report)
                # Durable effect intent advances the version. Assignment identity
                # remains pinned; lifecycle fencing still rejects raced decisions.
                job = self.queries.job(job_id)
                if receipt is None:
                    return {'job_id': job_id, 'pending': 'verdict_effect'}
                if not self._current_pr(self.github.pr(item, job['candidate']['pr']), job):
                    return {'job_id': job_id, 'pending': 'candidate_reconciliation'}
                evidence.update(candidate_id=job['candidate_id'], verdict='pass' if report['outcome'] == 'pass' else 'fail',
                                check=receipt)
                if report['outcome'] == 'reject':
                    return {'job_id': job_id, **self.commands.reject_job(self._command(job), job_id, **self._args(job, evidence))}
                gates = []
                if job['role'] == 'audit' and (current_policy.get('human_required') or report['human_required'] != 'not_required'):
                    reason = report['human_reason'] if report['human_required'] != 'not_required' else 'Repository policy requires human approval'
                    gates = [{'code': 'approval_required', 'details': {'reason': reason, 'job_id': job_id}}]
                return {'job_id': job_id, **self.commands.finish_job(self._command(job), job_id,
                    **self._args(job, evidence), gates=gates)}
        else:
            return {'job_id': job_id, 'pending': 'unsupported_role'}

        try:
            logs = Artifacts(self.store).read(job_id, 'logs').content.decode('utf-8', errors='replace')
        except ArtifactExpired:
            logs = ''  # Retention expiry is not worker evidence.
        unavailable = self._availability(capture, logs)
        if unavailable:
            return {'job_id': job_id, **self._defer(job, evidence, 'provider_unavailable',
                {'reason': unavailable, 'retry_at': self.clock() + self.availability_retry_seconds,
                 'provider_id': job['provider_id'], 'provider': job['provider'], 'job_id': job_id})}
        evidence['reason'] = 'deadline_exceeded' if capture.get('timed_out') else 'unverified_result'
        return {'job_id': job_id, **self._failure(job, evidence)}

    def cycle(self, *, after='', limit=50):
        page = self.queries.pending(after=after, limit=limit, results=True)
        values = []
        for job_id in page['items']:
            try:
                values.append(self.step(job_id))
            except (GitHubUnavailable, TransitionError, StoreError, ArtifactCorrupt, OSError) as error:
                values.append({'job_id': job_id, 'error': getattr(error, 'code', type(error).__name__)})
        return {'items': values, 'next_cursor': page['next_cursor']}
