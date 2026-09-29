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
    require(len(app_ids) == len(set(app_ids)))
    fields(value['readiness'], ('discovered', 'enabled', 'access_ready', 'protection_ready', 'protection_status'))
    require(all(type(value['readiness'][k]) is bool for k in
                ('discovered', 'enabled', 'access_ready', 'protection_ready')))
    require(value['readiness']['protection_status'] in
            ('undiscovered', 'unchecked', 'checking', 'stale', 'unknown', 'needs_setup', 'ready', 'access_needed'))
    # The client understands one additive policy. It will not accept a server's
    # arbitrary ruleset body, permissions, endpoints or shell instructions.
    fields(value['baseline'], ('branch', 'required_approvals', 'dismiss_stale_reviews'))
    require(type(value['baseline']['required_approvals']) is int
            and value['baseline']['required_approvals'] == 2
            and value['baseline']['dismiss_stale_reviews'] is True)
    if value['baseline']['branch'] is not None:
        baseline(value['baseline']['branch'])
    return value


def connection_identity(value):
    """Pin role identities separately from refreshable repository observations."""
    requirements(value)
    return {role: {k: data[k] for k in ('app_id', 'installation_ids')}
            for role, data in value['roles'].items()}


def baseline(branch):
    require(isinstance(branch, str) and 0 < len(branch) <= 255
            and not any(ord(c) < 32 or c in '*?[\\' for c in branch)
            and not branch.startswith('-') and not branch.endswith('/')
            and '..' not in branch and '@{' not in branch)
    return {'name': 'Forge review baseline', 'target': 'branch', 'enforcement': 'active',
            'bypass_actors': [], 'conditions': {'ref_name': {'include': ['refs/heads/' + branch], 'exclude': []}},
            'rules': [{'type': 'pull_request', 'parameters': {
                'required_approving_review_count': 2, 'dismiss_stale_reviews_on_push': True,
                'require_code_owner_review': False, 'require_last_push_approval': False,
                'required_review_thread_resolution': False}}]}
