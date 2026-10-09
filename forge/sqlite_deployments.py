"""Deployment profiles and strategist execution state in the selected store."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import time

from .deployments import Deployments, host, OPTIONAL_PERMISSIONS
from .factory.github import GitHubUnavailable
from .store.auxiliary import save_in, job_in
from .store.operations import deployment_views
from .store.runtime import writable


class SqliteDeployments(Deployments):
    def __init__(self, root, *, runner, github, settings, clock=time.time):
        self.root = Path(root)
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
                or db.execute('SELECT 1 FROM discussion_leases LIMIT 1').fetchone())

    def memory_draining(self, config):
        if not config.get('memory_tiers'):
            return False
        with self.database.read_snapshot() as db:
            return db.execute("SELECT 1 FROM unit_environment_observations e JOIN units u ON u.id=e.unit_id "
                              "WHERE e.memory_retry_json IS NOT NULL AND e.memory_retry_json!='null' AND u.state NOT IN ('merged','closed','dissolved') LIMIT 1").fetchone() is not None

    def observe(self, github, ledger, reader):
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
                merge = json.loads(candidate['details_json']).get('merge_sha')
                if not merge:
                    continue
                item = dict(repo=row['name'], repo_id=row['id'], number=candidate['github_number'])
                live, deploy = (observed.get('live') or {}).get('commit'), observed.get('deploy') or {}
                verdict, commit, via = None, None, None
                try:
                    if live and (merge.startswith(live) or github.contains(item, merge, live)):
                        verdict, commit, via = 'success', live, 'live'
                    elif deploy.get('state') in ('success', 'failure') and deploy.get('sha') and github.contains(item, merge, deploy['sha']):
                        verdict, commit, via = deploy['state'], deploy['sha'], 'github'
                except GitHubUnavailable as error:
                    errors.append({'repo': row['name'], 'reason': str(error)})
                    continue
                if verdict:
                    identifier = hashlib.sha256((candidate['id'] + commit + verdict + via).encode()).hexdigest()
                    with self.database.write() as db:
                        writable(db)
                        db.execute('INSERT INTO deployment_observations VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING',
                                   (identifier, row['id'], candidate['id'], (profile.get('deploy') or {}).get('environment') or '',
                                    verdict, commit, self.clock(), json.dumps({'via': via, 'run_url': deploy.get('run_url')})))
        return errors
