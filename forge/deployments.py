"""Deployment profiles proposed by a read-only strategist, never applied by it.

The store is controller-owned operational state, like the ledger: one entry per
repository, one strategist execution at a time, and no effect on settings,
issues or any domain until an operator confirms the proposal.
"""
from copy import deepcopy
import json
import math
from pathlib import Path
import re
import time
import uuid

from .app_access import OPTIONAL_PERMISSIONS
from .state import AtomicStore, StateError
from .factory.contracts import SHA
from .factory.github import GitHubUnavailable
from .factory.live import LiveUnavailable
from .factory.runner import RunnerCapacity, RunnerError

STATUSES = ('idle', 'running', 'proposed', 'confirmed', 'rejected', 'failed')
STRATEGIES = ('github_deployments', 'health', 'header', 'unobservable')
OUTCOMES = ('pass', 'reject', 'unable', 'wait', 'blocked_environment')
PROFILE = ('strategy', 'deploy', 'domain', 'exposure', 'commit_source', 'proposal')
CAPACITY = 'Worker capacity was not available in time; re-read deployment to retry'
PERMISSION = 'GitHub deployments need the recommended permission'
UNREADABLE = 'The deployment domain could not be read'
# A domain is later fetched over the network: accept a bare host, never a URL.
HOST = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                  r'(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)+')


def host(value):
    if value is None or value == '':
        return None
    if not isinstance(value, str) or len(value) > 253 or not HOST.fullmatch(value):
        raise ValueError('A deployment domain is a bare host name, without scheme, port or path')
    return value


def profile(value, job):
    """Strategist results get the same distrust as any other worker result."""
    if not isinstance(value, dict) or len(json.dumps(value)) > 65536:
        raise ValueError('The deployment read did not return a valid result')
    expected = {'version', 'attempt', 'role', 'outcome', 'summary', *PROFILE}
    if 'blocker' in value:
        expected.add('blocker')  # Optional: older image contracts omit the field.
    if (set(value) != expected or type(value['version']) is not int or value['version'] != 1
            or value['attempt'] != job['id'] or value['role'] != 'strategist'
            or value['outcome'] not in OUTCOMES
            or not isinstance(value['summary'], str) or not value['summary'].strip()):
        raise ValueError('The deployment read does not satisfy its assignment')
    if value['outcome'] != 'pass':
        return value
    def texts(item, keys):
        return (isinstance(item, dict) and set(item) == set(keys)
                and all(item[key] is None or isinstance(item[key], str) for key in keys))
    if value['strategy'] not in STRATEGIES or not texts(value['deploy'], ('workflow', 'environment', 'trigger')):
        raise ValueError('The deployment read must name one strategy and its deploy workflow')
    domain, exposure = value['domain'], value['exposure']
    if (not texts(domain, ('value', 'source', 'confidence')) or domain['source'] not in ('workflow', 'docs', 'config', 'none')
            or domain['confidence'] not in ('high', 'low', 'none')):
        raise ValueError('The deployment read must state where the domain came from')
    host(domain['value'])
    if (not texts(exposure, ('kind', 'path', 'field', 'header')) or exposure['kind'] not in ('health', 'header', 'none')
            or (exposure['kind'] == 'health' and not str(exposure['path'] or '').startswith('/'))
            or (exposure['kind'] == 'header' and not exposure['header'])):
        raise ValueError('The deployment read must say where the running commit is exposed')
    if not isinstance(value['commit_source'], str):
        raise ValueError('The deployment read must say where the build learns its commit')
    if value['proposal'] is not None and not (texts(value['proposal'], ('title', 'body'))
            and all(str(value['proposal'][key] or '').strip() for key in ('title', 'body'))):
        raise ValueError('A proposal needs a title and a body')
    return value


