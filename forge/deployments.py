"""Deployment profiles proposed by a read-only strategist, never applied by it.

The selected store holds one profile per repository and one strategist
execution at a time, with no effect on settings, issues or any domain until an
operator confirms the proposal.
"""
from copy import deepcopy
import hashlib
import json
import logging
import math
from pathlib import Path
import re
import time
import uuid

from .app_access import OPTIONAL_PERMISSIONS
from .store.auxiliary import save_in, job_in
from .store.operations import deployment_views
from .store.runtime import required, writable
from .factory.github import GitHubUnavailable
from .factory.live import LiveUnavailable
from .factory.runner import RunnerCapacity, RunnerError, scrub

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
    # Optional only because an older image's contract omits them; a current
    # worker writes all three, as contracts.result() also allows.
    expected |= {key for key in ('blocker', 'headline', 'plain') if key in value}
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
            or (exposure['path'] is not None and not exposure['path'].startswith('/'))
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
        self.database = required(root)
        self.runner, self.github, self.settings, self.clock = runner, github, settings, clock

    def read(self):
        profiles = deployment_views(self.database)
        with self.database.read_snapshot() as db:
            for row in db.execute('SELECT repository_id,execution_id FROM deployment_profiles WHERE execution_id IS NOT NULL'):
                execution = db.execute('SELECT cleanup FROM auxiliary_executions WHERE id=?', (row['execution_id'],)).fetchone()
                profiles[str(row['repository_id'])]['job'] = job_in(db, row['execution_id']) if execution and not execution[0] else None
            error = db.execute("SELECT details_json FROM runtime_controls WHERE name='deployment_error'").fetchone()
        return {'version': 1, 'repositories': profiles, **(json.loads(error[0]) if error else {})}

    def save(self, repo_id, name=None, **values):
        mapping = dict(status='status', workflows_digest='workflows_digest', head='head_oid', proposal='proposal_json', summary='summary',
                       reason='reason', error='error', observed_at='observed_at', checked_at='checked_at',
                       reread_requested='reread_requested', observation='observation_json', observed='observed_json', exposure_issue='exposure_issue')
        if set(values) - (set(mapping) | {'job'}):
            raise ValueError('Unknown deployment observation field')
        with self.database.write() as db:
            writable(db)
            if name is not None:
                db.execute('INSERT INTO repositories(id,full_name,enabled) VALUES(?,?,0) ON CONFLICT(id) DO NOTHING', (int(repo_id), name))
            db.execute("INSERT INTO deployment_profiles(repository_id,status,checked_at,reread_requested,source_fields_json) "
                       "VALUES(?,'idle',0,0,'[]') ON CONFLICT(repository_id) DO NOTHING", (int(repo_id),))
            fields = {}
            for key, value in values.items():
                if key == 'job':
                    if value is not None:
                        save_in(db, 'deployment', str(repo_id), value, self.clock())
                    fields['execution_id'] = value['id'] if value else None
                else:
                    fields[mapping[key]] = json.dumps(value, allow_nan=False) if mapping[key].endswith('_json') else int(value) if key == 'reread_requested' else value
            if fields:
                db.execute('UPDATE deployment_profiles SET ' + ','.join(key + '=?' for key in fields) + ' WHERE repository_id=?',
                           (*fields.values(), int(repo_id)))

    def fail(self, message):
        with self.database.write() as db:
            writable(db)
            db.execute("INSERT INTO runtime_controls(name,updated_at,details_json) VALUES('deployment_error',?,?) "
                       'ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at,details_json=excluded.details_json',
                       (self.clock(), json.dumps({'error': str(message)[:500]})))

    def request_reread(self, repo_id, name=None):
        self.save(repo_id, name, reread_requested=True)

    def confirm(self, repo_id, domain):
        with self.database.write() as db:
            writable(db)
            row = db.execute('SELECT * FROM deployment_profiles WHERE repository_id=?', (int(repo_id),)).fetchone()
            if row is None or row['status'] != 'proposed' or not row['proposal_json']:
                raise ValueError('There is no deployment proposal to confirm')
            proposed = json.loads(row['proposal_json'])
            value = dict(strategy=proposed['strategy'], deploy=deepcopy(proposed['deploy']),
                         domain=host(proposed['domain']['value']) if domain is None else host(domain),
                         exposure=deepcopy(proposed['exposure']), workflows_digest=row['workflows_digest'],
                         confirmed_at=self.clock(), exposure_issue=row['exposure_issue'])
            db.execute("UPDATE deployment_profiles SET status='confirmed',observed_at=?,reason=NULL,error=NULL WHERE repository_id=?", (self.clock(), int(repo_id)))
            return value

    def reject(self, repo_id, reason):
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('A rejected proposal needs a reason')
        with self.database.write() as db:
            writable(db)
            row = db.execute('SELECT status FROM deployment_profiles WHERE repository_id=?', (int(repo_id),)).fetchone()
            if row is None or row[0] not in ('proposed', 'failed'):
                raise ValueError('There is no deployment proposal to reject')
            db.execute("UPDATE deployment_profiles SET status='rejected',reason=?,observed_at=? WHERE repository_id=?", (reason.strip()[:2000], self.clock(), int(repo_id)))

    def leased(self, config):
        reference = config.get('provider_auth_file_env')
        if not reference:
            return False
        with self.database.read_snapshot() as db:
            return bool(db.execute("SELECT 1 FROM job_leases l JOIN job_launches j ON j.job_id=l.job_id "
                "WHERE json_extract(j.runner_config_json,'$.provider_auth_file_env')=? UNION ALL "
                "SELECT 1 FROM auxiliary_leases l JOIN auxiliary_runtime r ON r.execution_id=l.execution_id "
                "WHERE json_extract(r.manifest_json,'$.runner_config.provider_auth_file_env')=? LIMIT 1", (reference, reference)).fetchone()
                or db.execute("SELECT 1 FROM discussion_leases l LEFT JOIN discussion_runtime r ON r.execution_id=l.execution_id "
                    "WHERE r.manifest_json IS NULL OR json_extract(r.manifest_json,'$.runner_config.provider_auth_file_env')=? LIMIT 1",
                    (reference,)).fetchone())

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
        if config.get('enabled'):
            self.start(config, data)

    def start(self, config, data):
        # Internal investigations must not consume capacity reserved for a
        # larger pipeline retry waiting for existing workers to drain.
        if self.memory_draining(config):
            return
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

    def memory_draining(self, config):
        if not config.get('memory_tiers'):
            return False
        admitted = {row['id'] for row in self.settings.view()['repositories'] if row['dispatch_allowed']}
        with self.database.read_snapshot() as db:
            rows = db.execute("SELECT u.repository_id FROM unit_environment_observations e JOIN units u ON u.id=e.unit_id "
                              "WHERE e.memory_retry_json IS NOT NULL AND e.memory_retry_json!='null' "
                              "AND u.state IN ('queued','building','integrating','reviewing','auditing') "
                              "AND NOT EXISTS(SELECT 1 FROM unit_gates g WHERE g.unit_id=u.id "
                              "AND g.code='operator_pause' AND g.resolved_at IS NULL)")
            return any(row['repository_id'] in admitted for row in rows)

    def observe(self, github, reader):
        errors, view = [], self.settings.view()
        ungranted = any(item.split(':')[0].strip() == 'deployments' for item in
                        ((view.get('roles') or {}).get('controller') or {}).get('missing_recommended') or [])
        for row in view['repositories']:
            profile = (row.get('deployment') or {}).get('profile')
            if not profile or not row['enabled']:
                continue
            if time.monotonic() >= getattr(github, 'deadline', float('inf')):
                break
            entry = deployment_views(self.database).get(str(row['id']), {})
            observed = self.read_live(github, reader, row, profile, entry.get('observed') or {}, ungranted)
            self.save(row['id'], observed=observed)
            if observed['error']:
                errors.append({'repo': row['name'], 'reason': observed['error']})
            with self.database.read_snapshot() as db:
                candidates = db.execute("SELECT c.*,i.github_number,e.details_json FROM units u JOIN candidates c ON c.id=u.current_candidate_id "
                    "JOIN unit_issues m ON m.unit_id=u.id JOIN issues i ON i.id=m.issue_id JOIN candidate_evidence e ON e.candidate_id=c.id AND e.kind='merge' AND e.verdict='pass' "
                    "WHERE u.state='merged' AND u.repository_id=? AND NOT EXISTS(SELECT 1 FROM deployment_observations d WHERE d.candidate_id=c.id AND d.status='success') "
                    'ORDER BY e.observed_at,c.id LIMIT 100', (row['id'],)).fetchall()
            for candidate in candidates:
                if time.monotonic() >= getattr(github, 'deadline', float('inf')):
                    break
                try:
                    merge = json.loads(candidate['details_json']).get('merge_sha')
                    if not merge:
                        continue
                    item = dict(repo=row['name'], repo_id=row['id'], number=candidate['github_number'])
                    live, deploy = (observed.get('live') or {}).get('commit'), observed.get('deploy') or {}
                    verdict, commit, via = None, None, None
                    if live and (merge.startswith(live) or github.contains(item, merge, live)):
                        verdict, commit, via = 'success', live, 'live'
                    elif deploy.get('state') in ('success', 'failure') and deploy.get('sha') and github.contains(item, merge, deploy['sha']):
                        verdict, commit, via = deploy['state'], deploy['sha'], 'github'
                    if verdict:
                        identifier = hashlib.sha256((candidate['id'] + commit + verdict + via).encode()).hexdigest()
                        with self.database.write() as db:
                            writable(db)
                            db.execute('INSERT INTO deployment_observations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                                       (identifier, row['id'], candidate['id'], (profile.get('deploy') or {}).get('environment') or '',
                                        verdict, commit, self.clock(), json.dumps({'via': via, 'run_url': deploy.get('run_url')})))
                except Exception as error:
                    reason = scrub(str(error))[:500] if isinstance(error, GitHubUnavailable) else ('Deployment observation failed (' + type(error).__name__ + '); retry pending')
                    logging.getLogger(__name__).warning('%s: candidate %s', reason, candidate['id'])
                    observed['error'] = '; '.join(filter(None, (observed['error'], reason)))[:500]
                    errors.append({'repo': row['name'], 'reason': reason})
            self.save(row['id'], observed=observed)
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
                    runner.prepare(job)
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
