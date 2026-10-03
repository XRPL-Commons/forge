"""Deterministic reconciliation: cleanup, evidence, investigations, then dispatch.

Call cycle under the runtime's exclusive controller lock. Every external effect
has a preceding durable intent; ambiguous responses are reconciled on later cycles.
"""
from copy import deepcopy
from pathlib import Path
import re
import time
import json

from .contracts import NEXT_ROLE, ROLES, dependencies, candidate, digest, result, environment_key, summary as plain_summary
from .config import policy
from .ownership import ownership
from .memory import allocation as memory_allocation, confirmed as memory_confirmed, pending as memory_pending
from .capacity import memory_bytes
from .providers import available as provider_available
from .github import GitHubUnavailable, GitHubBudgetExhausted
from .ledger import DecisionChanged
from .runner import RunnerError, RunnerUnavailable, RunnerCapacity, ContainerRunner
from ..state import AtomicStore, StateError


def classify_error(text: str) -> str:
    """Use only CLI error channels, never successful model prose, for health."""
    lowered = text.lower()
    if any(s in lowered for s in ("bwrap:", "bubblewrap is required", "no space left on device",
                                  "cannot allocate memory", "sandbox could not initialize")):
        return "failed_infrastructure"
    if any(s in lowered for s in ("permission denied", "approval required", "sandbox denied")):
        return "blocked_permission"
    if any(s in lowered for s in ("insufficient_quota", "credit balance", "usage limit",
                                  "out of credits", "exhausted credits")):
        return "blocked_quota"
    if any(s in lowered for s in ("authentication_error", "invalid_api_key", "not logged in",
                                  "please login", "please log in", "token expired", "unauthorized")):
        return "blocked_auth"
    return "failed_retryable"


