"""Resumable move from one App per role to the single Forge App.

The controller's App becomes every role's credential. Nothing is deleted:
each former App is kept in ``retired_apps`` so its check runs, reviews and PR
authorship stay attributable, pending effects keep their identity, and the
operator can uninstall the old Apps afterwards. Every step is idempotent, so a
crash or a stale revision is resolved by planning and applying again.
"""
from copy import deepcopy
import time

from .app_access import APP_PERMISSIONS, missing_app_permissions
from .settings import ROLES, digest


class ConsolidationError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def plan(config, observation, *, now):
    """Pure: the next config, the retired identities, and what blocks it."""
    connections = config['connections']
    controller = connections.get('controller')
    if not controller:
        return {'status': 'blocked', 'blockers': [{'code': 'controller_required',
                'detail': 'Configure and verify the Forge (controller) App first.'}], 'retire': [], 'config': config}
    pending = [role for role in ROLES if connections.get(role) != controller]
    if not pending:
        return {'status': 'complete', 'blockers': [], 'retire': [], 'config': config}
    row = observation.get('roles', {}).get('controller', {})
    blockers = []
    if row.get('connection') != digest(controller) or row.get('status') != 'ok':
        blockers.append({'code': 'controller_unverified', 'detail': 'Sync GitHub access so the Forge App is freshly verified.'})
    missing = missing_app_permissions(row.get('permissions', {})) if row.get('connection') == digest(controller) else []
    if missing:
        # A live approval: the owner accepts the wider grant on GitHub.
        blockers.append({'code': 'app_permissions_required', 'missing': missing, 'required': APP_PERMISSIONS,
                         'detail': 'Accept the Forge App permission update on GitHub: ' + ', '.join(missing)})
    covered = {repo['id'] for repo in row.get('repositories', [])}
    uncovered = [repo['name'] for repo in config['repositories'] if repo['enabled'] and repo['id'] not in covered]
    if row.get('connection') == digest(controller) and uncovered:
        blockers.append({'code': 'installation_coverage', 'repositories': uncovered,
                         'detail': 'Install the Forge App on every enabled repository first.'})
    retired = {entry['app_id']: deepcopy(entry) for entry in config.get('retired_apps', [])}
    for role in pending:
        connection = connections.get(role)
        if not connection or connection['app_id'] == controller['app_id']:
            continue
        observed = observation.get('roles', {}).get(role, {})
        slug = observed.get('app_slug') if observed.get('connection') == digest(connection) else None
        entry = retired.setdefault(connection['app_id'], {'app_id': connection['app_id'], 'slug': slug,
                                                           'roles': [], 'retired_at': now})
        entry['slug'] = entry['slug'] or slug
        if role not in entry['roles']:
            entry['roles'].append(role)
    updated = deepcopy(config)
    updated['connections'] = {role: deepcopy(controller) for role in ROLES}
    updated['retired_apps'] = sorted(retired.values(), key=lambda entry: entry['app_id'])
    return {'status': 'blocked' if blockers else 'ready', 'blockers': blockers,
            'retire': updated['retired_apps'], 'config': updated}


class Consolidation:
    def __init__(self, settings, *, clock=None):
        self.settings = settings
        self.clock = clock or getattr(settings, 'clock', time.time)

    def plan(self):
        config = self.settings.load()
        result = plan(config, self.settings.observation(), now=self.clock())
        return {key: value for key, value in result.items() if key != 'config'} | {'revision': digest(config)}

    def apply(self, revision):
        """Switch every role to the Forge App; replaying a finished move is a no-op."""
        with self.settings.locked('config'):
            config, storage_revision = self.settings.config_snapshot()
            if revision != digest(config):
                current = plan(config, self.settings.observation(), now=self.clock())
                if current['status'] == 'complete':
                    return {'status': 'complete', 'revision': digest(config)}
                raise ConsolidationError('plan_changed', 'Settings changed; read a new plan before applying it.')
            observation = self.settings.observation()
            result = plan(config, observation, now=self.clock())
            if result['status'] == 'complete':
                self._share_observation(result['config'], observation)
                return {'status': 'complete', 'revision': digest(config)}
            if result['status'] == 'blocked':
                raise ConsolidationError(result['blockers'][0]['code'], result['blockers'][0]['detail'])
            generation = self.settings.generation()
            self.settings.save_config(result['config'], expected_generation=generation, expected_revision=storage_revision)
            self._share_observation(result['config'], observation, expected_generation=generation)
        return {'status': 'complete', 'revision': digest(result['config']), 'retired': result['retire']}

    def _share_observation(self, config, observation, *, expected_generation=None):
        """Every role now holds the controller's connection, so its observation is theirs.

        Without this, admission would wait for the next sync and pause dispatch."""
        row = observation.get('roles', {}).get('controller')
        if not row or row.get('connection') != digest(config['connections']['controller']):
            return
        roles = {role: deepcopy(row) for role in ROLES
                 if observation['roles'].get(role, {}).get('connection') != row['connection']}
        if roles:
            self.settings.save_observation({'roles': roles, 'observed_at': observation.get('observed_at', self.clock())},
                                           expected_generation=expected_generation)
