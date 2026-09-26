"""Deterministic reconciliation: cleanup, evidence, investigations, then dispatch.

Call cycle under the runtime's exclusive controller lock. Every external effect
has a preceding durable intent; ambiguous responses are reconciled on later cycles.
"""
from copy import deepcopy
from pathlib import Path
import time
import json

from .contracts import NEXT_ROLE, ROLES, dependencies, candidate, digest, result, references_issue, environment_key, summary as plain_summary
from .config import policy
from .memory import allocation as memory_allocation, confirmed as memory_confirmed, pending as memory_pending
from .capacity import memory_bytes
from .providers import available as provider_available
from .github import GitHubUnavailable, GitHubBudgetExhausted
from .runner import RunnerError, RunnerUnavailable, RunnerCapacity, ContainerRunner
from ..state import StateError


class Supervisor:
    def __init__(self, ledger, runner, github, settings, config, *, clock=time.time):
        self.ledger, self.runner, self.github = ledger, runner, github
        self.settings, self.config, self.clock = settings, config, clock
        self.errors = []
        self.capacity = None
        root = self.factory_root()
        if root is not None:
            from .discussions import Discussions, reserve_logins
            self.config = reserve_logins(config, Discussions(root).active_jobs())

    def cycle(self):
        self.errors = []
        self.capacity = None
        budget = self.config['cycle_budget_seconds']
        self.github.deadline = time.monotonic() + budget
        # Enforce the observation deadline at the API boundary, not just
        # between issues: one slow reconciliation must not spend launch time.
        cycle_deadline = self.github.deadline
        observe_until = cycle_deadline - budget / 2
        # Even paused/disabled factories must finish teardown and reconciliation.
        for job in self.ledger.read()['jobs'].values():
            if job['role'] == 'merge':
                continue
            try:
                self.execution(job)
            except GitHubBudgetExhausted:
                pass
            except (RunnerError, GitHubUnavailable, OSError, ValueError) as error:
                self.errors.append({'attempt': job['id'], 'phase': 'runner', 'reason': str(error)})
        self.github.deadline = observe_until
        for job in sorted(self.ledger.read()['jobs'].values(), key=lambda j: j.get('last_observed', 0)):
            if time.monotonic() >= observe_until:
                break
            if job.get('capture') is not None and job['outcome'] is None:
                try:
                    self.outcome(job)
                except GitHubBudgetExhausted:
                    pass
                except (GitHubUnavailable, ValueError, StateError) as error:
                    if job['role'] != 'recovery' and self.clock() > job.get('deadline', self.clock()) + 900:
                        self.ledger.hold(job['issue_key'], 'Outcome remains unknown; runner reset, GitHub reconciliation pending')
                    self.errors.append({'attempt': job['id'], 'phase': 'reconciliation', 'reason': str(error)})
                finally:
                    self.ledger.job_update(job['id'], last_observed=self.clock())
        for item in sorted(self.ledger.read()['issues'].values(), key=lambda i: i.get('last_observed', 0)):
            if time.monotonic() >= observe_until:
                break
            try:
                self.reconcile(item)
            except GitHubBudgetExhausted:
                pass  # The next cycle resumes with the least recently observed issue.
            except (GitHubUnavailable, ValueError, KeyError) as error:
                self.errors.append({'issue': item['key'], 'phase': 'github', 'reason': str(error)})
            finally:
                with self.ledger.transaction() as data:
                    data['issues'][item['key']]['last_observed'] = self.clock()
        self.observe_deployments(observe_until)
        self.github.deadline = cycle_deadline
        if self.config['enabled'] and time.monotonic() < self.github.deadline:
            # New issues must be found even while the queue is never empty:
            # dispatch only discovers on an empty queue, so a busy factory
            # would otherwise stop reading GitHub for new ready issues.
            if self.discovery_due():
                deadline = self.github.deadline
                # Discovery is bounded too. Leave enough time to verify rules
                # and persist a launch even with a large dependency backlog.
                self.github.deadline = time.monotonic() + min(budget / 8, max(0, deadline - time.monotonic()) / 3)
                try:
                    self.discover()
                finally:
                    self.github.deadline = deadline
            self.dispatch(discover_missing=False)
        return {'at': self.clock(), 'errors': self.errors, 'capacity': self.capacity,
                'issues': len(self.ledger.read()['issues'])}

    def factory_root(self):
        """The live factory root, never the setup copy; the unit fakes have none."""
        try:
            root = Path(self.settings.root)
        except (AttributeError, TypeError):
            return None
        return root.parent if root.name == 'setup' else root

    def observe_deployments(self, observe_until):
        """Deployment tracking is an observation: it reports, it never fails a cycle."""
        root = self.factory_root()
        if root is None or time.monotonic() >= observe_until:
            return
        from ..deployments import Deployments
        from .live import read_commit
        deadline = self.github.deadline
        self.github.deadline = min(deadline, observe_until)
        try:
            store = Deployments(root, runner=self.runner, github=self.github,
                                settings=self.settings, clock=self.clock)
            for error in store.observe(self.github, self.ledger, read_commit):
                self.errors.append({'repo': error['repo'], 'phase': 'deployment', 'reason': error['reason']})
        except (GitHubUnavailable, StateError, OSError, ValueError, KeyError, TypeError) as error:
            self.errors.append({'phase': 'deployment', 'reason': str(error)})
        finally:
            self.github.deadline = deadline

    def execution(self, job):
        runner = (ContainerRunner(job['runner_config'], environ=self.runner.environ) if isinstance(self.runner, ContainerRunner)
                  and job.get('runner_config') else self.runner)
        if job['cleanup']:
            return
        if self.clock() >= job['deadline'] and job.get('capacity_wait_since') is None and job['phase'] in ('reserved', 'created', 'launching'):
            self.ledger.job_update(job['id'], fenced=True, phase='stopping')
            job = self.ledger.read()['jobs'][job['id']]
        if job['phase'] in ('reserved', 'created', 'launching') and not job['fenced']:
            # Resuming persisted launch intents never creates another execution ID.
            if not self.config['enabled'] or not self.settings.admission(job['repo'])['allowed']:
                if job['role'] == 'recovery':
                    self.ledger.findings(job['id'], self.incomplete('Investigation paused before launch'))
                else:
                    self.ledger.wait(job['id'], 'Dispatch access or capacity is paused', self.clock() + self.config['availability_retry_seconds'])
                self.ledger.job_update(job['id'], fenced=True, phase='stopping', capture={'result': None, 'logs': '', 'missing': []})
                job = self.ledger.read()['jobs'][job['id']]
            else:
                try:
                    current = runner.inspect(job)
                    if current is None or current['status'] == 'created':
                        self.capacity = runner.capacity(job)
                        if not self.capacity['allowed']:
                            self.defer_capacity(job)
                            return
                        if job.get('capacity_wait_since') is not None:
                            self.ledger.job_update(job['id'], deadline=job['deadline'] + max(0, self.clock() - job['capacity_wait_since']), capacity_wait_since=None)
                            job = self.ledger.read()['jobs'][job['id']]
                    if job['phase'] == 'reserved':
                        try:
                            ci = self.github.ci_evidence(job)
                        except GitHubBudgetExhausted:
                            raise
                        except GitHubUnavailable:
                            self.ledger.block_environment(job['id'], 'ci_evidence', 'The controller could not obtain commit-bound CI checks. Check GitHub App access and connectivity before rechecking.',
                                                          summary=self.card({'headline': 'Stopped: Forge cannot read the automated checks',
                                                                             'plain': 'Forge needs the results of the repository’s automated checks before it can judge the work. '
                                                                                      'It could not read them from GitHub. Repair the connection, then recheck this issue.'}, job))
                            self.ledger.job_update(job['id'], fenced=True, phase='stopping', capture={'result': None, 'logs': '', 'missing': []})
                            return
                        self.ledger.job_update(job['id'], ci_evidence=ci, review_policy=policy(self.config))
                        job = self.ledger.read()['jobs'][job['id']]
                        if job.get('candidate') and job['role'] != 'recovery':
                            self.github.check(job, 'controller', job['candidate'], job['candidate']['key'], None, 'Candidate is being processed')
                            if job['role'] in ('review', 'audit'):
                                self.github.check(job, job['role'], job['candidate'], job['id'], None, 'Assigned evaluation is pending')
                        container_id = runner.create(job)
                        self.ledger.job_update(job['id'], container_id=container_id, phase='created')
                        job = self.ledger.read()['jobs'][job['id']]
                    current = runner.inspect(job)
                    if current and current['status'] == 'created':
                        token = self.github.token(job['role'], job['repo_id'], worker=True)
                        runner.prepare(job, token)
                        self.ledger.job_update(job['id'], phase='launching')
                        runner.start(job)
                    self.ledger.job_update(job['id'], phase='running')
                    job = self.ledger.read()['jobs'][job['id']]
                except RunnerCapacity as unavailable:
                    self.capacity = unavailable.decision
                    self.defer_capacity(job)
                    return
                except GitHubBudgetExhausted:
                    raise  # Persist the launch intent unchanged for the next cycle.
                except (GitHubUnavailable, RunnerUnavailable) as unavailable:
                    # Missing provider/GitHub access before launch is an availability wait.
                    if job['role'] == 'recovery' and not (job.get('provider_id') and isinstance(unavailable, RunnerUnavailable)):
                        self.ledger.findings(job['id'], self.incomplete('Investigation credentials unavailable'))
                    else:
                        self.ledger.wait(job['id'], 'blocked_auth' if isinstance(unavailable, RunnerUnavailable) else 'Role credentials unavailable', self.clock() + self.config['availability_retry_seconds'])
                    self.ledger.job_update(job['id'], fenced=True, phase='stopping')
                    job = self.ledger.read()['jobs'][job['id']]
        current = runner.inspect(job)
        finished = current is None or current['status'] in ('exited', 'dead')
        deadline = self.clock() >= job['deadline']
        if not job['fenced'] and (finished or deadline):
            capture = runner.capture(job)
            capture['timed_out'] = deadline and not finished
            self.ledger.job_update(job['id'], capture=capture, fenced=True, phase='stopping')
            job = self.ledger.read()['jobs'][job['id']]
        if not job['fenced'] and current and current['running']:
            self.ledger.job_update(job['id'], observation=runner.observe(job, current))
        if job['fenced']:
            if job.get('capture') is None:
                self.ledger.job_update(job['id'], capture=runner.capture(job))
                job = self.ledger.read()['jobs'][job['id']]
            # Reset does not wait for GitHub or an investigator. Missing evidence
            # is recorded; it does not prevent freeing a clean runner.
            verification = runner.reset(job)
            self.ledger.job_update(job['id'], teardown=verification)
            self.ledger.release(job['id'])

    def defer_capacity(self, job):
        if job.get('capacity_wait_since') is None:
            self.ledger.job_update(job['id'], capacity_wait_since=self.clock())

    def card(self, value, job):
        """Plain-language card for an outcome, from the worker's words or Forge's own."""
        return plain_summary(value, job['role'], self.clock())

    @staticmethod
    def incomplete(reason):
        return {'summary': reason, 'pattern': '', 'evidence': [], 'uncertainty': reason,
                'alternative': None, 'incomplete': True,
                'headline': 'The investigation could not finish',
                'plain': 'Forge could not look into the failed attempt. ' + reason.rstrip('.') +
                         '. The issue stays where it is until someone decides what to try next.'}

    @staticmethod
    def availability(capture):
        if (capture.get('execution') or {}).get('exit_code', 0) == 0:
            return None
        from ..worker import classify_error
        for line in capture.get('logs', '').splitlines():
            try:
                event = json.loads(line)
                if not isinstance(event, dict) or event.get('type') not in ('error', 'turn.failed'):
                    continue
                error = event.get('error') or event
                classification = classify_error(error.get('message', '') if isinstance(error, dict) else '')
                if classification in ('blocked_auth', 'blocked_quota'):
                    return classification
            except (ValueError, TypeError):
                continue
        return None

    def outcome(self, job):
        capture = job['capture']
        try:
            memory_report = result(capture.get('result'), job)
        except ValueError:
            memory_report = None
        if memory_confirmed(capture, memory_report):
            detail = (memory_report or {}).get('summary', 'Container OOM kill')
            if self.ledger.retry_memory(job['id'], self.config, detail):
                return
        if (capture.get('execution') or {}).get('oom_killed'):
            self.ledger.block_environment(job['id'], 'resources', 'The container exceeded its memory limit. The published candidate is retained; review worker capacity before rechecking.',
                                          summary=self.card({'headline': 'Stopped: the worker ran out of memory',
                                                             'plain': 'The worker used more memory than it is allowed and was stopped. Any pull request it had already opened is kept. '
                                                                      'Give workers more memory, then recheck this issue.'}, job))
            return
        # A recovery job also needs to fall back rather than turning a provider
        # outage into an incomplete investigation. Only adapter error events count.
        if job['role'] == 'recovery' and job.get('provider_id') and self.availability(capture):
            self.ledger.wait(job['id'], self.availability(capture), self.clock() + self.config['availability_retry_seconds'])
            return
        try:
            report = result(capture.get('result'), job)
        except ValueError:
            report = None
        item = self.ledger.read()['issues'][job['issue_key']]
        if report and report['outcome'] == 'blocked_environment':
            self.ledger.block_environment(job['id'], report['blocker'], report['summary'], summary=self.card(report, job))
            return
        if job['role'] == 'recovery':
            original = self.ledger.read()['jobs'][job['failure']['attempt']]
            if item['state'] in ('merged', 'cancelled'):
                findings = report if report and report['outcome'] == 'pass' else self.incomplete('Investigation ended after external issue completion')
                self.ledger.findings(job['id'], findings, summary=self.card(findings, job))
                return
            current_issue, requirements = self.github.issue(item)
            if original['role'] == 'build' and original['requirements'] == requirements and current_issue['state'] == 'open':
                recovered = self.github.publication(original, item['requirements'], original['policy'])
                if recovered:
                    self.ledger.recover_publication(original['id'], recovered)
            findings = (report if report and report['outcome'] == 'pass' else self.incomplete('Investigation did not return usable findings'))
            self.ledger.findings(job['id'], findings, summary=self.card(findings, job))
            return
        if item['active'] != job['id']:
            return
        issue, requirements = self.github.issue(item)
        if issue['state'] != 'open':
            self.ledger.cancel(item['key'], 'Issue closed externally; reconcile any published PR separately')
            return
        if requirements != job['requirements'] or job['policy'] != digest(policy(self.config)):
            self.invalidate(item['key'], 'Requirements or policy changed during the attempt')
            self.update_requirements(item['key'], requirements)
            return
        if job['role'] == 'build':
            published = self.github.publication(job, requirements, job['policy'])
            if published:
                self.ledger.accept(job['id'], candidate=published, summary=self.card(report, job))
                return
        else:
            current = self.github.pr(item, job['candidate']['pr'])
            if current.get('merged'):
                return  # Reconciliation records external completion and cleans up.
            if (current['head']['sha'] != job['candidate']['head']
                    or current['state'] != 'open' or current['draft']):
                self.pause_external_change(item, 'PR changed outside its build assignment; reconcile developer changes before resuming')
                return
            if current['base']['sha'] != job['candidate']['base']:
                return  # Reconciliation creates a fresh candidate and reruns review.
            if report and report['outcome'] in ('pass', 'reject'):
                check_id = self.github.verdict(job, report)
                # Verify revision again after publication; stale results are only history.
                verified = self.github.pr(item, job['candidate']['pr'])
                if verified['head']['sha'] != current['head']['sha']:
                    self.pause_external_change(item, 'PR changed during verdict publication; reconcile developer changes before resuming')
                    return
                if verified['base']['sha'] != current['base']['sha']:
                    return
                if report['outcome'] == 'pass':
                    self.ledger.accept(job['id'], evidence={**report, 'check_id': check_id}, summary=self.card(report, job))
                else:
                    self.ledger.fail(job['id'], report['summary'], rejection=True,
                                     evidence=[f"PR #{current['number']}, check {check_id}"], summary=self.card(report, job))
                return
        if report and report['outcome'] == 'wait':
            from ..worker import classify_error
            classification = classify_error(report['summary'])
            if classification in ('blocked_auth', 'blocked_quota'):
                self.ledger.wait(job['id'], classification, self.clock() + self.config['availability_retry_seconds'])
                return
        if report is None and self.availability(capture):
            self.ledger.wait(job['id'], self.availability(capture), self.clock() + self.config['availability_retry_seconds'])
            return
        reason = report['summary'] if report else ('Execution deadline exceeded' if capture.get('timed_out') else 'No verified publication or usable result')
        self.ledger.fail(job['id'], reason, evidence=[capture.get('logs', ''), *capture.get('missing', [])],
                         summary=self.card(report, job) or self.card(
                             {'headline': 'Stopped: the attempt did not finish',
                              'plain': 'The worker stopped without a usable result. Nothing was merged. '
                                       'Forge investigates the failure before trying again.'}, job))

    def invalidate(self, key, reason, *, candidate=None, hold=False):
        item = self.ledger.read()['issues'][key]
        old = item.get('candidate')
        if old:
            pr = self.github.pr(item, old['pr'])
            gate = candidate or {**old, 'head': pr['head']['sha']}
            self.github.check(item, 'controller', gate, gate['key'], None, reason)
        self.ledger.invalidate(key, reason, candidate=candidate, hold=hold)

    def pause_external_change(self, item, reason):
        self.invalidate(item['key'], reason, hold=True)

    def update_requirements(self, key, requirements):
        with self.ledger.transaction() as data:
            data['issues'][key]['requirements'] = requirements

    def dependency_ready(self, item, issue):
        blocked = []
        for number in dependencies(issue.get('body')):
            value = self.github.call('controller', item, 'GET', f'/issues/{number}')
            if (not isinstance(value, dict) or value.get('number') != number
                    or 'pull_request' in value or value.get('state') not in ('open', 'closed')):
                raise GitHubUnavailable('Prerequisite issue could not be verified')
            if value['state'] != 'closed':
                blocked.append(number)
        with self.ledger.transaction() as data:
            current = data['issues'][item['key']]
            if current.get('blocked_dependencies', []) != blocked:
                self.ledger.event(data, current, 'dependencies_checked', blocked=blocked)
            current['blocked_dependencies'] = blocked
            current['dependency_reason'] = ('Waiting for ' + ', '.join('#' + str(n) for n in blocked) + '.') if blocked else None
        return not blocked

    def reconcile(self, item):
        if item.get('operator_request'):
            return  # An interrupted decision must finish before observing new eligibility.
        issue, requirements = self.github.issue(item)
        if self.ledger.migrate_protection_hold(item['key']):
            item = self.ledger.read()['issues'][item['key']]
        candidate_value = item['candidate']
        if candidate_value:
            pr = self.github.pr(item, candidate_value['pr'])
            if pr.get('merged'):
                with self.ledger.transaction() as data:
                    current = data['issues'][item['key']]
                    if current['state'] != 'merged':
                        if current['active']:
                            job = data['jobs'][current['active']]
                            job['outcome'] = 'accepted' if job['role'] == 'merge' else 'external_merge'
                            if not job['cleanup']:
                                job.update(fenced=True, phase='stopping')
                        current.update(state='merged', active=None, reason=None)
                        self.ledger.event(data, current, 'merged', pr=pr['number'], sha=pr.get('merge_commit_sha'))
                completed = self.ledger.read()['issues'][item['key']]
                self.github.close_merged_issue(completed, issue)
                self.github.labels(completed)
                return
        if issue['state'] != 'open':
            self.ledger.cancel(item['key'], 'Issue closed externally')
            self.github.labels(self.ledger.read()['issues'][item['key']])
            return
        if item.get('environment_hold') and item['state'] == 'hold':
            labels = {label['name'] for label in issue.get('labels', [])}
            if labels.intersection(self.config['hold_labels']):
                self.ledger.hold(item['key'], 'Issue carries an explicit hold or dependency label')
                return
            held = item['environment_hold']
            failed_job = self.ledger.read()['jobs'].get(held['attempt'])
            if (held['blocker'] == 'ci_evidence' and failed_job and failed_job['cleanup']
                    and self.settings.admission(item['repo'])['allowed']):
                if self.clock() < held.get('recheck_after', 0):
                    return
                try:
                    self.github.ci_evidence(failed_job)
                except GitHubBudgetExhausted:
                    raise
                except GitHubUnavailable:
                    with self.ledger.transaction() as data:
                        data['issues'][item['key']]['environment_hold']['recheck_after'] = (
                            self.clock() + self.config['availability_retry_seconds'])
                    return
                self.ledger.resume_environment(item['key'], 'Commit-bound CI checks are readable again')
                item = self.ledger.read()['issues'][item['key']]
                # Continue normal requirements/candidate reconciliation before dispatch.
                held = item.get('environment_hold')
                if held is None:
                    self.reconcile(item)
                    return
            if failed_job and self.settings.admission(item['repo'])['allowed']:
                capture = failed_job.get('capture') or {}
                try:
                    report = result(capture.get('result'), failed_job)
                except ValueError:
                    report = None
                if memory_confirmed(capture, report) and self.ledger.retry_memory(
                        failed_job['id'], self.config, held['detail'], held=True):
                    return
            effective = self.memory_config(item, failed_job['role'] if failed_job else 'build')
            # A lower default is not a repair for a worker that exhausted a
            # larger tier. Keep cap-exhausted jobs held until the environment improves.
            used = (failed_job.get('runner_config') or {}).get('memory') if failed_job else None
            if used and memory_bytes(used) > memory_bytes(effective['memory']):
                effective['memory'] = used
            if (held['environment'] != environment_key(effective)
                    and self.settings.admission(item['repo'])['allowed']):
                self.ledger.resume_environment(item['key'], 'Runner environment changed; run readiness checks again')
                item = self.ledger.read()['issues'][item['key']]
        if item['state'] == 'hold' and item.get('prerequisite_hold'):
            labels = {label['name'] for label in issue.get('labels', [])}
            if labels.intersection(self.config['hold_labels']):
                self.ledger.hold(item['key'], 'Issue carries an explicit hold or dependency label')
                return
            if item['prerequisite_hold'].get('legacy'):
                if (issue.get('assignees') or 'wip' in labels
                        or any(references_issue(p.get('body'), item['repo'], item['number'])
                               for p in self.github.pulls(item))):
                    self.ledger.hold(item['key'], 'Existing work needs review before Forge starts another attempt')
                    return
            if self.settings.admission(item['repo'])['allowed'] and self.github.protections(item, {'base_ref': self.github.target(item)}):
                self.ledger.resume_protection(item['key'])
                item = self.ledger.read()['issues'][item['key']]
        if not item['active'] and not self.dependency_ready(item, issue):
            return
        if item['state'] in ('failed', 'cancelled', 'hold', 'investigating'):
            # A ready label cannot erase failures, but external closure/merge was
            # reconciled above so historical failures do not remain current alerts.
            self.github.labels(item)
            return
        labels = {l['name'] for l in issue.get('labels', [])}
        if labels.intersection(self.config['hold_labels']):
            self.ledger.hold(item['key'], 'Issue carries an explicit hold or dependency label', fence_active=True)
            return
        if requirements != item['requirements']:
            self.invalidate(item['key'], 'Issue requirements changed')
            self.update_requirements(item['key'], requirements)
            return
        if candidate_value and item['state'] not in ('ready', 'merged'):
            if self.github.target(item) != candidate_value['base_ref']:
                self.invalidate(item['key'], 'Repository default branch changed')
                return
            build = self.ledger.read()['jobs'][candidate_value['build_attempt']]
            try:
                fresh = candidate(pr, build, requirements, digest(policy(self.config)))
                fresh['branch'] = pr['head']['ref']
            except (ValueError, KeyError, TypeError):
                self.pause_external_change(item, 'PR changed outside its build assignment; reconcile developer changes before resuming')
                return
            if fresh['key'] != candidate_value['key']:
                self.invalidate(item['key'], 'Head, base, requirements or policy changed', candidate=fresh)
                return
        if item['active'] and self.ledger.read()['jobs'][item['active']]['role'] != 'merge':
            return
        if item['state'] in ('audited', 'approval_needed'):
            self.merge(item, pr)
        self.github.labels(self.ledger.read()['issues'][item['key']])

    def merge(self, item, pr):
        audit = item['evidence'].get('audit', {})
        required = self.config['human_required'] or audit.get('human_required') == 'required'
        approved, blocked = self.github.human(item, pr)
        reason = None
        if audit.get('human_required') not in ('required', 'not_required'):
            reason = 'Audit did not resolve the human approval requirement'
        elif blocked:
            reason = 'A current GitHub review requests changes'
        elif required and not approved:
            reason = audit.get('human_reason') or 'An authorized human must approve the current candidate'
        elif required and not self.github.native_human_gate(item):
            reason = 'Configure native human CODEOWNERS protection before automatically merging escalated changes'
        elif not self.github.gates(item):
            reason = 'Current review and audit checks must match the accepted evidence'
        elif not self.github.protections(item, item['candidate']):
            reason = 'The two-review and stale-approval protection baseline is missing'
        external = item['candidate']['key']
        if reason:
            self.github.check(item, 'controller', item['candidate'], external, None, reason)
            with self.ledger.transaction() as data:
                data['issues'][item['key']].update(state='approval_needed', reason=reason)
            return
        if not self.config['merge_enabled'] or not self.config['enabled'] or not self.settings.admission(item['repo'])['allowed']:
            self.github.check(item, 'controller', item['candidate'], external, None, 'Automatic merge is paused by configuration')
            return
        # Keep merge-policy pending until every fresh observation passes. GitHub
        # performs the final checks at merge time, with the exact head precondition.
        fresh = self.github.pr(item, pr['number'])
        if fresh['head']['sha'] != item['candidate']['head'] or fresh['base']['sha'] != item['candidate']['base']:
            return
        if fresh.get('mergeable') is False:
            identifier = self.merge_attempt(item)
            self.ledger.fail(identifier, 'Candidate has merge conflicts', rejection=True)
            self.github.check(item, 'controller', item['candidate'], external, 'failure', 'Return to Build to resolve merge conflicts')
            return
        if fresh.get('mergeable') is not True:
            return  # GitHub is still computing mergeability; no failed attempt.
        self.github.check(item, 'controller', item['candidate'], external, 'success', 'Technical and human gates verified for this candidate')
        # Re-read approvals immediately before requesting a merge. Native branch
        # protections handle new commits/requests for changes racing this request.
        approved, blocked = self.github.human(item, fresh)
        if blocked or required and not approved:
            self.github.check(item, 'controller', item['candidate'], external, None, 'Approval changed; waiting for current authorization')
            return
        self.merge_attempt(item)
        self.github.merge(item)
        # Neither a successful HTTP request nor a local outcome establishes merged.
        verified = self.github.pr(item, pr['number'])
        if verified.get('merged'):
            current = self.ledger.read()['issues'][item['key']]
            self.ledger.accept(current['active'], evidence={'pr': pr['number'], 'sha': verified.get('merge_commit_sha')})

    def merge_attempt(self, item):
        import uuid
        with self.ledger.transaction() as data:
            current = data['issues'][item['key']]
            if current['active']:
                return current['active']
            identifier = str(uuid.uuid4())
            data['jobs'][identifier] = {'id': identifier, 'issue_key': item['key'], 'role': 'merge',
                'candidate': deepcopy(current['candidate']), 'outcome': None, 'cleanup': True,
                'phase': 'external', 'fenced': False, 'created_at': self.clock()}
            current['active'] = identifier
            self.ledger.event(data, current, 'merge_requested', attempt=identifier)
            return identifier

    def memory_config(self, item, role, data=None):
        data = self.ledger.read() if data is None else data
        return {**self.config, 'memory': memory_allocation(self.config, data, item, role)}

    def memory_priority(self, data):
        return next((item for item in memory_pending(data, self.config)
                     if not item.get('blocked_dependencies')
                     and item.get('retry_after', 0) <= self.clock()
                     and self.settings.admission(item['repo'])['allowed']), None)

    def dispatch(self, *, discover_missing=True):
        if not self.config.get('provider_pool') and self.ledger.read().get('provider_retry_after', 0) > self.clock():
            return
        try:
            self.runner.ready()
        except RunnerError as error:
            self.errors.append({'phase': 'capacity', 'reason': str(error)})
            return
        # Recovery before Audit, Review, Build; memory admission applies to all roles.
        priority = self.memory_priority(self.ledger.read())
        roles = list(ROLES)
        if priority:
            role = priority['memory_retry']['role']
            roles.remove(role)
            roles.insert(0, role)
        for role in roles:
            for index in range(self.config['slots'][role]):
                if time.monotonic() >= self.github.deadline:
                    return
                data = self.ledger.read()
                pool = self.config.get('provider_pool', [])
                if pool and not any((self.config.get('provider_ready') is None or entry['id'] in self.config['provider_ready']) and provider_available(entry, data, self.clock()) for entry in pool):
                    return
                slot = f'{role}-{index + 1}'
                if data['slots'].get(slot):
                    continue
                item, failure = self.choose(role, data)
                if item is None and role == 'build' and discover_missing:
                    self.discover()
                    item, failure = self.choose(role, self.ledger.read())
                if item is None:
                    continue
                worker_config = self.memory_config(item, role, self.ledger.read())
                runner = (ContainerRunner(worker_config, environ=self.runner.environ)
                          if isinstance(self.runner, ContainerRunner) else self.runner)
                self.capacity = runner.capacity()
                if not self.capacity['allowed']:
                    total = self.capacity.get('total_bytes')
                    if total and (memory_bytes(worker_config['memory']) + memory_bytes(self.config['host_reserve']) > total):
                        self.ledger.hold(item['key'], 'Required worker memory exceeds host capacity; resize the host or revise memory tiers')
                    # Do not backfill small jobs while this escalation drains.
                    return
                try:
                    # Re-read at the launch boundary, including newly discovered
                    # issues that have not entered the observation rotation yet.
                    issue, requirements = self.github.issue(item)
                    if not self.dependency_ready(item, issue):
                        continue
                    if issue['state'] != 'open':
                        self.ledger.cancel(item['key'], 'Issue closed externally')
                        continue
                    if requirements != item['requirements']:
                        self.invalidate(item['key'], 'Issue requirements changed')
                        self.update_requirements(item['key'], requirements)
                        continue
                    target = self.github.target(item)
                    if role != 'recovery' and not self.github.protections(item, {'base_ref': target}):
                        self.ledger.pause_for_protection(item['key'])
                        continue
                    job = self.ledger.reserve(item['key'], role, slot, publisher=self.github.publisher(role),
                        policy=digest(policy(self.config)), timeout=self.config['timeout_seconds'][role], failure=failure, runner_config=worker_config, target_branch=target)
                    self.execution(job)
                except GitHubBudgetExhausted:
                    return
                except GitHubUnavailable as error:
                    # A bad/inaccessible prerequisite must not monopolize every
                    # free slot. Preserve the issue and let other work proceed.
                    with self.ledger.transaction() as data:
                        current = data['issues'][item['key']]
                        if not current['active']:
                            current.update(retry_after=self.clock() + self.config['availability_retry_seconds'],
                                           reason=str(error))
                    self.errors.append({'issue': item['key'], 'phase': 'dispatch', 'reason': str(error)})
                except (StateError, RunnerError) as error:
                    self.errors.append({'issue': item['key'], 'phase': 'dispatch', 'reason': str(error)})

    def choose(self, role, data):
        priority = self.memory_priority(data)
        if priority:
            if priority['memory_retry']['role'] != role:
                return None, None
            failure = next((f for f in priority['failures'] if f['investigation'] == 'pending'), None) if role == 'recovery' else None
            return priority, failure
        for item in sorted(data['issues'].values(), key=lambda i: (i['created_at'], i['repo_id'], i['number'])):
            if item.get('operator_request') or item.get('environment_hold') or item.get('blocked_dependencies'):
                continue
            if not self.settings.admission(item['repo'])['allowed'] or item.get('retry_after', 0) > self.clock():
                continue
            if any(j['issue_key'] == item['key'] and not j['cleanup'] for j in data['jobs'].values()):
                continue
            if role == 'recovery':
                failure = next((f for f in item['failures'] if f['investigation'] == 'pending'), None)
                if failure:
                    return item, failure
            elif (not item['active'] and NEXT_ROLE.get(item['state']) == role
                    and item.get('retry_after', 0) <= self.clock()):
                return item, None
        return None, None

    DISCOVERY_INTERVAL = 300

    def discovery_due(self):
        """True when any enabled repository has not been read for new issues in a while."""
        observed = self.ledger.read().get('discovery_observed', {})
        repositories = [r for r in self.settings.view()['repositories'] if r.get('dispatch_allowed')]
        return any(self.clock() - observed.get(str(r['id']), 0) >= self.DISCOVERY_INTERVAL for r in repositories)

    def discover(self):
        observed = self.ledger.read().get('discovery_observed', {})
        repositories = sorted(self.settings.view()['repositories'], key=lambda r: observed.get(str(r['id']), 0))
        for repo in repositories:
            if time.monotonic() >= getattr(self.github, 'deadline', float('inf')):
                break
            if not repo['dispatch_allowed']:
                continue
            item = {'repo': repo['name'], 'repo_id': repo['id']}
            try:
                # Existing PRs on first encounter are held for explicit adoption.
                # Never guess legacy ownership from titles or conversational links.
                pulls = self.github.pulls(item)
                known = self.ledger.read()['issues']
                for issue in sorted(self.github.issues(item), key=lambda i: i['number']):
                    if f"{repo['id']}:{issue['number']}" in known:
                        continue  # Existing issues already have their own reconciliation rotation.
                    labels = {l['name'] for l in issue.get('labels', [])}
                    hold = 'Issue carries a hold or dependency label' if labels.intersection(self.config['hold_labels']) else None
                    associated = any(references_issue(p.get('body'), repo['name'], issue['number']) for p in pulls)
                    if associated:
                        hold = 'Existing PR requires explicit assignment adoption before automation'
                    _, requirements = self.github.issue({**item, 'number': issue['number']})
                    self.ledger.discover(repo, issue['number'], requirements, hold=hold, title=issue.get('title'))
            except GitHubBudgetExhausted:
                # Interrupted mid-repository: leave it unstamped so it is the
                # first repository read next cycle, resuming where it stopped.
                break
            except (GitHubUnavailable, ValueError, KeyError) as error:
                self.errors.append({'repo': repo['name'], 'phase': 'discovery', 'reason': str(error)})
            with self.ledger.transaction() as data:
                data.setdefault('discovery_observed', {})[str(repo['id'])] = self.clock()