class Supervisor:
    def __init__(self, ledger, runner, github, settings, config, *, clock=time.time):
        self.ledger, self.runner, self.github = ledger, runner, github
        self.settings, self.config, self.clock = settings, config, clock
        self.errors = []
        self.capacity = None
        self.environment_evidence = {}
        self.ownership_pulls = {}
        root = self.factory_root()
        if root is not None:
            from .discussions import Discussions, reserve_logins
            self.config = reserve_logins(config, Discussions(root).active_jobs())

    def cycle(self):
        self.environment_evidence = {}
        self.ownership_pulls = {}
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
        requested = self.check_requests()
        # Issues waiting for a human are observed first, at most every 45 s so they
        # cannot starve the rotation: an approval given on GitHub otherwise waited
        # minutes for the rotation to come round.
        now = self.clock()
        awaiting = lambda i: i['state'] in ('approval_needed', 'audited') and now - i.get('last_observed', 0) >= 45
        for item in sorted(self.ledger.read()['issues'].values(), key=lambda i: (
                not bool(i.get('operator_sync')), i['key'] not in requested, not awaiting(i), i.get('last_observed', 0))):
            if time.monotonic() >= observe_until:
                break
            # Merged and cancelled issues change rarely (a reopen); observing them
            # as often as open work stretched a full rotation to ~20 minutes.
            # The first observation after settling still runs: it closes the
            # GitHub issue and updates labels.
            settled = item['state'] in self.SETTLED_STATES and not item.get('active')
            if (settled and item.get('settled_observed')
                    and self.clock() - item.get('last_observed', 0) < self.SETTLED_OBSERVE_SECONDS):
                continue
            try:
                self.reconcile(item)
            except GitHubBudgetExhausted:
                # Cut short: leave it least recently observed so the next cycle
                # starts with it. Marking it observed sent a heavy issue (8 calls,
                # ~12 s) to the back of the rotation every time it was interrupted.
                # After three interruptions in a row it rotates anyway, so one
                # issue that never fits cannot starve the others.
                with self.ledger.transaction() as data:
                    current = data['issues'][item['key']]
                    current['budget_interruptions'] = current.get('budget_interruptions', 0) + 1
                    if current['budget_interruptions'] >= 3:
                        current.update(last_observed=self.clock(), budget_interruptions=0)
                break
            except DecisionChanged:
                continue  # Keep the new decision first in the next observation rotation.
            except (GitHubUnavailable, ValueError, KeyError) as error:
                self.errors.append({'issue': item['key'], 'phase': 'github', 'reason': str(error)})
            # Observed only when the reconcile finished or failed on its own terms.
            with self.ledger.transaction() as data:
                data['issues'][item['key']]['last_observed'] = self.clock()
                data['issues'][item['key']].pop('budget_interruptions', None)
                if settled:
                    data['issues'][item['key']]['settled_observed'] = True
                else:
                    data['issues'][item['key']].pop('settled_observed', None)
            if item['key'] in requested:
                self.check_requests(done=(item['key'], requested[item['key']]))
        self.observe_deployments(observe_until)
        # Launching gets at least half the budget even when observation ran
        # over: otherwise a long launch path (academy #640: 17 dependency
        # reads) ran out every cycle and was silently retried forever.
        self.github.deadline = max(cycle_deadline, time.monotonic() + budget / 2)
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
            if report['blocker'] == 'services' and self.ledger.retry_services(job['id'], self.config, report['required_services']):
                return
            self.ledger.block_environment(job['id'], report['blocker'], report['summary'], summary=self.card(report, job),
                                          required_services=report.get('required_services'))
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
            with self.ledger.transaction() as data:
                data['issues'][item['key']]['ownership_wait'] = None
            self.ledger.cancel(item['key'], 'Issue closed externally; reconcile any published PR separately')
            return
        if requirements != job['requirements'] or job['policy'] != digest(policy(self.config)):
            self.invalidate(item['key'], 'Requirements or policy changed during the attempt')
            self.update_requirements(item['key'], requirements)
            return
        if job['role'] == 'build':
            if report and report['outcome'] == 'needs_decision':
                self.ledger.request_decision(job['id'], report['decision'], summary=self.card(report, job))
                return
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

    def ownership_ready(self, item, issue, pulls=None, *, fresh=False):
        # Share a repository read within observation, but refresh at dispatch
        # and merge boundaries. No cache survives a controller cycle.
        key = item['repo_id']
        if pulls is not None:
            self.ownership_pulls[key] = pulls
        elif fresh or key not in self.ownership_pulls:
            self.ownership_pulls[key] = self.github.pulls(item)
        owner = ownership(issue, item, item['repo'], self.ownership_pulls[key])
        with self.ledger.transaction() as data:
            current = data['issues'][item['key']]
            if current.get('ownership_wait') != owner:
                self.ledger.event(data, current, 'ownership_changed', owner=owner)
            current['ownership_wait'] = owner
        return owner is None

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
            current['dependencies_checked_at'] = self.clock()
        return not blocked

    DEPENDENCY_RECHECK_SECONDS = 120

    def dependencies_recently_clear(self, item):
        """Observation confirmed every dependency closed moments ago; a body
        change is caught by the requirements comparison that follows."""
        return (not item.get('blocked_dependencies')
                and 0 <= self.clock() - item.get('dependencies_checked_at', -1e9) < self.DEPENDENCY_RECHECK_SECONDS)

    def reconcile(self, item):
        with self.ledger.observing(item):
            self.reconcile_current(item)

    def reconcile_current(self, item):
        if item.get('operator_sync'):
            from .issue_actions import IssueActions
            root = self.factory_root() or self.ledger.store.root.parent.parent
            IssueActions(root, self.ledger, settings=self.settings, github=self.github).sync(item)
            item = self.ledger.read()['issues'][item['key']]
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
                        current.update(state='merged', active=None, reason=None, ownership_wait=None)
                        self.ledger.event(data, current, 'merged', pr=pr['number'], sha=pr.get('merge_commit_sha'))
                completed = self.ledger.read()['issues'][item['key']]
                self.github.close_merged_issue(completed, issue)
                self.github.labels(completed)
                return
        if issue['state'] != 'open':
            with self.ledger.transaction() as data:
                data['issues'][item['key']]['ownership_wait'] = None
            self.ledger.cancel(item['key'], 'Issue closed externally')
            self.github.labels(self.ledger.read()['issues'][item['key']])
            return
        ownership_clear = self.ownership_ready(item, issue, fresh=item['state'] in ('audited', 'approval_needed'))
        if not ownership_clear and not item.get('active'):
            return
        if item['state'] == 'hold' and item.get('decision_request') and not item.get('active'):
            # A worker that asked "should this wait for #90?" when the issue itself
            # names #90 as a prerequisite: Forge's own dependency wait answers it.
            asked = item['decision_request']
            text = ' '.join(str(asked.get(k) or '') for k in ('question', 'recommendation', 'evidence'))
            named = [n for n in dependencies(issue.get('body')) if re.search(rf'(?<![\w/])#{n}\b', text)]
            if named:
                self.ledger.retry(item['key'], 'Waits for prerequisites named in the issue: '
                                  + ', '.join('#' + str(n) for n in named))
                self.reconcile(self.ledger.read()['issues'][item['key']])
                return
        if item.get('environment_hold') and item['state'] == 'hold':
            labels = {label['name'] for label in issue.get('labels', [])}
            if labels.intersection(self.config['hold_labels']):
                self.ledger.hold(item['key'], 'Issue carries an explicit hold or dependency label')
                return
            held = item['environment_hold']
            if held['blocker'] == 'dependency_access' and dependencies(issue.get('body')):
                # Workers reported "wait for #N" as an access problem. Once Forge can
                # read the prerequisites itself, the ordinary dependency wait applies.
                if self.ledger.resume_environment(item['key'], 'The issue names prerequisite issues; waiting on them instead'):
                    self.reconcile(self.ledger.read()['issues'][item['key']])
                return
            if held['blocker'] == 'services':
                # Older installations held missing databases for manual approval.
                # Resume once under the same bounded policy as a new blocker.
                from .environments import choose
                selected = choose(self.config, item, issue)
                if selected['automatic'] and self.settings.admission(item['repo'])['allowed']:
                    self.ledger.retry_services(held['attempt'], self.config, held.get('required_services', []), held=True)
                return
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
        if ownership_clear and item['state'] in ('audited', 'approval_needed'):
            self.merge(item, pr)
        self.github.labels(self.ledger.read()['issues'][item['key']])

    def merge(self, item, pr):
        audit = item['evidence'].get('audit', {})
        required = self.config['human_required'] or audit.get('human_required') == 'required'
        approvals = self.github.approvals(item, pr)
        blocked = any(a['state'] == 'CHANGES_REQUESTED' for a in approvals)
        approved = any(a['counted'] for a in approvals) and not blocked
        reason = None
        if audit.get('human_required') not in ('required', 'not_required'):
            reason = 'Audit did not resolve the human approval requirement'
        elif blocked:
            reason = 'A current GitHub review requests changes'
        elif required and not approved:
            reason = audit.get('human_reason') or 'An authorized human must approve the current candidate'
        elif not self.github.gates(item):
            reason = 'Current review and audit checks must match the accepted evidence'
        elif not self.github.protections(item, item['candidate']):
            reason = 'The two-review and stale-approval protection baseline is missing'
        external = item['candidate']['key']
        # A conflicted candidate cannot merge whatever a person decides, so it
        # goes back to Build before anyone is asked to approve it.
        fresh = self.github.pr(item, pr['number'])
        if fresh['head']['sha'] == item['candidate']['head'] and fresh.get('mergeable') is False:
            target = item['candidate'].get('base_ref') or 'the target branch'
            # Publish first: once the issue leaves approval it is not checked here
            # again, so a check lost after the move would stay "waiting" forever.
            self.github.check(item, 'controller', item['candidate'], external, 'failure',
                              f'Conflicts with {target}; Forge is returning it to Build to update the branch')
            self.ledger.resolve_conflict(item['key'], target)
            return
        missing_approval = (required and not approved and not blocked
                            and audit.get('human_required') in ('required', 'not_required'))
        gate_kind = 'approval' if missing_approval else 'blocked' if reason else 'ready'
        with self.ledger.transaction() as data:
            data['issues'][item['key']]['merge_gate'] = {
                'candidate': item['candidate']['key'], 'kind': gate_kind, 'reason': reason,
                'approvals': approvals,
            }
        if reason:
            self.github.check(item, 'controller', item['candidate'], external, None, reason)
            with self.ledger.transaction() as data:
                current = data['issues'][item['key']]
                if current['state'] != 'approval_needed':
                    self.ledger.event(data, current, 'state_changed', previous=current['state'], state='approval_needed')
                current.update(state='approval_needed', reason=reason)
            return
        if not self.config['merge_enabled'] or not self.config['enabled'] or not self.settings.admission(item['repo'])['allowed']:
            self.github.check(item, 'controller', item['candidate'], external, None, 'Automatic merge is paused by configuration')
            return
        # Keep merge-policy pending until every fresh observation passes. GitHub
        # performs the final checks at merge time, with the exact head precondition.
        if fresh['head']['sha'] != item['candidate']['head'] or fresh['base']['sha'] != item['candidate']['base']:
            return
        if fresh.get('mergeable') is not True:
            return  # GitHub is still computing mergeability; no failed attempt.
        self.github.check(item, 'controller', item['candidate'], external, 'success', 'Technical and human gates verified for this candidate')
        # Re-read approvals immediately before requesting a merge; the merge is
        # pinned to the candidate head, so a new commit racing it fails.
        approved, blocked = self.github.human(item, fresh)
        if blocked or required and not approved:
            self.github.check(item, 'controller', item['candidate'], external, None, 'Approval changed; waiting for current authorization')
            return
        self.merge_attempt(item)
        try:
            self.github.merge(item)
        except GitHubUnavailable as error:
            with self.ledger.transaction() as data:
                data['issues'][item['key']]['merge_gate'] = {
                    'candidate': item['candidate']['key'], 'kind': 'refused', 'reason': str(error),
                }
            raise
        # Neither a successful HTTP request nor a local outcome establishes merged.
        verified = self.github.pr(item, pr['number'])
        if verified.get('merged'):
            current = self.ledger.read()['issues'][item['key']]
            self.ledger.accept(current['active'], evidence={'pr': pr['number'], 'sha': verified.get('merge_commit_sha')})

    def check_requests(self, done=None):
        """Issues the operator asked to check now (after approving on GitHub, say)."""
        store = getattr(self.ledger, 'store', None)
        if store is None:
            return {}
        with AtomicStore(store.root, 'check-requests').transaction() as data:
            if done and data.get(done[0]) == done[1]:
                del data[done[0]]  # A newer request for the same issue stays queued.
            return dict(data)

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
        from .environments import worker_config
        config = worker_config(self.config, item)
        allocated = memory_allocation(self.config, data, item, role)
        config['memory'] = max((config['memory'], allocated), key=memory_bytes)
        return config

    def select_environment(self, item):
        from .environments import choose
        issue, _ = self.github.issue(item)
        if (not self.config['environment_tiers'] and not self.config['repository_environments']
                and not any(l['name'].startswith('env:') for l in issue.get('labels', []))):
            return item
        pending = item.get('environment_label_pending')
        if pending:
            observed = {l['name'] for l in issue.get('labels', []) if l['name'].startswith('env:')}
            before, target = set(pending['before']), pending['target']
            if observed not in (before, before | {target}, {target}):
                raise ValueError('Environment label changed while selection was being applied; review the issue')
            self.github.environment_label(item, issue, target)
            with self.ledger.transaction() as data:
                data['issues'][item['key']].pop('environment_label_pending', None)
            item = self.ledger.read()['issues'][item['key']]
            issue, _ = self.github.issue(item)
        root = self.factory_root()
        if root is not None and 'full' in self.config['environment_tiers']:
            from ..repository_environments import RepositoryEnvironments
            key = str(item['repo_id'])
            if key not in self.environment_evidence:
                try:
                    self.environment_evidence[key] = RepositoryEnvironments(self.settings).automatic(item)
                except (OSError, ValueError):
                    self.environment_evidence[key] = {}  # Scan failures never block an otherwise eligible issue.
        choice = choose(self.config, item, issue, self.environment_evidence.get(str(item['repo_id'])))
        label = 'env:' + choice['tier']
        # Persist intent before publishing the label, so interruption after the
        # GitHub write cannot turn our own selection into an operator override.
        with self.ledger.transaction() as data:
            current = data['issues'][item['key']]
            if choice != current.get('environment_choice'):
                self.ledger.event(data, current, 'environment_selected', **choice)
            current.update(environment_choice=choice, environment_label=label)
            current['environment_label_pending'] = {'before': [l['name'] for l in issue.get('labels', [])
                                                                if l['name'].startswith('env:')], 'target': label}
        self.github.environment_label(item, issue, label)
        with self.ledger.transaction() as data:
            data['issues'][item['key']].pop('environment_label_pending', None)
        return self.ledger.read()['issues'][item['key']]

    def memory_priority(self, data):
        return next((item for item in memory_pending(data, self.config)
                     if not item.get('operator_sync') and not item.get('operator_hold') and not item.get('blocked_dependencies') and not item.get('ownership_wait')
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
                if pool and not any((self.config.get('provider_ready') is None or entry['id'] in self.config['provider_ready']) and provider_available(entry, data, self.clock(), codex_parallel_auth=self.config.get('codex_parallel_auth', False)) for entry in pool):
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
                try:
                    with self.ledger.observing(item):
                        try:
                            if self.config['environment_tiers'] or self.config['repository_environments'] or item.get('environment_choice'):
                                item = self.select_environment(item)
                        except GitHubBudgetExhausted:
                            return
                        except (ValueError, GitHubUnavailable) as error:
                            with self.ledger.transaction() as data:
                                data['issues'][item['key']].update(reason=str(error),
                                    retry_after=self.clock() + self.config['availability_retry_seconds'])
                            self.errors.append({'issue': item['key'], 'phase': 'environment', 'reason': str(error)})
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
                            if not self.ownership_ready(item, issue, fresh=True):
                                continue
                            if item.get('decision_version'):
                                from .issue_actions import IssueActions
                                root = self.factory_root() or self.ledger.store.root.parent.parent
                                IssueActions(root, self.ledger, settings=self.settings, github=self.github).prerequisites(item, issue, item)
                            if {label['name'] for label in issue.get('labels', [])}.intersection(self.config['hold_labels']):
                                self.ledger.hold(item['key'], 'Issue carries an explicit hold or dependency label')
                                continue
                            if not self.config['environment_tiers'] and not self.config['repository_environments']:
                                from .environments import choose
                                choose(self.config, item, issue)  # Reject unavailable explicit tiers even when opt-in is off.
                            if isinstance(runner, ContainerRunner) and worker_config['environment_tier'] == 'full':
                                runner.ready()
                            if not self.dependencies_recently_clear(item) and not self.dependency_ready(item, issue):
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
                            publisher = self.github.publisher(role)
                            draft = (self.github.own_draft(item, item['number'], publisher)
                                     if role == 'build' and not item.get('candidate') else None)
                            job = self.ledger.reserve(item['key'], role, slot, publisher=publisher,
                                policy=digest(policy(self.config)), timeout=self.config['timeout_seconds'][role], failure=failure, runner_config=worker_config, target_branch=target,
                                existing_draft=draft)
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
                        except ValueError as error:
                            with self.ledger.transaction() as data:
                                data['issues'][item['key']].update(reason=str(error),
                                    retry_after=self.clock() + self.config['availability_retry_seconds'])
                            self.errors.append({'issue': item['key'], 'phase': 'environment', 'reason': str(error)})
                        except (StateError, RunnerError) as error:
                            self.errors.append({'issue': item['key'], 'phase': 'dispatch', 'reason': str(error)})
                except StateError as error:
                    self.errors.append({'issue': item['key'], 'phase': 'dispatch', 'reason': str(error)})

    def choose(self, role, data):
        priority = self.memory_priority(data)
        if priority:
            if priority['memory_retry']['role'] != role:
                return None, None
            failure = next((f for f in priority['failures'] if f['investigation'] == 'pending'), None) if role == 'recovery' else None
            return priority, failure
        for item in sorted(data['issues'].values(), key=lambda i: (i['created_at'], i['repo_id'], i['number'])):
            if item.get('operator_request') or item.get('operator_sync') or item.get('operator_hold') or item.get('environment_hold') or item.get('blocked_dependencies') or item.get('ownership_wait'):
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
    SETTLED_STATES = ('merged', 'cancelled')
    SETTLED_OBSERVE_SECONDS = 3600

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
                # Claiming PRs wait for handoff and are checked again on observation.
                pulls = self.github.pulls(item)
                known = self.ledger.read()['issues']
                for issue in sorted(self.github.issues(item), key=lambda i: i['number']):
                    if f"{repo['id']}:{issue['number']}" in known:
                        continue  # Existing issues already have their own reconciliation rotation.
                    labels = {l['name'] for l in issue.get('labels', [])}
                    hold = 'Issue carries a hold or dependency label' if labels.intersection(self.config['hold_labels']) else None
                    _, requirements = self.github.issue({**item, 'number': issue['number']})
                    key = self.ledger.discover(repo, issue['number'], requirements, hold=hold, title=issue.get('title'))
                    self.ownership_ready(self.ledger.read()['issues'][key], issue, pulls)
            except GitHubBudgetExhausted:
                # Interrupted mid-repository: leave it unstamped so it is the
                # first repository read next cycle, resuming where it stopped.
                break
            except (GitHubUnavailable, ValueError, KeyError, StateError) as error:
                self.errors.append({'repo': repo['name'], 'phase': 'discovery', 'reason': str(error)})
            with self.ledger.transaction() as data:
                data.setdefault('discovery_observed', {})[str(repo['id'])] = self.clock()
