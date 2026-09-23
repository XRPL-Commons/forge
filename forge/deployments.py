"""Deployment profiles proposed by a read-only strategist, never applied by it.

The store is controller-owned operational state, like the ledger: one entry per
repository, one strategist execution at a time, and no effect on settings,
issues or any domain until an operator confirms the proposal.
"""
from copy import deepcopy
import json
from pathlib import Path
import re
import time
import uuid

from .state import AtomicStore
from .factory.github import GitHubUnavailable
from .factory.runner import RunnerCapacity, RunnerError

STATUSES = ('idle', 'running', 'proposed', 'confirmed', 'rejected', 'failed')
STRATEGIES = ('github_deployments', 'health', 'header', 'unobservable')
OUTCOMES = ('pass', 'reject', 'unable', 'wait', 'blocked_environment')
PROFILE = ('strategy', 'deploy', 'domain', 'exposure', 'commit_source', 'proposal')
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
        expected.add('blocker')  # Older image contracts remain readable.
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

    def __init__(self, root, *, runner, github, settings, clock=time.time):
        self.root = Path(root)
        self.store = AtomicStore(self.root / 'state', 'deployments')
        self.runner, self.github, self.settings, self.clock = runner, github, settings, clock

    @staticmethod
    def blank(name):
        return {'name': name, 'status': 'idle', 'workflows_digest': None, 'head': None, 'job': None,
                'proposal': None, 'summary': None, 'reason': None, 'error': None,
                'observed_at': None, 'checked_at': 0, 'reread_requested': False, 'observation': None}

    def read(self):
        with self.store.transaction() as data:
            data['version'] = 1
            data.setdefault('repositories', {})
            return deepcopy(data)

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
        for row in self.settings.view()['repositories']:
            if not row['dispatch_allowed']:
                continue
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
                    # Read-only worker credential; the note is the strategist assignment.
                    runner.prepare(job, self.github.token('controller', job['repo_id'], worker=True))
                    runner.start(job)
                    if job.get('capacity_wait_since') is not None:
                        job['deadline'] += max(0, self.clock() - job.pop('capacity_wait_since'))
                    self.save(key, job=job)
                    return
            except RunnerCapacity:
                job.setdefault('capacity_wait_since', self.clock())
                self.save(key, job=job)
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

    def finish(self, key, job):
        capture, report, reason = job['capture'] or {}, None, None
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
