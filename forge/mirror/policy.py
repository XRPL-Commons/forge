"""Pinned dependency policy; live observations can only remove authority."""
from dataclasses import dataclass
from copy import deepcopy
import re
from urllib.parse import urlsplit

from ..factory.source_inputs import digest

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,99}/[A-Za-z0-9_.-]{1,100}\Z')
OID = re.compile(r'(?:[0-9a-f]{40}|[0-9a-f]{64})\Z')


class MirrorError(ValueError):
    def __init__(self, reason, *, transient=False):
        self.reason, self.transient = reason, transient
        super().__init__(reason)


def canonical_name(value):
    if not isinstance(value, str) or not NAME.fullmatch(value) or value.split('/')[1] in {'.', '..'}:
        raise MirrorError('repository_unavailable')
    return value.lower()


def github_repository(value):
    """Normalize supported Git transport declarations, never arbitrary URLs."""
    if not isinstance(value, str) or len(value) > 2048 or any(c in value for c in '\\%\x00\r\n\t '):
        raise MirrorError('dependency_format_unsupported')
    value = value.split('#', 1)[0]
    if value.startswith('git+'):
        value = value[4:]
    if value.startswith('github:'):
        name = value[7:]
    elif value.startswith('git@github.com:'):
        name = value[15:]
    else:
        parsed = urlsplit(value)
        if (parsed.scheme not in {'https', 'ssh', 'git'} or parsed.hostname != 'github.com'
                or parsed.port is not None or parsed.password is not None or parsed.query
                or parsed.username not in ({None, 'git'} if parsed.scheme == 'ssh' else {None})):
            raise MirrorError('dependency_format_unsupported')
        name = parsed.path.removeprefix('/')
    if name.endswith('.git'):
        name = name[:-4]
    return canonical_name(name)


@dataclass(frozen=True)
class Repository:
    repository_id: int
    canonical_name: str
    owner_id: int
    owner_type: str
    installation_id: int | None
    default_branch: str = 'main'
    actual_access: bool = False

    def __post_init__(self):
        canonical_name(self.canonical_name)
        if type(self.repository_id) is not int or self.repository_id <= 0 or type(self.owner_id) is not int or self.owner_id <= 0:
            raise MirrorError('repository_unavailable')


def _identities(values):
    if not isinstance(values, list) or len(values) > 2048:
        raise MirrorError('dependency_format_unsupported')
    for item in values:
        if not isinstance(item, dict) or not set(item) <= {'repository_id', 'canonical_name', 'source_evidence'}:
            raise MirrorError('dependency_format_unsupported')
        canonical_name(item['canonical_name'])
        if item.get('repository_id') is not None and (type(item['repository_id']) is not int or item['repository_id'] <= 0):
            raise MirrorError('dependency_format_unsupported')


def validate_policy(policy):
    expected = {'version', 'project_repository_id', 'canonical_repository', 'revision', 'manifest_digest',
                'declared_repositories', 'organization_installations', 'denied_repositories', 'extractor_version'}
    if not isinstance(policy, dict) or set(policy) != expected or policy['version'] != 1 or policy['extractor_version'] != 1:
        raise MirrorError('dependency_format_unsupported')
    if type(policy['project_repository_id']) is not int or policy['project_repository_id'] <= 0:
        raise MirrorError('dependency_format_unsupported')
    canonical_name(policy['canonical_repository'])
    if not isinstance(policy['revision'], str) or not 0 < len(policy['revision']) <= 200:
        raise MirrorError('dependency_format_unsupported')
    if not isinstance(policy['manifest_digest'], str) or not re.fullmatch('[0-9a-f]{64}', policy['manifest_digest']):
        raise MirrorError('dependency_format_unsupported')
    _identities(policy['declared_repositories'])
    _identities(policy['denied_repositories'])
    installations = policy['organization_installations']
    if not isinstance(installations, list) or len(installations) > 200:
        raise MirrorError('dependency_format_unsupported')
    for item in installations:
        if (not isinstance(item, dict) or set(item) != {'account_id', 'installation_id', 'app_id'}
                or any(type(value) is not int or value <= 0 for value in item.values())):
            raise MirrorError('dependency_format_unsupported')
    if len(__import__('json').dumps(policy)) > 1024 * 1024:
        raise MirrorError('dependency_format_unsupported')
    return deepcopy(policy)


def policy_digest(policy):
    return digest(validate_policy(policy))


def _matches(item, repo):
    return (item.get('repository_id') == repo.repository_id
            or canonical_name(item['canonical_name']) == canonical_name(repo.canonical_name))


def authorize(policy, repo, *, live_installations, live_denied=()):
    """Caller supplies independently verified current metadata, including access."""
    if any(_matches(item, repo) for item in (*policy['denied_repositories'], *live_denied)):
        raise MirrorError('project_denied')
    source = repo.repository_id == policy['project_repository_id']
    declared = any((item['repository_id'] == repo.repository_id if item.get('repository_id') is not None
                    else canonical_name(item['canonical_name']) == canonical_name(repo.canonical_name))
                   for item in policy['declared_repositories'])
    installed = {tuple(item[key] for key in ('account_id', 'installation_id', 'app_id')) for item in live_installations}
    organization = (repo.owner_type == 'Organization' and any(
        item['account_id'] == repo.owner_id
        and tuple(item[key] for key in ('account_id', 'installation_id', 'app_id')) in installed
        for item in policy['organization_installations']))
    if not (source or declared or organization):
        raise MirrorError('policy_denied')
    if repo.actual_access is not True:
        raise MirrorError('installation_access_missing')
    return 'source' if source else 'declared' if declared else 'organization'
