"""Local repository preparation; remote verification is an explicit interface.

The authenticated transport must supply instance generation and pinned App
identities. This module never receives GitHub token values and sends only
repository identity/revision to its Forge interface.
"""
from copy import deepcopy
import time

from .contracts import SetupError, connection_identity, digest, fields, require, requirements


class Preparation:
    def __init__(self, github, forge, *, instance, generation, apps, clock=time.time, progress=None, repair=None, github_protection=None):
        self.github, self.forge, self.clock = github, forge, clock
        self.instance, self.generation, self.apps = instance, generation, deepcopy(apps)
        self.progress = progress or (lambda phase, step: None)
        require(repair in (None, 'apply-if-admin', 'print-commands', 'skip'))
        require(github_protection is None, 'github_protection_retired',
                'Forge enforces its merge policy in the supervisor. GitHub protection settings are no longer configured by Forge.')

    def _authorize(self, target, capability):
        self.forge.authorize(self.instance, self.generation,
                             {'id': target['id'], 'name': target['name']}, capability)

    def _requirements(self, target):
        # authorize is repeated before every local write as well as server work.
        # The concrete transport must enforce authentication/scopes itself.
        self._authorize(target, 'repositories:read')
        value = deepcopy(requirements(self.forge.requirements({'id': target['id'], 'name': target['name']})))
        require(value['repository']['id'] == target['id']
                and value['repository']['name'].lower() == target['name'].lower(),
                'repository_identity_changed', 'Forge and GitHub disagree about the repository identity.')
        require(connection_identity(value) == self.apps, 'app_identity_changed',
                'Forge App identities changed; review this connection before continuing.')
        return value

    def plan(self, name):
        target = self.github.repository(name)
        value = self._requirements(target)
        steps, blockers = [], []
        for role, app in value['roles'].items():
            if app['access'] == 'ready':
                continue
            if app['access'] == 'missing_access' and len(app['installation_ids']) == 1:
                if not any(step.get('installation_id')==app['installation_ids'][0] for step in steps):
                    steps.append({'kind': 'github.installation.add_repository', 'role': role,
                                  'installation_id': app['installation_ids'][0]})
            else:
                blockers.append({'code': 'app_access_unverified', 'role': role})
        if any(step['kind'].startswith('github.') for step in steps) and not target['admin']:
            blockers.append({'code': 'github_admin_required'})
        created_at = self.clock()
        plan = {'version': 1, 'instance': self.instance, 'generation': self.generation,
                'target': target, 'revision': value['revision'], 'apps': deepcopy(self.apps),
                'created_at': created_at, 'expires_at': created_at + 600,
                'steps': steps, 'blockers': blockers}
        plan['id'] = digest(plan)
        return plan

    def apply(self, plan, *, enable=False):
        fields(plan, ('version', 'instance', 'generation', 'target', 'revision', 'apps',
                      'created_at', 'expires_at', 'steps', 'blockers', 'id'))
        require(type(enable) is bool and type(plan['version']) is int and plan['version'] == 1)
        require(plan['id'] == digest({k: v for k, v in plan.items() if k != 'id'}),
                'plan_changed', 'The saved plan changed; create a new plan.')
        require(plan['instance'] == self.instance and plan['generation'] == self.generation
                and plan['apps'] == self.apps, 'connection_changed', 'The plan belongs to a different connection.')
        require(type(plan['created_at']) in (int, float) and type(plan['expires_at']) in (int, float)
                and plan['created_at'] <= self.clock() < plan['expires_at'] <= plan['created_at'] + 600,
                'plan_expired', 'The setup plan expired; create a new plan.')
        target = plan['target']
        # Reconstruct every local action from current GitHub/Forge observations.
        # An edited action list, endpoint or ruleset body is never executable.
        current = self.plan(target['name'])
        require(current['target'] == target and current['revision'] == plan['revision']
                and current['steps'] == plan['steps'] and current['blockers'] == plan['blockers'],
                'plan_stale', 'Repository setup changed; create a new plan.')
        self._authorize(target, 'repositories:prepare')
        if enable:
            self._authorize(target, 'repositories:activate')
        completed = []
        if plan['blockers']:
            return {'status': 'blocked', 'completed': completed, 'blockers': plan['blockers']}
        for step in plan['steps']:
            fresh_target = self.github.repository(target['name'])
            require(fresh_target == target, 'repository_identity_changed',
                    'Repository identity, default branch or permissions changed; create a new plan.')
            fresh = self._requirements(target)
            self._authorize(target, 'repositories:prepare')
            require(fresh['revision'] == plan['revision'], 'plan_stale', 'Forge settings changed; create a new plan.')
            if step['kind'] == 'github.installation.add_repository':
                if fresh['roles'][step['role']]['access'] != 'ready':
                    try:
                        self.progress('started', step)
                        self.github.grant_repository(step['installation_id'], target['id'])
                    except SetupError as error:
                        return {'status': 'needs_user_action', 'completed': completed,
                                'code': error.code, 'next_actions': [{
                                    'kind': 'open_url',
                                    'url': self.github.installation_url(target, step['installation_id']),
                                    'reason': 'Check this App installation and grant the repository access.'}]}
            else:
                raise SetupError('retired_preparation_action', 'This saved GitHub rule action is retired. Create a new preparation plan.')
            completed.append(deepcopy(step))
            self.progress('completed', step)
        # No raw GitHub result or local authentication data crosses this boundary.
        self._authorize(target, 'repositories:prepare')
        self.forge.verify({'id': target['id'], 'name': target['name']}, plan['revision'])
        final = self._requirements(target)
        if not (final['readiness']['access_ready'] and final['readiness']['protection_ready']):
            return {'status': 'verification_pending', 'completed': completed,
                    'next_actions': [{'kind': 'forge.repositories.verify'}]}
        if enable:
            self._authorize(target, 'repositories:activate')
            self.forge.enable({'id': target['id'], 'name': target['name']}, final['revision'])
            final = self._requirements(target)
            require(final['readiness']['enabled'], 'activation_unconfirmed', 'Activation has not been verified.')
        return {'status': 'enabled' if final['readiness']['enabled'] else 'ready', 'completed': completed,
                'repository': final['repository']}

    def setup(self, name, *, apply=False, enable=False):
        """Bounded coordination for one explicit repository setup request.

        Real transports may return before verification finishes; the caller can
        resume from the returned status. This function never retries an uncertain
        administrative write or continues past a browser handoff.
        """
        require(type(apply) is bool and type(enable) is bool)
        if not apply:
            return {'status': 'planned', 'plan': self.plan(name)}
        plan = self.plan(name)
        return self.apply(plan, enable=enable)
