"""Implementation readiness from the same revision-bound report shown in Settings.

Access-only planning and diagnostics keep Settings.admission. This adapter adds
protection freshness and branch identity for delivery work, and projects that
observation into the existing repository_setup lifecycle gate.
"""
import uuid

from ..repository_policy import protection_ready
from ..repository_setup import MAX_AGE, RepositorySetup, SetupObservationChanged, observation_key
from ..settings import ROLES, digest
from ..store.models import Command, VersionConflict
from ..store.repository_readiness import read_revision
from ..store.transitions import Lifecycle, TERMINAL
from .github import GitHubUnavailable


class RepositoryReadiness:
    def __init__(self, store, settings, *, clock):
        self.store, self.settings, self.clock = store, settings, clock
        self.setup = RepositorySetup(settings)
        self.commands = Lifecycle(store, clock=clock)

    def evaluate(self, item):
        # Access/config/report must describe one setup observation, not a mix
        # read across a concurrent connection or operator configuration change.
        with self.setup.database.read_session() as db:
            value = self._evaluate(item)
            value['read_authority'] = dict(domain=self.settings.domain, scope=self.settings.scope,
                revision=read_revision(db, domain=self.settings.domain, scope=self.settings.scope,
                                       repository_id=value['repository_id']))
            return value

    def _evaluate(self, item):
        unit = item.get('unit') or {}
        repo = item.get('repo') or (item.get('issue') or {}).get('repository')
        branch = item.get('target_branch') or unit.get('base_branch')
        config = self.settings.load()
        row = next((row for row in config['repositories'] if row['name'].lower() == (repo or '').lower()), None)
        access = self.settings.admission(repo) if repo else {'allowed': False, 'reason': 'repository_not_enabled'}
        revision = observation_key(config, row) if row else ''
        result = dict(repository_id=row['id'] if row else unit.get('repository_id', item.get('repo_id')),
                      base_branch=branch, allowed=False, access_allowed=access['allowed'] is True,
                      source_revision=revision, reason=access['reason'], valid_until=None)
        if not result['access_allowed'] or row is None:
            return result
        access_observation = self.settings.observation()['roles']
        result['valid_until'] = min(access_observation[role]['observed_at'] + config['access_max_age_seconds']
                                    for role in ROLES)
        saved = self.setup.read()['repositories'].get(str(row['id']), {})
        report, status = saved.get('report') or {}, saved.get('status')
        known = saved.get('last_known') or {}
        # The periodic Settings check first marks the row 'checking', then may
        # record a failed read (no branch, unavailable). Neither is evidence that
        # protection changed: keep the last verified report of this same revision
        # while it is fresh, instead of opening and resolving the gate each sync.
        if (status == 'checking' or not report.get('available')) and known.get('available'):
            report, status = known, 'ready' if protection_ready(known) else 'needs_setup'
        if saved.get('revision') != revision or saved.get('name') != row['name']:
            result['reason'] = 'protection_unchecked'
        elif not 0 <= self.clock() - report.get('observed_at', 0) < MAX_AGE:
            result['reason'] = 'protection_stale'
        elif branch is not None and report.get('branch') is not None and report.get('branch') != branch:
            # A report without a branch is a failed read, not a branch change.
            result['reason'] = 'protection_branch_changed'
        elif report.get('repo_id', row['id']) != row['id']:
            result['reason'] = 'protection_identity_changed'
        elif status != 'ready' or not protection_ready(report) or branch is not None and report.get('branch') != branch:
            result['reason'] = 'protection_unavailable' if not report.get('available') else 'protection_needs_setup'
        else:
            result.update(allowed=True, reason='implementation_ready', valid_until=min(result['valid_until'], report['observed_at'] + MAX_AGE))
        return result

    def allowed(self, item):
        return self.evaluate(item)['allowed']

    def observe(self, unit_id):
        with self.store.read_snapshot() as db:
            unit = db.execute('SELECT u.*,r.full_name FROM units u JOIN repositories r ON r.id=u.repository_id WHERE u.id=?',
                              (unit_id,)).fetchone()
        if unit is None or unit['state'] in TERMINAL:
            return None
        value = self.evaluate({'repo': unit['full_name'], 'repo_id': unit['repository_id'],
                               'target_branch': unit['base_branch']})
        # Missing configuration is still tied to the actual delivery repository.
        value.update(repository_id=unit['repository_id'], base_branch=unit['base_branch'])
        return self.commands.observe_repository_readiness(
            Command(str(uuid.uuid4()), 'supervisor:repository-readiness', unit_id, unit['version']), **value)

    def protections(self, snapshot, item, branch, github):
        """Persist the actual final dispatch read, bound to its captured settings."""
        with self.setup.database.read_session():
            config, generation = self.settings.load(), self.settings.generation()
        row = next((row for row in config['repositories'] if row['id'] == item['repo_id'] and row['name'] == item['repo']), None)
        if row is None:
            self.observe(snapshot['unit']['id'])
            return False
        try:
            report = github.protection_report(item, {'base_ref': branch})
        except GitHubUnavailable:
            report = {'repo_id': row['id'], 'branch': branch, 'available': False,
                      'requirements': [], 'errors': [{'detail': 'Could not verify repository protections.'}],
                      'observed_at': self.clock()}
        if report.get('repo_id') != row['id'] or report.get('branch') != branch:
            raise GitHubUnavailable('Protection observation identity does not match this repository and branch')
        data = {'repositories': {str(row['id']): dict(name=row['name'], revision=observation_key(config, row),
            status='ready' if protection_ready(report) else 'needs_setup' if report.get('available') else 'unknown',
            started_at=self.clock(), report=report)}}
        try:
            self.setup.save_observation(row['id'], data, expected_generation=generation, expected_config=digest(config))
        except SetupObservationChanged as error:
            raise VersionConflict('Repository settings changed during the protection check') from error
        result = self.observe(snapshot['unit']['id'])
        return bool(result and result['allowed'])
