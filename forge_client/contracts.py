"""Closed, credential-free contracts for repository setup."""
import hashlib
import json
import re

ROLES = ('build', 'review', 'audit', 'controller')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/(?!\.{1,2}$)[A-Za-z0-9_.-]+')
REVISION = re.compile(r'[a-f0-9]{64}')


class SetupError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def require(condition, code='invalid_contract', message='Invalid repository setup data.'):
    if not condition:
        raise SetupError(code, message)


def fields(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys))


def positive(value):
    return type(value) is int and 0 < value < 2**53


def repository(value):
    fields(value, ('id', 'name'))
    require(positive(value['id']) and isinstance(value['name'], str)
            and len(value['name']) <= 200 and NAME.fullmatch(value['name']))
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def requirements(value):
    fields(value, ('version', 'repository', 'revision', 'roles', 'readiness', 'baseline'))
    require(type(value['version']) is int and value['version'] == 1)
    repository(value['repository'])
    require(isinstance(value['revision'], str) and REVISION.fullmatch(value['revision']))
    fields(value['roles'], ROLES)
    app_ids = []
    for role in value['roles'].values():
        fields(role, ('app_id', 'installation_ids', 'access'))
        require(role['app_id'] is None or positive(role['app_id']))
        ids = role['installation_ids']
        require(isinstance(ids, list) and len(ids) <= 20 and all(positive(x) for x in ids)
                and len(ids) == len(set(ids)))
        require(role['app_id'] is not None or not ids)
        require(role['access'] in ('ready', 'missing_access', 'unknown', 'not_configured',
                                  'stale', 'missing_permissions', 'name_changed', 'archived'))
        if role['app_id'] is not None:
            app_ids.append(role['app_id'])
    controller=value['roles']['controller']
    for role in value['roles'].values():
        if role['app_id'] is not None and app_ids.count(role['app_id'])>1:
            require(role['app_id']==controller['app_id'] and role['installation_ids']==controller['installation_ids'],
                    'invalid_contract', 'Shared role identities must use the exact Forge App installation.')
    fields(value['readiness'], ('discovered', 'enabled', 'access_ready', 'protection_ready', 'protection_status'))
    require(all(type(value['readiness'][k]) is bool for k in
                ('discovered', 'enabled', 'access_ready', 'protection_ready')))
    require(value['readiness']['protection_status'] in
            ('undiscovered', 'unchecked', 'checking', 'stale', 'unknown', 'needs_setup', 'ready', 'access_needed'))
    require(value['readiness']['access_ready'] == all(r['access'] == 'ready' for r in value['roles'].values()))
    require(value['readiness']['protection_ready'] == (value['readiness']['protection_status'] == 'ready'))
    require(value['readiness']['discovered'] or not any(value['readiness'][k] for k in
            ('enabled', 'access_ready', 'protection_ready')))
    # The client understands these additive policies. It will not accept a server's
    # arbitrary ruleset body, permissions, endpoints or shell instructions.
    baseline_fields={'branch','required_approvals','dismiss_stale_reviews'}
    require(isinstance(value['baseline'],dict) and baseline_fields <= set(value['baseline']) <= baseline_fields|{'merge_check_scope','github_protection'})
    if 'github_protection' in value['baseline']:
        require(value['baseline']['github_protection'] in ('required','optional'))
    if 'merge_check_scope' in value['baseline']:
        require(value['baseline']['merge_check_scope'] in ('all','forge'))
    require(type(value['baseline']['required_approvals']) is int
            and value['baseline']['required_approvals'] == 2
            and value['baseline']['dismiss_stale_reviews'] is True)
    if value['readiness']['protection_status'] in ('ready', 'needs_setup'):
        require(value['baseline']['branch'] is not None)
    if value['baseline']['branch'] is not None:
        validate_branch(value['baseline']['branch'])
    return value


def connection_identity(value):
    """Pin role identities separately from refreshable repository observations."""
    requirements(value)
    return {role: {k: data[k] for k in ('app_id', 'installation_ids')}
            for role, data in value['roles'].items()}


def validate_branch(branch):
    require(isinstance(branch, str) and 0 < len(branch) <= 255
            and not any(ord(c) < 32 or c in '*?[\\' for c in branch)
            and not branch.startswith('-') and not branch.endswith('/')
            and '..' not in branch and '@{' not in branch)


def baseline(branch, controller_app_id, merge_check_scope=None):
    """Forge's additive ruleset: one App bypass actor, and with scope 'all' the
    forge/merge-policy check required from that App on every pull request.

    It never replaces or relaxes another ruleset or classic protection."""
    validate_branch(branch)
    require(positive(controller_app_id), 'controller_identity_required',
            'Verify the controller App before preparing repository rules.')
    require(merge_check_scope in (None, 'all', 'forge'), 'invalid_merge_check_scope')
    rules = [{'type': 'pull_request', 'parameters': {
        'required_approving_review_count': 2, 'dismiss_stale_reviews_on_push': True,
        'require_code_owner_review': False, 'require_last_push_approval': False,
        'required_review_thread_resolution': False}}]
    if merge_check_scope == 'all':
        rules.append({'type': 'required_status_checks', 'parameters': {
            'strict_required_status_checks_policy': False,
            'required_status_checks': [{'context': 'forge/merge-policy', 'integration_id': controller_app_id}]}})
    return {'name': 'Forge review baseline', 'target': 'branch', 'enforcement': 'active',
            # RepositoryRole 5 is GitHub's administrator role. Keep operators
            # able to bypass this additive baseline independently of the App.
            'bypass_actors': [
                {'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'},
                {'actor_id': controller_app_id, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'},
            ],
            'conditions': {'ref_name': {'include': ['refs/heads/' + branch], 'exclude': []}},
            'rules': rules}