class Deployments:
    """One strategist execution at a time, driven from the supervisor cycle."""

    TIMEOUT = 900
    DIGEST_INTERVAL = 1800
    BACKOFF = 600
    FAILURE_LIMIT = 3

    def __init__(self, root, *, runner, github, settings, clock=time.time):
        self.root = Path(root)
        self.store = AtomicStore(self.root / 'state', 'deployments')
        self.runner, self.github, self.settings, self.clock = runner, github, settings, clock

    @staticmethod
    def blank(name):
        return {'name': name, 'status': 'idle', 'workflows_digest': None, 'head': None, 'job': None,
                'proposal': None, 'summary': None, 'reason': None, 'error': None,
                'observed_at': None, 'checked_at': 0, 'reread_requested': False, 'observation': None,
                'exposure_issue': None, 'observed': None}

    def read(self):
        """The supervisor reads this store every cycle; a read never writes it."""
        try:
            data = json.loads(self.store.path.read_text()) if self.store.path.exists() else {}
        except (OSError, ValueError) as error:
            raise StateError('unreadable deployment state; reconcile before recovery') from error
        if not isinstance(data, dict) or (not self.store.path.exists() and self.store.marker.exists()):
            raise StateError('invalid or missing initialized deployment state')
        data.setdefault('version', 1)
        data.setdefault('repositories', {})
        return data

    def entry(self, data, repo_id, name=None):
        entry = data.setdefault('repositories', {}).setdefault(str(repo_id), self.blank(name))
        if name:
            entry['name'] = name
        return entry

    def save(self, repo_id, name=None, **values):
        with self.store.transaction() as data:
            data['version'] = 1
            self.entry(data, repo_id, name).update(values)

    def fail(self, message):
        """A cycle-level failure belongs in the store, not in the supervisor's path."""
        with self.store.transaction() as data:
            data['version'] = 1
            data.setdefault('repositories', {})
            data['error'] = str(message)[:500]

    def request_reread(self, repo_id, name=None):
        self.save(repo_id, name, reread_requested=True)

    def confirm(self, repo_id, domain):
        """Return the profile for the operator's settings; the store only records the decision."""
        with self.store.transaction() as data:
            entry = data.get('repositories', {}).get(str(repo_id))
            if not entry or entry['status'] != 'proposed' or not entry['proposal']:
                raise ValueError('There is no deployment proposal to confirm')
            proposed = entry['proposal']
            value = {'strategy': proposed['strategy'], 'deploy': deepcopy(proposed['deploy']),
                     'domain': host(domain) if domain else host(proposed['domain']['value']),
                     'exposure': deepcopy(proposed['exposure']), 'workflows_digest': entry['workflows_digest'],
                     'confirmed_at': self.clock(), 'exposure_issue': None}
            entry.update(status='confirmed', observed_at=self.clock(), reason=None, error=None)
            return value

    def reject(self, repo_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('A rejected proposal needs a reason')
        with self.store.transaction() as data:
            entry = data.get('repositories', {}).get(str(repo_id))
            if not entry or entry['status'] not in ('proposed', 'failed'):
                raise ValueError('There is no deployment proposal to reject')
            entry.update(status='rejected', reason=reason.strip()[:2000], observed_at=self.clock())

    def leased(self, config):
        """A rotating OAuth credential has one writer; pipeline work holds it first."""
        reference = config.get('provider_auth_file_env')
        root = self.root / 'state/factory'
        if not reference or not (root / 'factory-ledger.json').exists():
            return False
        from .factory.ledger import Ledger
        return any(not job['cleanup'] and (job.get('runner_config') or {}).get('provider_auth_file_env') == reference
                   for job in Ledger(root).read()['jobs'].values())

    def new_job(self, config, repo, head, deadline):
        """A probe-shaped internal job: no issue, no candidate, no publication."""
        return {'id': str(uuid.uuid4()), 'slot': 'deployment-strategist', 'role': 'strategist',
                'repo': repo['name'], 'repo_id': repo['id'], 'issue_number': None,
                'publisher': 'forge-strategist', 'target_branch': None, 'branch': None,
                'candidate': None, 'history': {}, 'head': head, 'deadline': deadline,
                'fenced': False, 'runner_config': deepcopy(config), 'kind': 'strategist',
                'cleanup': False, 'capture': None}

    def live(self, data):
        for key, entry in data['repositories'].items():
            job = entry.get('job')
            if job and not job['cleanup']:
                return key
        return None

    def tick(self, config):
        data = self.read()
        key = self.live(data)
        if key is not None:
            self.drive(key)
            data = self.read()
            if self.live(data) is not None:
                return
        self.start(config, data)

    def start(self, config, data):
        if self.leased(config):
            return  # The coding account is held by a pipeline worker; retry next cycle.
        for row in self.settings.view()['repositories']:
            if not row['dispatch_allowed']:
                continue
            if time.monotonic() >= getattr(self.github, 'deadline', float('inf')):
                return  # The cycle's observation budget is spent; resume next cycle.
            entry = data['repositories'].get(str(row['id']))
            reread = bool(entry and entry['reread_requested'])
            if entry and not reread and self.clock() - entry['checked_at'] < self.DIGEST_INTERVAL:
                continue
            try:
                value, head = self.github.workflows_digest({'repo': row['name'], 'repo_id': row['id']})
            except (GitHubUnavailable, ValueError, TypeError, KeyError) as error:
                self.save(row['id'], row['name'], checked_at=self.clock(),
                          error=str(error) if isinstance(error, GitHubUnavailable) else
                                'The repository workflows could not be read')
                continue
            self.save(row['id'], row['name'], checked_at=self.clock(), error=None)
            if not reread and entry and entry['workflows_digest'] == value:
                continue
            job = self.new_job(config, row, head, self.clock() + self.TIMEOUT)
            job['workflows_digest'] = value
            self.save(row['id'], row['name'], status='running', job=job, reread_requested=False,
                      summary=None, reason=None, observation=None)
            self.drive(str(row['id']))
            return  # One strategist at a time; the next repository waits a cycle.

    def observe(self, github, ledger, reader):
        """One bounded read per confirmed profile, then a verdict per merged issue."""
        errors, view, merged = [], self.settings.view(), None
        # Whether the optional grant is missing was decided by the access sync; a
        # refused token is otherwise an outage, not a permission to ask for.
        ungranted = any(item.split(':')[0].strip() == 'deployments' for item in
                        ((view.get('roles') or {}).get('controller') or {}).get('missing_recommended') or [])
        for row in view['repositories']:
            profile = (row.get('deployment') or {}).get('profile')
            if not profile or not row['enabled']:
                continue
            if time.monotonic() >= getattr(github, 'deadline', float('inf')):
                break  # The cycle's observation budget is spent; resume next cycle.
            if merged is None:
                merged = {}
                for issue in ledger.read()['issues'].values():
                    if issue['state'] == 'merged' and not issue.get('deployed'):
                        merged.setdefault(issue['repo_id'], []).append(issue)
            entry = self.read()['repositories'].get(str(row['id'])) or {}
            observed = self.read_live(github, reader, row, profile, entry.get('observed') or {}, ungranted)
            self.save(row['id'], row['name'], observed=observed)
            if observed['error']:
                errors.append({'repo': row['name'], 'reason': observed['error']})
            errors.extend(self.verdicts(github, ledger, row, observed, merged.get(row['id']) or []))
        return errors

    def read_live(self, github, reader, row, profile, previous, ungranted):
        """Two reads at most: the deployments API, then the product's own domain."""
        item = {'repo': row['name'], 'repo_id': row['id']}
        observed = {'live': previous.get('live'), 'deploy': previous.get('deploy'), 'error': None,
                    'failures': previous.get('failures', 0), 'retry_after': previous.get('retry_after', 0),
                    'live_error': previous.get('live_error')}
        reasons = []
        environment = (profile.get('deploy') or {}).get('environment')
        if environment:
            try:
                github.token('controller', row['id'], extra=OPTIONAL_PERMISSIONS['controller'])
            except GitHubUnavailable as error:
                reasons.append(PERMISSION if ungranted else str(error))
            else:
                try:
                    observed['deploy'] = self.deployment(github, item, environment)
                except (GitHubUnavailable, ValueError, KeyError, TypeError) as error:
                    reasons.append(str(error))
        exposure = profile.get('exposure') or {}
        if profile.get('domain') and exposure.get('kind') in ('health', 'header'):
            paused = observed['retry_after'] - self.clock()
            if paused > 0:
                # A paused domain is still a failing domain; the card must keep saying so.
                reasons.append('%s (retrying in %d min)' % (observed['live_error'] or UNREADABLE,
                                                            math.ceil(paused / 60)))
            else:
                url = 'https://' + profile['domain'] + (exposure.get('path') or '/')
                try:
                    commit = reader(url, kind=exposure['kind'], field=exposure.get('field'),
                                    header=exposure.get('header'))
                    observed.update(failures=0, retry_after=0, live_error=None)
                    if commit:
                        observed['live'] = {'commit': commit, 'at': self.clock()}
                except LiveUnavailable as error:
                    observed['failures'] += 1
                    if observed['failures'] >= self.FAILURE_LIMIT:
                        observed['retry_after'] = self.clock() + self.BACKOFF
                    observed['live_error'] = str(error)
                    reasons.append(str(error))
        observed['error'] = '; '.join(reasons)[:500] or None
        return observed

    @staticmethod
    def deployment(github, item, environment):
        rows = github.deployments(item, environment)
        if not rows:
            return None
        newest = rows[0]
        latest = next(iter(github.deployment_statuses(item, newest['id'])), None)
        return {'sha': newest['sha'], 'state': latest['state'] if latest else 'pending',
                'at': (latest or newest).get('created_at'),
                'run_url': (latest or {}).get('log_url') or (latest or {}).get('target_url')}

    def verdicts(self, github, ledger, row, observed, issues):
        """A merge is deployed only when Forge can see its commit in what is running."""
        errors = []
        live = (observed.get('live') or {}).get('commit')
        deploy = observed.get('deploy') or {}
        for issue in issues:
            if time.monotonic() >= getattr(github, 'deadline', float('inf')):
                break
            try:
                merge = self.merge_commit(github, ledger, issue)
                if not merge:
                    continue
                if live and (merge.startswith(live) or github.contains(issue, merge, live)):
                    ledger.deployed(issue['key'], {'commit': live, 'via': 'live'})
                elif deploy.get('state') not in ('success', 'failure') or not deploy.get('sha'):
                    continue  # No deployment carries a verdict for this merge yet.
                elif not github.contains(issue, merge, deploy['sha']):
                    continue  # The deployment predates this merge; it says nothing about it.
                elif deploy['state'] == 'success':
                    ledger.deployed(issue['key'], {'commit': deploy['sha'], 'via': 'github'})
                else:
                    ledger.deploy_failed(issue['key'], {'sha': deploy['sha'], 'run_url': deploy.get('run_url')})
            except (GitHubUnavailable, StateError, ValueError, KeyError, TypeError) as error:
                errors.append({'repo': row['name'], 'reason': str(error)})
        return errors

    @staticmethod
    def merge_commit(github, ledger, issue):
        candidate = issue['candidate'] or {}
        if candidate.get('merge_commit'):
            return candidate['merge_commit']
        if not candidate.get('pr'):
            return None
        value = github.pr(issue, candidate['pr']).get('merge_commit_sha')
        if not isinstance(value, str) or not SHA.fullmatch(value):
            return None
        ledger.merge_commit(issue['key'], value)
        return value

    def drive(self, key):
        job = self.read()['repositories'][key]['job']
        runner = self.runner(job['runner_config'])
        if not job['fenced']:
            try:
                current = runner.inspect(job)
                if current is None:
                    job['container_id'] = runner.create(job)
                    self.save(key, job=job)
                    current = runner.inspect(job)
                if current is None:
                    raise RunnerError('The strategist container is not observable; reconcile before replacement')
                if current['status'] == 'created':
                    # The recovery role is the controller App's read-only worker credential.
                    runner.prepare(job, self.github.token('recovery', job['repo_id'], worker=True))
                    runner.start(job)
                    if job.get('capacity_wait_since') is not None:
                        job['deadline'] += max(0, self.clock() - job.pop('capacity_wait_since'))
                    self.save(key, job=job)
                    return
            except RunnerCapacity:
                if self.clock() < job['deadline']:
                    job.setdefault('capacity_wait_since', self.clock())
                    self.save(key, job=job)
                    return
                # Waiting for memory is not unbounded: give the slot back and report.
                job.update(capture=runner.capture(job), fenced=True)
                self.save(key, job=job)
                runner.reset(job)
                job['cleanup'] = True
                self.finish(key, job, reason=CAPACITY)
                return
            timed_out = self.clock() >= job['deadline']
            if current['running'] and not timed_out:
                self.save(key, observation=runner.observe(job, current))
                return
            capture = runner.capture(job)
            capture['timed_out'] = timed_out and current['running']
            job.update(capture=capture, fenced=True)
            self.save(key, job=job)
        runner.reset(job)
        job['cleanup'] = True
        self.finish(key, job)

    def finish(self, key, job, reason=None):
        capture, report = job['capture'] or {}, None
        if reason is None:
            if (capture.get('execution') or {}).get('oom_killed'):
                reason = 'The deployment read exceeded its memory limit; review worker capacity before re-reading'
            elif capture.get('timed_out'):
                reason = 'The deployment read did not finish before its deadline'
            else:
                try:
                    report = profile(capture.get('result'), job)
                except ValueError as error:
                    reason = str(error)
        values = {'job': job, 'observation': None, 'error': None, 'observed_at': self.clock(),
                  'workflows_digest': job['workflows_digest'], 'head': job['head']}
        if report and report['outcome'] == 'pass':
            values.update(status='proposed', summary=report['summary'],
                          proposal={field: report[field] for field in PROFILE})
        else:
            values.update(status='failed', proposal=None,
                          summary=report['summary'] if report else reason)
        self.save(key, **values)
