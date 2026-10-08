"""Repository scans and confirmation, with per-scan authority and atomic results."""
import json
import threading
import time
import uuid

from .repository_environments import RepositoryEnvironments, SCAN_TIMEOUT
from .factory.config import load, validate
from .factory.environments import probe
from .factory.github import GitHubUnavailable
from .settings import digest
from .store.configuration import Configuration
from .store.runtime import writable


class SqliteRepositoryEnvironments(RepositoryEnvironments):
    @property
    def database(self):
        return self.settings.database

    def snapshot(self):
        with self.database.read_snapshot() as db:
            config = db.execute("SELECT settings_json FROM configuration WHERE domain='runtime.json' AND subject='instance' AND active=1").fetchone()
            rows = db.execute('SELECT s.* FROM environment_scans s WHERE s.id=(SELECT n.id FROM environment_scans n '
                              'WHERE n.repository_id=s.repository_id ORDER BY n.created_at DESC,n.rowid DESC LIMIT 1)').fetchall()
            proposals, scans = {}, {}
            for row in rows:
                key = str(row['repository_id'])
                scans[key] = dict(id=row['source_id'] or row['id'], started_at=row['created_at'],
                                  status='complete' if row['status'] == 'completed' else row['status'], error=row['error'])
                # A refresh can be running or unavailable while the last
                # successful evidence remains useful to automatic selection.
                # Reset supersedes these proposals without deleting history.
                proposal = db.execute('SELECT p.proposal_json FROM environment_proposals p JOIN environment_scans s ON s.id=p.scan_id '
                                      "WHERE s.repository_id=? AND s.status='completed' AND p.status IN ('proposed','approved','applied') "
                                      'ORDER BY s.created_at DESC,s.rowid DESC,p.revision DESC LIMIT 1',
                                      (row['repository_id'],)).fetchone()
                if proposal:
                    proposals[key] = json.loads(proposal[0])
            return validate(json.loads(config[0]) if config else {}), proposals, scans

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
