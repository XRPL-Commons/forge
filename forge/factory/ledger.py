"""Serialized issue ledger and durable launch intents, independent of terminals."""
from contextlib import contextmanager
from copy import deepcopy
from .providers import available as provider_available
from .accounts import cooldown_since
import time
import json
import uuid

from ..state import AtomicStore, StateError
from .contracts import MAX_FAILURES, NEXT_ROLE, PASS_STATE, RETRY_STATE, ROLES, STATES

# A human records the new approach and retries; nothing else releases this hold.
APPROACH_HOLD = 'Repeated failure requires a supported change of approach'


class DecisionChanged(StateError):
    """An operator superseded the supervisor's observation."""


class Ledger:
    def __init__(self, root, clock=time.time):
        self.store = AtomicStore(root, 'factory-ledger')
        self.clock = clock
        self.decision_fence = None

    @contextmanager
    def observing(self, item):
        previous = self.decision_fence
        self.decision_fence = (item['key'], item.get('decision_version', 0))
        try:
            self.read()  # Reject an already stale snapshot before external work.
            yield
        finally:
            self.decision_fence = previous

    @contextmanager
    def transaction(self):
        with self.store.transaction() as data:
            if data or self.store.marker.exists():
                self.validate(data)
            data.setdefault('version', 1)
            for key in ('issues', 'jobs', 'slots'):
                data.setdefault(key, {})
            data.setdefault('events', [])
            self.check_decision(data)
            yield data
            self.validate(data)

    def check_decision(self, data):
        if self.decision_fence:
            key, version = self.decision_fence
            if data['issues'].get(key, {}).get('decision_version', 0) != version:
                raise DecisionChanged('An operator changed this issue; observe it again.')

    @staticmethod
    def validate(data):
        if data.get('version') != 1 or any(not isinstance(data.get(k), dict) for k in ('issues', 'jobs', 'slots')) or not isinstance(data.get('events'), list):
            raise StateError('Incomplete or corrupt initialized factory ledger; reconcile before recovery')
        try:
            json.dumps(data, allow_nan=False)
            for key, item in data['issues'].items():
                if item['key'] != key or item['state'] not in STATES or not 0 <= item['budget_start'] <= len(item['failures']):
                    raise ValueError()
                if item['active'] is not None and (item['active'] not in data['jobs'] or data['jobs'][item['active']]['issue_key'] != key):
                    raise ValueError()
            for key, job in data['jobs'].items():
                if job['id'] != key or job['role'] not in (*ROLES, 'merge') or job['issue_key'] not in data['issues'] or type(job['cleanup']) is not bool:
                    raise ValueError()
                if not job['cleanup'] and data['slots'].get(job['slot']) != key:
                    raise ValueError()
            for slot, key in data['slots'].items():
                job = data['jobs'][key]
                if job['cleanup'] or job['slot'] != slot:
                    raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise StateError('Factory ownership invariants are invalid; reconcile before recovery') from None

    def read(self):
        data = self.store.read()
        if not data or not self.store.marker.exists():
            # Preserve first-use initialization and its durable marker.
            with self.transaction() as data:
                return deepcopy(data)
        self.validate(data)
        self.check_decision(data)
        return data

    def event(self, data, issue, kind, **fields):
        data['events'].append({'sequence': len(data['events']) + 1, 'at': self.clock(),
                               'issue': issue['key'], 'kind': kind, **fields})

    def discover(self, repo, number, requirements, *, hold=None, title=None):
        key = f"{repo['id']}:{number}"
        with self.transaction() as data:
            self.discover_in(data, repo, number, requirements, hold=hold, title=title)
        return key

    def discover_in(self, data, repo, number, requirements, *, hold=None, title=None):
        key = f"{repo['id']}:{number}"
        if key not in data['issues']:
            issue = {'key': key, 'repo_id': repo['id'], 'repo': repo['name'], 'number': number,
                     'title': title or f'Issue #{number}', 'state': 'hold' if hold else 'ready', 'reason': hold, 'requirements': requirements,
                     'candidate': None, 'active': None, 'failures': [], 'budget_start': 0,
                     'retry_plan': None, 'evidence': {}, 'created_at': self.clock()}
            data['issues'][key] = issue
            self.event(data, issue, 'discovered', reason=hold)

    def reserve(self, key, role, slot, *, publisher, policy, timeout, failure=None, provider='default', runner_config=None, target_branch='main',
                existing_draft=None):
        """One atomic ownership transfer before any external process launch."""
        with self.transaction() as data:
            issue = data['issues'][key]
            if issue.get('operator_sync'):
                raise StateError('Confirm the operator decision against GitHub before dispatch')
            if issue.get('operator_hold'):
                raise StateError('An operator hold prevents dispatch')
            if any(j['issue_key'] == key and not j['cleanup'] for j in data['jobs'].values()):
                raise StateError('Previous execution must finish cleanup before replacement')
            if data['slots'].get(slot):
                raise StateError('Slot has an execution whose teardown is not verified')
            if role == 'recovery':
                if failure is None or failure.get('investigation') != 'pending':
                    raise StateError('Failure has already been investigated or assigned')
                actual = next(f for f in issue['failures'] if f['attempt'] == failure['attempt'])
                if actual['investigation'] != 'pending':
                    raise StateError('Investigation is already assigned')
            elif issue['active'] or NEXT_ROLE.get(issue['state']) != role:
                raise StateError('Issue is not dispatchable for this role')
            runner_config = deepcopy(runner_config)
            pool = (runner_config or {}).get('provider_pool', [])
            provider_id = None
            if pool:
                start = data.get('provider_cursor', 0) % len(pool)
                selected = next(((start + offset) % len(pool) for offset in range(len(pool))
                                 if ((runner_config.get('provider_ready') is None or pool[(start + offset) % len(pool)]['id'] in runner_config['provider_ready'])
                                     and provider_available(pool[(start + offset) % len(pool)], data, self.clock(),
                                                            codex_parallel_auth=runner_config.get('codex_parallel_auth', False)))), None)
                if selected is None:
                    raise StateError('All coding providers are temporarily unavailable')
                entry = pool[selected]
                data['provider_cursor'] = (selected + 1) % len(pool)
                runner_config.update({k: v for k, v in entry.items() if k != 'id'})
                provider, provider_id = entry['provider'], entry['id']
            identifier = str(uuid.uuid4())
            retry = issue.get('memory_retry') or {}
            if not retry and role == 'build':
                from .memory import confirmed
                from .contracts import result
                previous = max((j for j in data['jobs'].values() if j['issue_key'] == key and j['role'] == role),
                               key=lambda j: j['created_at'], default=None)
                if previous:
                    capture = previous.get('capture') or {}
                    try:
                        report = result(capture.get('result'), previous)
                    except ValueError:
                        report = None
                    # Failed builds can publish a useful draft without an accepted
                    # candidate. Continue that assigned branch instead of
                    # orphaning its PR on a fresh branch after investigation.
                    if (previous['outcome'] in ('blocked_environment', 'waiting', 'failed', 'needs_decision', 'services_retry', 'waiting_on_issues')
                            or ((runner_config or {}).get('memory_tiers') and confirmed(capture, report))):
                        retry = {'role': role, 'branch': previous['branch']}
            # GitHub's open draft is the truth about where the work is; the last
            # job's branch only stands in when there is none.
            if role == 'build' and not issue['candidate'] and existing_draft:
                retry = {'role': role, 'branch': existing_draft['branch']}
                self.event(data, issue, 'draft_resumed', attempt=identifier,
                           pr=existing_draft['pr'], branch=existing_draft['branch'],
                           superseded=existing_draft['superseded'],
                           reason=f"Continuing PR #{existing_draft['pr']}." + (
                               ' Superseded drafts left open for operator review: '
                               + ', '.join('#' + str(n) for n in existing_draft['superseded']) + '.'
                               if existing_draft['superseded'] else ''))
            branch = (issue['candidate'].get('branch') if issue['candidate'] else None) or (retry.get('branch') if role == 'build' else None) or f"forge/issue-{issue['number']}-{identifier[:8]}"
            job = {'id': identifier, 'issue_key': key, 'repo_id': issue['repo_id'], 'repo': issue['repo'],
                   'issue_number': issue['number'], 'role': role, 'slot': slot, 'provider': provider,
                   'provider_id': provider_id, 'model': (runner_config or {}).get('model'),
                   'publisher': publisher, 'branch': branch, 'target_branch': target_branch, 'runner_config': deepcopy(runner_config), 'candidate': deepcopy(issue['candidate']),
                   'policy': policy, 'requirements': issue['requirements'],
                   'created_at': self.clock(), 'deadline': self.clock() + timeout,
                   'phase': 'reserved', 'outcome': None, 'cleanup': False, 'fenced': False,
                   'history': {'failures': [{'attempt': f['attempt'], 'role': f['role'], 'cause': f['cause'][:2000],
                                             'findings': deepcopy(f.get('findings'))} for f in issue['failures'][-3:]],
                               'retry_plan': issue['retry_plan']}}
            if retry and retry['role'] == role:
                job['resume_branch'] = retry.get('branch') if role == 'build' else None
            if role == 'build' and existing_draft and not issue['candidate']:
                job['resume_pr'] = existing_draft['pr']
            if role == 'recovery':
                job['failure'] = deepcopy(failure)
                actual['investigation'] = identifier
            else:
                issue['active'] = identifier
            data['jobs'][identifier] = job
            data['slots'][slot] = identifier
            self.event(data, issue, 'assigned', attempt=identifier, role=role, slot=slot,
                       provider=provider, model=job['model'], provider_id=provider_id)
            return deepcopy(job)

    def job_update(self, identifier, **fields):
        with self.transaction() as data:
            job = data['jobs'][identifier]
            if job['cleanup'] and any(k in fields for k in ('phase', 'fenced')):
                raise StateError('An old execution cannot be relaunched')
            job.update(fields)

    def release(self, identifier):
        with self.transaction() as data:
            job = data['jobs'][identifier]
            if data['slots'].get(job['slot']) not in (None, identifier):
                raise StateError('Stale release cannot free a replacement')
            if not job['fenced']:
                raise StateError('Execution must be fenced before release')
            data['slots'].pop(job['slot'], None)
            job.update(cleanup=True, phase='cleaned')
            self.event(data, data['issues'][job['issue_key']], 'runner_released', attempt=identifier)

    def remember(self, issue, summary, container=None):
        """The plain-language card of the deciding result, kept with its outcome."""
        if not summary:
            return
        issue['last_summary'] = summary
        if container is not None:
            container['summary'] = summary

    def accept(self, identifier, *, candidate=None, evidence=None, summary=None):
        with self.transaction() as data:
            job, issue = self._current(data, identifier)
            if job['outcome'] is not None:
                return False
            if job.get('cancelled'):
                return False
            if candidate is not None:
                candidate = {**candidate, 'accepted_at': self.clock()}
                issue['candidate'] = candidate
                issue['evidence'] = {}
            elif job['candidate'] != issue['candidate']:
                raise StateError('Candidate changed during execution')
            issue['state'] = PASS_STATE[job['role']]
            issue['reason'] = None
            issue['active'] = None
            if evidence:
                issue['evidence'][job['role']] = evidence
            # The verdict's own report already carries headline and plain; the
            # accepted candidate is the outcome record that otherwise has none.
            self.remember(issue, summary, issue['candidate'] if candidate is not None else None)
            job['outcome'] = 'accepted'
            self.learn_memory(data, issue, job)
            self.event(data, issue, 'accepted', attempt=identifier, role=job['role'], candidate=issue['candidate'])
            return True

    @staticmethod
    def _current(data, identifier):
        job = data['jobs'][identifier]
        issue = data['issues'][job['issue_key']]
        if issue['active'] != identifier and job['outcome'] is None:
            raise StateError('Obsolete attempt cannot change the issue')
        return job, issue

    def fail(self, identifier, cause, *, rejection=False, evidence=None, summary=None):
        with self.transaction() as data:
            job, issue = self._current(data, identifier)
            if job['outcome'] is not None:
                return False
            job['outcome'] = 'failed'
            failure = {'attempt': identifier, 'role': job['role'], 'cause': cause,
                       'evidence': evidence or [], 'investigation': 'pending', 'findings': None,
                       'retry_state': 'ready' if rejection else RETRY_STATE[job['role']],
                       'at': self.clock(), 'charged': True}
            issue['failures'].append(failure)
            self.remember(issue, summary, failure)
            issue['active'] = None
            issue['state'] = 'failed' if self.failure_count(issue) >= MAX_FAILURES else 'investigating'
            issue['reason'] = cause
            if rejection:
                issue['evidence'] = {}
            self.event(data, issue, 'failed_attempt', attempt=identifier, cause=cause, count=self.failure_count(issue))
            return True

    def resolve_conflict(self, key, target):
        """The target moved under a finished candidate: rebuild it on the same branch.

        Not a failed attempt: the change was fine when made, so no budget is
        charged. Review and audit run again on the updated head."""
        with self.transaction() as data:
            issue = data['issues'][key]
            job = data['jobs'].get(issue['active']) if issue['active'] else None
            if job and job['role'] == 'merge' and job['outcome'] is None:
                # A merge request GitHub refused leaves its job open; nothing merged, so
                # it must not pin a conflicted candidate forever (thegame #53/#57/#64).
                job.update(outcome='superseded', fenced=True)
                issue['active'] = None
            if issue['active'] or not issue['candidate']:
                return False
            plan = (f'The pull request now conflicts with the latest {target}. Update the existing branch from {target}, '
                    'resolve the conflicts without changing the intended behaviour, run the checks again and push to the same pull request.')
            issue.update(state='ready', retry_plan=plan, reason=f'Conflicts with {target}; returning to Build to update the branch',
                         evidence={})
            self.event(data, issue, 'candidate_conflict', reason=plan)
            return True

    @staticmethod
    def failure_count(issue):
        return sum(f['charged'] for f in issue['failures'][issue['budget_start']:])

    def findings(self, identifier, findings, summary=None):
        with self.transaction() as data:
            job = data['jobs'][identifier]
            if job['role'] != 'recovery' or job['outcome'] is not None:
                return
            issue = data['issues'][job['issue_key']]
            failure = next(f for f in issue['failures'] if f['attempt'] == job['failure']['attempt'])
            failure.update(investigation='complete', findings=findings)
            self.remember(issue, summary)
            job['outcome'] = 'investigated'
            if not findings.get('incomplete'):
                self.learn_memory(data, issue, job)
            self.event(data, issue, 'investigated', attempt=failure['attempt'], findings=findings)
            if issue['state'] != 'investigating':
                return
            related = [f for f in issue['failures'][issue['budget_start']:] if f['charged']]
            pattern = findings.get('pattern')
            repeated = bool(pattern and sum((f.get('findings') or {}).get('pattern') == pattern for f in related) >= 2)
            alternative = findings.get('alternative')
            # Missing evidence is itself a reason to stop guessing after two attempts.
            uncertain = len(related) >= 2 and (not pattern or findings.get('incomplete'))
            prior_changes = {str((f.get('findings') or {}).get('alternative')) for f in related[:-1]}
            if (repeated or uncertain) and (not alternative or not findings.get('evidence') or str(alternative) in prior_changes):
                issue.update(state='hold', reason=APPROACH_HOLD)
            else:
                issue.update(state=failure['retry_state'], retry_plan=alternative or findings['summary'])

    def recover_publication(self, identifier, candidate):
        """Correct one confirmed build failure after independent GitHub verification."""
        with self.transaction() as data:
            job = data['jobs'][identifier]
            issue = data['issues'][job['issue_key']]
            if job['role'] != 'build' or job['outcome'] != 'failed' or issue['active'] or issue['state'] not in ('investigating', 'failed'):
                return False
            failures = [f for f in issue['failures'][issue['budget_start']:] if f['charged']]
            if not failures or failures[-1]['attempt'] != identifier:
                return False
            failure = failures[-1]
            failure['charged'] = False
            failure['correction'] = 'Exact submitted PR recovered from GitHub'
            job['outcome'] = 'accepted'
            issue.update(state='built', candidate={**candidate, 'accepted_at': self.clock()}, reason=None, evidence={})
            self.event(data, issue, 'failure_corrected', attempt=identifier, candidate=candidate)
            return True

    def learn_memory(self, data, issue, job):
        from .memory import profile_key
        from .capacity import memory_bytes
        config = job.get('runner_config') or {}
        if config.get('memory_tiers'):
            profiles = data.setdefault('memory_profiles', {})
            key = profile_key(job['repo_id'], job['role'])
            previous = profiles.get(key, {}).get('memory', config['memory_tiers'][0])
            if memory_bytes(config['memory']) >= memory_bytes(previous):
                profiles[key] = {'memory': config['memory'], 'attempt': job['id'], 'at': self.clock()}
        if (issue.get('memory_retry') or {}).get('role') == job['role']:
            issue.pop('memory_retry', None)

    def retry_memory(self, identifier, config, detail, *, held=False):
        """Record one bounded escalation atomically; never charge a code failure."""
        from .capacity import memory_bytes
        with self.transaction() as data:
            job = data['jobs'][identifier]
            issue = data['issues'][job['issue_key']]
            if (issue['state'] in ('merged', 'cancelled') or issue.get('operator_hold')
                    or (job['outcome'] is not None and not (held and job['outcome'] == 'blocked_environment'))):
                return False
            if held:
                if (issue.get('environment_hold') or {}).get('attempt') != identifier or issue['active']:
                    return False
            elif issue['state'] == 'hold' or (job['role'] != 'recovery' and issue['active'] != identifier):
                return False
            used = (job.get('runner_config') or {}).get('memory', config['memory'])
            target = next((v for v in config.get('memory_tiers', []) if memory_bytes(v) > memory_bytes(used)), None)
            if not target:
                return False
            if job['role'] == 'recovery':
                failure = next(f for f in issue['failures'] if f['attempt'] == job['failure']['attempt'])
                failure['investigation'] = 'pending'
            retry_state = (issue.get('environment_hold') or {}).get('state', issue['state'])
            issue.pop('environment_hold', None)
            issue.update(state=retry_state, active=None, retry_after=0,
                         reason=f'Waiting for a {target} worker after memory exhaustion',
                         memory_retry={'role': job['role'], 'memory': target, 'attempt': identifier,
                                       'branch': job['branch'], 'at': self.clock()})
            issue['last_summary'] = {'headline': f'Retrying with {target} of memory',
                                    'plain': 'The worker ran out of memory. Forge will retry with a larger worker when capacity is available. Published work is retained.',
                                    'role': job['role'], 'at': self.clock()}
            job['outcome'] = 'memory_retry'
            self.event(data, issue, 'memory_escalated', attempt=identifier, previous=used, memory=target, detail=detail[:2000])
            return True

    def wait_on_issues(self, identifier, numbers, evidence, body, blocked, summary=None):
        """Release the worker while prerequisites finish, without spending a retry."""
        from .contracts import digest
        with self.transaction() as data:
            job, issue = self._current(data, identifier)
            if job['outcome'] is not None:
                return
            if job['role'] != 'build':
                raise StateError('Only a build can declare prerequisites')
            previous = issue.get('declared_dependencies', []) if issue.get('dependency_body') == digest(body or '') else []
            issue.update(state='ready', active=None, reason=None,
                         declared_dependencies=sorted(set(previous) | set(numbers)),
                         dependency_evidence=evidence, dependency_body=digest(body or ''),
                         blocked_dependencies=blocked,
                         dependency_reason=('Waiting for ' + ', '.join('#' + str(n) for n in blocked) + '.') if blocked else None)
            issue.pop('dependencies_checked_at', None)
            job['outcome'] = 'waiting_on_issues'
            self.remember(issue, summary)
            self.event(data, issue, 'dependencies_declared', attempt=identifier, issues=numbers,
                       reason=evidence)

    def request_decision(self, identifier, decision, summary=None):
        """A question stops dispatch without charging a failure or starting review."""
        with self.transaction() as data:
            job, issue = self._current(data, identifier)
            if job['outcome'] is not None:
                return
            if job['role'] != 'build':
                raise StateError('Only a build can request a decision')
            issue['decision_request'] = {**deepcopy(decision), 'attempt': identifier}
            issue.update(state='hold', active=None, reason=decision['question'])
            job['outcome'] = 'needs_decision'
            self.remember(issue, summary)
            self.event(data, issue, 'decision_requested', attempt=identifier, decision=deepcopy(decision), reason=decision['question'])

    def retry_services(self, identifier, config, required, *, held=False):
        """Adjust services once per issue, within the installed database environment."""
        with self.transaction() as data:
            job = data['jobs'][identifier]
            issue = data['issues'][job['issue_key']]
            choice = issue.get('environment_choice') or {}
            blocked = issue.get('environment_hold') or {}
            eligible_hold = (held and job['outcome'] == 'blocked_environment' and job['cleanup']
                             and blocked.get('attempt') == identifier and issue['state'] == 'hold' and not issue['active'])
            if ((job['outcome'] is not None and not eligible_hold) or issue.get('environment_retry')
                    or 'full' not in config['environment_tiers'] or not required
                    or not set(required) <= {'mongodb', 'postgres', 'redis'}
                    or set(required) <= set((job.get('runner_config') or {}).get('services', []))
                    or choice.get('automatic') is False or issue.get('environment_override')
                    or (issue['state'] in ('hold', 'merged', 'cancelled') and not eligible_hold) or issue.get('operator_hold')):
                return False
            if job['role'] != 'recovery' and issue['active'] != identifier and not eligible_hold:
                raise StateError('Obsolete attempt cannot change its environment')
            if job['role'] == 'recovery':
                failure = next(f for f in issue['failures'] if f['attempt'] == job['failure']['attempt'])
                failure['investigation'] = 'pending'
            issue['environment_retry'] = {'attempt': identifier, 'services': sorted(set(required) | set((job.get('runner_config') or {}).get('services', []))), 'branch': job['branch']}
            if eligible_hold:
                issue['state'] = blocked['state']
                issue.pop('environment_hold')
            issue.update(active=None, retry_after=0, reason='Retrying with the required database services')
            job['outcome'] = 'services_retry'
            self.event(data, issue, 'services_retry', attempt=identifier, services=list(required), branch=job['branch'])
            return True

    def block_environment(self, identifier, blocker, detail, summary=None, required_services=None):
        """Availability is not a code failure; retain the candidate and stage."""
        from .contracts import ENVIRONMENT_BLOCKERS, environment_key
        if blocker not in ENVIRONMENT_BLOCKERS:
            raise ValueError('Unknown environment blocker')
        with self.transaction() as data:
            job = data['jobs'][identifier]
            issue = data['issues'][job['issue_key']]
            if job['outcome'] is not None:
                return
            if issue['state'] in ('merged', 'cancelled') or issue.get('operator_hold'):
                job['outcome'] = 'superseded'
                return
            if job['role'] != 'recovery' and issue['active'] != identifier:
                raise StateError('Obsolete attempt cannot block the environment')
            if job['role'] == 'recovery':
                failure = next(f for f in issue['failures'] if f['attempt'] == job['failure']['attempt'])
                failure['investigation'] = 'pending'
            issue['environment_hold'] = {'state': issue['state'], 'blocker': blocker,
                                         'detail': detail[:8000], 'attempt': identifier,
                                         'environment': environment_key(job.get('runner_config') or {})}
            if required_services:
                issue['environment_hold']['required_services'] = list(required_services)
            self.remember(issue, summary, issue['environment_hold'])
            issue.update(state='hold', active=None, reason='Forge environment needs attention (' + blocker.replace('_', ' ') + ')')
            job['outcome'] = 'blocked_environment'
            self.event(data, issue, 'environment_blocked', attempt=identifier, blocker=blocker, detail=detail[:8000])

    def resume_environment(self, key, reason):
        with self.transaction() as data:
            issue = data['issues'][key]
            held = issue.get('environment_hold')
            if not held or issue['state'] != 'hold' or issue['active']:
                return False
            if any(j['issue_key'] == key and not j['cleanup'] for j in data['jobs'].values()):
                return False
            issue.update(state=held['state'], reason=None, retry_after=0)
            issue.pop('environment_hold')
            self.event(data, issue, 'environment_recheck', reason=reason)
            return True

    def wait(self, identifier, reason, until):
        with self.transaction() as data:
            job = data['jobs'][identifier]
            issue = data['issues'][job['issue_key']]
            pooled = bool(job.get('provider_id'))
            if job['role'] != 'recovery':
                job, issue = self._current(data, identifier)
            if job['outcome'] is not None:
                return
            job['outcome'] = 'waiting'
            if job['role'] == 'recovery':
                failure = next(f for f in issue['failures'] if f['attempt'] == job['failure']['attempt'])
                if failure['investigation'] == identifier:
                    failure['investigation'] = 'pending'
            else:
                issue.update(active=None, reason=reason, retry_after=self.clock() if pooled and reason in ('blocked_auth', 'blocked_quota') else until)
            if reason in ('blocked_auth', 'blocked_quota'):
                if pooled:
                    since = cooldown_since(data, job['provider'], reason, self.clock())
                    cooldowns = data.setdefault('provider_cooldowns', {})
                    cooldowns[job['provider']] = {'until': max(cooldowns.get(job['provider'], {}).get('until', 0), until),
                                                 'reason': reason, 'attempt': identifier, 'since': since}
                else:
                    data['provider_retry_after'] = max(data.get('provider_retry_after', 0), until)
            self.event(data, issue, 'availability_wait', attempt=identifier, reason=reason,
                       provider=job.get('provider'), model=job.get('model'), retry_after=until)

    def invalidate(self, key, reason, *, candidate=None, hold=False):
        with self.transaction() as data:
            issue = data['issues'][key]
            if issue['active']:
                job = data['jobs'][issue['active']]
                job.update(outcome='superseded', fenced=True, phase='stopping')
                issue['active'] = None
            if candidate:
                candidate = {**candidate, 'accepted_at': self.clock()}
            issue.update(state='hold' if hold else 'built' if candidate else 'ready', candidate=candidate or issue['candidate'],
                         evidence={}, reason=reason)
            self.event(data, issue, 'candidate_invalidated', reason=reason)
            if hold:
                self.event(data, issue, 'held', reason=reason)

    def hold(self, key, reason, *, fence_active=False):
        with self.transaction() as data:
            issue = data['issues'][key]
            if issue['state'] in ('merged', 'cancelled', 'failed'):
                return
            if issue['state'] == 'hold' and issue.get('reason') == reason:
                return
            if fence_active and issue['active']:
                job = data['jobs'][issue['active']]
                job.update(outcome='held', cancelled=True)
                if not job['cleanup']:
                    job.update(fenced=True, phase='stopping')
                issue['active'] = None
            issue.pop('prerequisite_hold', None)
            issue.pop('environment_hold', None)
            issue.update(state='hold', reason=reason)
            self.event(data, issue, 'held', reason=reason)

    def pause_for_protection(self, key):
        with self.transaction() as data:
            issue = data['issues'][key]
            if issue['active'] or issue['state'] not in ('ready', 'built', 'reviewed'):
                return
            issue['prerequisite_hold'] = {'state': issue['state']}
            issue.update(state='hold', reason='Repository review settings need attention')
            self.event(data, issue, 'protection_paused')

    def migrate_protection_hold(self, key):
        """Recognize only the old pre-first-build setup gate, never a human hold."""
        with self.transaction() as data:
            return self.migrate_protection_hold_in(data, key)

    def migrate_protection_hold_in(self, data, key):
        issue = data['issues'][key]
        reason = 'Configure two approving reviews and stale-approval dismissal before dispatch'
        events = [e for e in data['events'] if e['issue'] == key]
        if (issue['state'] != 'hold' or issue.get('reason') != reason
                or issue['active'] or issue['candidate'] or issue['failures']
                or any(j['issue_key'] == key for j in data['jobs'].values())
                or [e['kind'] for e in events] != ['discovered', 'held']
                or events[0].get('reason') is not None or events[1].get('reason') != reason):
            return False
        issue.update(prerequisite_hold={'state': 'ready', 'legacy': True},
                     reason='Repository review settings need attention')
        self.event(data, issue, 'protection_hold_migrated')
        return True

    def resume_protection(self, key):
        with self.transaction() as data:
            issue = data['issues'][key]
            held = issue.get('prerequisite_hold')
            if (issue['state'] != 'hold' or issue['active'] or not held
                    or issue.get('reason') != 'Repository review settings need attention'):
                return
            issue.update(state=held['state'], reason=None)
            issue.pop('prerequisite_hold', None)
            self.event(data, issue, 'protection_restored')

    def cancel(self, key, reason, *, closed_as=None):
        with self.transaction() as data:
            issue = data['issues'][key]
            if closed_as and issue['state'] != 'merged':
                issue['closed_as'] = closed_as  # GitHub's state_reason: completed, not_planned, duplicate
            if issue['state'] in ('cancelled', 'merged'):
                return
            if issue['active']:
                data['jobs'][issue['active']].update(cancelled=True, outcome='cancelled', fenced=True, phase='stopping')
            issue.update(state='cancelled', active=None, reason=reason)
            self.event(data, issue, 'cancelled', reason=reason)

    def deployed(self, key, verdict):
        """Evidence Forge read itself; a merge with none stays waiting, never failed."""
        with self.transaction() as data:
            issue = data['issues'][key]
            if issue['state'] != 'merged' or issue.get('deployed'):
                return False
            issue['deployed'] = {'commit': verdict['commit'], 'at': self.clock(), 'via': verdict['via']}
            issue['deploy_failed'] = None
            self.event(data, issue, 'deployed', commit=verdict['commit'], via=verdict['via'])
            return True

    def deploy_failed(self, key, info):
        """Recorded until a later deployment carrying the merge succeeds."""
        with self.transaction() as data:
            issue = data['issues'][key]
            if issue['state'] != 'merged' or issue.get('deployed'):
                return False
            if (issue.get('deploy_failed') or {}).get('sha') == info['sha']:
                return False
            issue['deploy_failed'] = {'sha': info['sha'], 'at': self.clock(), 'run_url': info.get('run_url')}
            self.event(data, issue, 'deploy_failed', sha=info['sha'], run_url=info.get('run_url'))
            return True

    def merge_commit(self, key, sha):
        """Cache the merge commit on the candidate: it is read from GitHub once."""
        with self.transaction() as data:
            candidate = data['issues'][key]['candidate']
            if candidate is not None:
                candidate['merge_commit'] = sha

    def retry(self, key, reason, *, request_id=None):
        if not reason.strip():
            raise ValueError('Human retry must record a decision')
        with self.transaction() as data:
            issue = data['issues'][key]
            if issue['state'] not in {'failed', 'hold', 'cancelled'} or issue['active']:
                raise StateError('Issue does not need a human retry decision')
            if any(j['issue_key'] == key and not j['cleanup'] for j in data['jobs'].values()):
                raise StateError('Reset old executions before retrying')
            issue.update(state='ready', budget_start=len(issue['failures']), retry_plan=reason, reason=None, evidence={})
            decision = issue.pop('decision_request', None)
            if decision:
                self.event(data, issue, 'decision_answered', decision=decision, reason=reason, request=request_id)
            self.event(data, issue, 'human_retry', reason=reason, request=request_id)
