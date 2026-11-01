"""Cached repository evidence and optional operator defaults."""
import json
import threading
import time
import uuid

from .factory.config import load, validate, save
from .factory.environments import probe, WORKER_CAPABILITIES
from .factory.github import GitHub, GitHubUnavailable
from .settings import digest
from .store.configuration import Configuration
from .store.runtime import writable

SCAN_TIMEOUT = 120
SCAN_CACHE_AGE = 600


class RepositoryEnvironments:
    def __init__(self, settings, *, github_class=GitHub):
        self.settings = settings
        self.root = settings.root.parent if settings.root.name == 'setup' else settings.root
        self.github_class = github_class

    @property
    def database(self):
        return self.settings.database

    def snapshot(self, *, runtime=None):
        with self.database.read_snapshot() as db:
            config = db.execute("SELECT settings_json FROM configuration WHERE domain='runtime.json' AND subject='instance' AND active=1").fetchone() if runtime is None else None
            rows = db.execute('SELECT * FROM (SELECT s.*,row_number() OVER (PARTITION BY repository_id '
                              'ORDER BY created_at DESC,rowid DESC) AS position FROM environment_scans s) '
                              'WHERE position=1').fetchall()
            # The selected scan still owns status. Fetch its repository's last
            # completed proposal once per snapshot, including an older success
            # while the newest scan is running or unavailable.
            repositories = json.dumps([row['repository_id'] for row in rows])
            proposals = {str(row['repository_id']): json.loads(row['proposal_json']) for row in db.execute(
                "SELECT repository_id,proposal_json FROM (SELECT s.repository_id,p.proposal_json,"
                "row_number() OVER (PARTITION BY s.repository_id ORDER BY s.created_at DESC,s.rowid DESC,p.revision DESC) AS position "
                "FROM environment_scans s JOIN environment_proposals p ON p.scan_id=s.id "
                "WHERE s.repository_id IN (SELECT value FROM json_each(?)) AND s.status='completed' "
                "AND p.status IN ('proposed','approved','applied')) WHERE position=1", (repositories,))}
            scans = {}
            for row in rows:
                key = str(row['repository_id'])
                scans[key] = dict(id=row['source_id'] or row['id'], started_at=row['created_at'],
                                  status='complete' if row['status'] == 'completed' else row['status'], error=row['error'])
            return (validate(json.loads(config[0]) if config else {}) if runtime is None else runtime), proposals, scans

    def automatic(self, repo):
        """Return cached evidence and refresh it without blocking dispatch."""
        snapshot = self.snapshot()
        config, proposals, scans = snapshot
        key = str(repo['repo_id'])
        scan = scans.get(key, {})
        current = self.view(repo['repo_id'], snapshot)
        age = self.settings.clock() - scan.get('started_at', 0)
        if current['status'] != 'running' and (not scan or age >= SCAN_CACHE_AGE):
            try:
                self.start_scan(repo, digest(self.settings.load()), current['revision'])
            except ValueError:
                pass  # A concurrent operator scan owns the refresh.
        proposal = proposals.get(key) or {**config['repository_environments'].get(key, {}), 'repo': repo['repo'], 'repo_id': repo['repo_id']}
        return proposal if proposal.get('repo') == repo['repo'] and proposal.get('repo_id') == repo['repo_id'] else {}

    def view(self, repo_id, snapshot=None):
        config, proposals, scans = snapshot if snapshot is not None else self.snapshot()
        key = str(repo_id)
        stored = config['repository_environments'].get(key)
        profile = stored if stored and stored.get('default') else None
        proposal, scan = proposals.get(key), scans.get(key, {})
        status, error = scan.get('status', 'idle'), scan.get('error')
        if status == 'running' and self.settings.clock() - scan['started_at'] >= SCAN_TIMEOUT:
            status, error = 'failed', 'The scan did not finish. Scan requirements again.'
        same = profile and proposal and all(profile.get(k) == proposal.get(k) for k in ('head', 'services', 'evidence'))
        return {'profile': profile, 'proposal': proposal if status not in ('running', 'failed') and not same else None,
                'worker_capabilities': dict(WORKER_CAPABILITIES),
                'external_verification': (proposal or {}).get('external_verification'),
                'status': status, 'error': error, 'full_available': 'full' in config['environment_tiers'],
                'revision': digest({'profile': stored, 'proposal': proposal, 'scan': scan,
                                    'full_available': 'full' in config['environment_tiers']})}

    def identity(self, repo_id, revision):
        if self.settings.root.name == 'setup':
            raise ValueError('Finish initial Forge setup before saving database requirements.')
        if self.settings.paused():
            raise ValueError('Factory reset is in progress.')
        if revision != digest(self.settings.load()):
            raise ValueError('Repository settings changed. Reload and try again.')
        row = next((r for r in self.settings.view()['repositories'] if r['id'] == repo_id), None)
        if not row or not row['access_ready']:
            raise ValueError('Refresh GitHub access before scanning or saving requirements.')
        return {'repo_id': repo_id, 'repo': row['name']}

    def act(self, repo_id, revision, environment_revision, action):
        repo = self.identity(repo_id, revision)
        if action == 'scan':
            self.start_scan(repo, revision, environment_revision)
        elif action == 'confirm':
            self.confirm(repo, revision, environment_revision)
        elif action == 'automatic':
            with self.settings.locked('runtime-config'):
                if self.view(repo_id)['revision'] != environment_revision:
                    raise ValueError('Database requirements changed. Reload and review them again.')
                config = load(self.root)
                config['repository_environments'].pop(str(repo_id), None)
                save(self.root, config)
        else:
            raise ValueError('Choose scan or confirm for database requirements.')

    def start_scan(self, repo, revision, environment_revision, *, background=True):
        identifier = uuid.uuid4().hex
        with self.database.write() as db:
            writable(db)
            current = self.view(repo['repo_id'])
            if current['revision'] != environment_revision:
                raise ValueError('Database requirements changed. Reload and review them again.')
            if current['status'] == 'running':
                raise ValueError('A requirements scan is already running.')
            if self.settings.paused() or digest(self.settings.load()) != revision:
                raise ValueError('Repository settings changed. Reload and try again.')
            db.execute('INSERT INTO repositories(id,full_name,enabled) VALUES(?,?,0) ON CONFLICT(id) DO NOTHING',
                       (repo['repo_id'], repo['repo']))
            db.execute('INSERT INTO environment_scans(id,repository_id,source_revision,status,created_at,source_id) '
                       "VALUES(?,?,?,'running',?,?)", (identifier, repo['repo_id'], revision, self.settings.clock(), identifier))
        if background:
            threading.Thread(target=self.scan, args=(repo, revision, identifier), daemon=True).start()
        else:
            self.scan(repo, revision, identifier)

    def scan(self, repo, revision, identifier):
        try:
            github = self.github_class(self.settings, load(self.root), clock=self.settings.clock)
            github.deadline = time.monotonic() + 90
            proposal, error = probe(github, repo), None
        except (GitHubUnavailable, ValueError, OSError, KeyError, TypeError):
            proposal, error = None, 'Could not finish the scan. Check GitHub access and try again.'
        with self.database.write() as db:
            writable(db)
            row = db.execute('SELECT * FROM environment_scans WHERE repository_id=? ORDER BY created_at DESC,rowid DESC LIMIT 1',
                             (repo['repo_id'],)).fetchone()
            if row is None or row['id'] != identifier or row['status'] != 'running':
                return
            if (self.settings.paused() or digest(self.settings.load()) != revision
                    or self.settings.clock() - row['created_at'] >= SCAN_TIMEOUT):
                error = 'Repository settings changed or the scan expired. Scan requirements again.'
            if error is None:
                db.execute('INSERT INTO environment_proposals(id,scan_id,revision,status,proposal_json,created_at) '
                           "VALUES(?,?,1,'proposed',?,?)", (identifier, identifier, json.dumps(proposal, allow_nan=False), self.settings.clock()))
            db.execute('UPDATE environment_scans SET status=?,error=? WHERE id=?',
                       ('failed' if error else 'completed', error, identifier))

    def confirm(self, repo, revision, environment_revision):
        self.identity(repo['repo_id'], revision)
        with self.settings.locked('runtime-config'), self.database.write() as db:
            writable(db)
            config, proposals, scans = self.snapshot()
            current = self.view(repo['repo_id'], (config, proposals, scans))
            if current['revision'] != environment_revision or self.settings.paused() or digest(self.settings.load()) != revision:
                raise ValueError('Database requirements changed. Reload and review them again.')
            proposal = current['proposal']
            if not proposal or proposal.get('repo_id') != repo['repo_id'] or proposal.get('repo') != repo['repo']:
                raise ValueError('Scan this repository before saving its requirements.')
            if proposal['services'] and not current['full_available']:
                raise ValueError('Database workers are unavailable on this host. Ask the Forge administrator to enable them.')
            config['repository_environments'][str(repo['repo_id'])] = {
                **{key: proposal[key] for key in ('services', 'evidence', 'head')},
                'confirmed_at': self.settings.clock(), 'default': True}
            prior = db.execute("SELECT revision FROM configuration WHERE domain='runtime.json' AND subject='instance' AND active=1").fetchone()
            Configuration(self.database, clock=self.settings.clock).replace_in(db, 'runtime.json', validate(config),
                                                                              expected_revision=prior[0] if prior else 0)
