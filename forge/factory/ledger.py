"""Supervisor-only issue ledger and durable launch intents, independent of terminals."""
from contextlib import contextmanager
from copy import deepcopy
from .providers import available as provider_available
import time
import json
import uuid

from ..state import AtomicStore, StateError
from .contracts import MAX_FAILURES, NEXT_ROLE, PASS_STATE, RETRY_STATE, ROLES, STATES


class Ledger:
    def __init__(self, root, clock=time.time):
        self.store = AtomicStore(root, 'factory-ledger')
        self.clock = clock

    @contextmanager
    def transaction(self):
        with self.store.transaction() as data:
            if data or self.store.marker.exists():
                self.validate(data)
            data.setdefault('version', 1)
            for key in ('issues', 'jobs', 'slots'):
                data.setdefault(key, {})
            data.setdefault('events', [])
            yield data
            self.validate(data)

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
        with self.transaction() as data:
            return deepcopy(data)

    def event(self, data, issue, kind, **fields):
        data['events'].append({'sequence': len(data['events']) + 1, 'at': self.clock(),
                               'issue': issue['key'], 'kind': kind, **fields})

    def discover(self, repo, number, requirements, *, hold=None, title=None):
        key = f"{repo['id']}:{number}"
        with self.transaction() as data:
            if key not in data['issues']:
                issue = {'key': key, 'repo_id': repo['id'], 'repo': repo['name'], 'number': number,
                         'title': title or f'Issue #{number}', 'state': 'hold' if hold else 'ready', 'reason': hold, 'requirements': requirements,
                         'candidate': None, 'active': None, 'failures': [], 'budget_start': 0,
                         'retry_plan': None, 'evidence': {}, 'created_at': self.clock()}
                data['issues'][key] = issue
                self.event(data, issue, 'discovered', reason=hold)
        return key

    def reserve(self, key, role, slot, *, publisher, policy, timeout, failure=None, provider='default', runner_config=None, target_branch='main'):
        """One atomic ownership transfer before any external process launch."""
        with self.transaction() as data:
            issue = data['issues'][key]
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
                                     and provider_available(pool[(start + offset) % len(pool)], data, self.clock()))), None)
                if selected is None:
                    raise StateError('All coding providers are temporarily unavailable')
                entry = pool[selected]
                data['provider_cursor'] = (selected + 1) % len(pool)
                runner_config.update({k: v for k, v in entry.items() if k != 'id'})
                provider, provider_id = entry['provider'], entry['id']
            identifier = str(uuid.uuid4())
            branch = (issue['candidate'].get('branch') if issue['candidate'] else None) or f"forge/issue-{issue['number']}-{identifier[:8]}"
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

    def accept(self, identifier, *, candidate=None, evidence=None):
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
            job['outcome'] = 'accepted'
            self.event(data, issue, 'accepted', attempt=identifier, role=job['role'], candidate=issue['candidate'])
            return True

    @staticmethod
    def _current(data, identifier):
        job = data['jobs'][identifier]
        issue = data['issues'][job['issue_key']]
        if issue['active'] != identifier and job['outcome'] is None:
            raise StateError('Obsolete attempt cannot change the issue')
        return job, issue

    def fail(self, identifier, cause, *, rejection=False, evidence=None):
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
            issue['active'] = None
            issue['state'] = 'failed' if self.failure_count(issue) >= MAX_FAILURES else 'investigating'
            issue['reason'] = cause
            if rejection:
                issue['evidence'] = {}
            self.event(data, issue, 'failed_attempt', attempt=identifier, cause=cause, count=self.failure_count(issue))
            return True

    @staticmethod
    def failure_count(issue):
        return sum(f['charged'] for f in issue['failures'][issue['budget_start']:])

    def findings(self, identifier, findings):
        with self.transaction() as data:
            job = data['jobs'][identifier]
            if job['role'] != 'recovery' or job['outcome'] is not None:
                return
            issue = data['issues'][job['issue_key']]
            failure = next(f for f in issue['failures'] if f['attempt'] == job['failure']['attempt'])
            failure.update(investigation='complete', findings=findings)
            job['outcome'] = 'investigated'
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
                issue.update(state='hold', reason='Repeated failure requires a supported change of approach')
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
                    cooldowns = data.setdefault('provider_cooldowns', {})
                    cooldowns[job['provider']] = {'until': max(cooldowns.get(job['provider'], {}).get('until', 0), until),
                                                 'reason': reason, 'attempt': identifier}
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
            issue.update(state='hold', reason=reason)
            self.event(data, issue, 'held', reason=reason)

    def cancel(self, key, reason):
        with self.transaction() as data:
            issue = data['issues'][key]
            if issue['state'] in ('cancelled', 'merged'):
                return
            if issue['active']:
                data['jobs'][issue['active']].update(cancelled=True, outcome='cancelled', fenced=True, phase='stopping')
            issue.update(state='cancelled', active=None, reason=reason)
            self.event(data, issue, 'cancelled', reason=reason)

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
            self.event(data, issue, 'human_retry', reason=reason, request=request_id)
