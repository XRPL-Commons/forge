"""Cached repository evidence and optional operator defaults."""
import threading
import time
import uuid

from .factory.config import load, validate
from .factory.environments import probe
from .factory.github import GitHub, GitHubUnavailable
from .settings import digest, read_json, write_json
from .state import AtomicStore

SCAN_TIMEOUT = 120
SCAN_CACHE_AGE = 600


def read_optional(path):
    return read_json(path) if path.exists() else {}


class RepositoryEnvironments:
    def __init__(self, settings, *, github_class=GitHub):
        self.settings = settings
        self.root = settings.root.parent if settings.root.name == 'setup' else settings.root
        self.github_class = github_class

    def snapshot(self):
        # Polling never contacts GitHub or creates state.
        config = load(self.root)
        return (config, read_optional(self.root / 'state/environment-proposals.json'),
                read_optional(self.root / 'state/environment-scans.json'))

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
                write_json(self.root / 'runtime.json', validate(config))
        else:
            raise ValueError('Choose scan or confirm for database requirements.')

    def start_scan(self, repo, revision, environment_revision):
        store = AtomicStore(self.root / 'state', 'environment-scans')
        key, identifier = str(repo['repo_id']), uuid.uuid4().hex
        with store.transaction() as scans:
            current = self.view(repo['repo_id'])
            if current['revision'] != environment_revision:
                raise ValueError('Database requirements changed. Reload and review them again.')
            if current['status'] == 'running':
                raise ValueError('A requirements scan is already running.')
            scans[key] = {'status': 'running', 'started_at': self.settings.clock(), 'id': identifier}
        threading.Thread(target=self.scan, args=(repo, revision, identifier), daemon=True).start()

    def scan(self, repo, revision, identifier):
        try:
            github = self.github_class(self.settings, load(self.root), clock=self.settings.clock)
            github.deadline = time.monotonic() + 90
            proposal = probe(github, repo)
            error = None
        except (GitHubUnavailable, ValueError, OSError, KeyError, TypeError):
            proposal, error = None, 'Could not finish the scan. Check GitHub access and try again.'
        store = AtomicStore(self.root / 'state', 'environment-scans')
        with store.transaction() as scans:
            key = str(repo['repo_id'])
            current = scans.get(key, {})
            if current.get('id') != identifier:
                return  # A newer scan or reset owns this repository now.
            if (self.settings.paused() or revision != digest(self.settings.load())
                    or self.settings.clock() - current['started_at'] >= SCAN_TIMEOUT):
                error = 'Repository settings changed or the scan expired. Scan requirements again.'
            if error is None:
                with AtomicStore(self.root / 'state', 'environment-proposals').transaction() as proposals:
                    proposals[key] = proposal
            scans[key] = {**current, 'status': 'failed' if error else 'complete', 'error': error}

    def confirm(self, repo, revision, environment_revision):
        key = str(repo['repo_id'])
        with self.settings.locked('runtime-config'), \
                AtomicStore(self.root / 'state', 'environment-scans').transaction() as scans, \
                AtomicStore(self.root / 'state', 'environment-proposals').transaction() as proposals:
            self.identity(repo['repo_id'], revision)
            config = load(self.root)
            current = self.view(repo['repo_id'], (config, proposals, scans))
            if current['revision'] != environment_revision:
                raise ValueError('Database requirements changed. Reload and review them again.')
            proposal = current['proposal']
            if not proposal or proposal.get('repo_id') != repo['repo_id'] or proposal.get('repo') != repo['repo']:
                raise ValueError('Scan this repository before saving its requirements.')
            if proposal['services'] and not current['full_available']:
                raise ValueError('Database workers are unavailable on this host. Ask the Forge administrator to enable them.')
            config['repository_environments'][key] = {
                **{k: proposal[k] for k in ('services', 'evidence', 'head')}, 'confirmed_at': self.settings.clock(), 'default': True}
            write_json(self.root / 'runtime.json', validate(config))
