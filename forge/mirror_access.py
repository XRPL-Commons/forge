"""Host-only, read-only GitHub App access for the repository mirror.

Personal credentials are never accepted. Installation discovery establishes
organization identity; it does not itself authorize reading a repository.
"""
from contextlib import contextmanager

from .app_access import AccessError, REPO_PATTERN


class MirrorAccess:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        self.client = client or settings.client

    def installations(self):
        connections = self.settings.load()['connections']
        found = {}
        for connection in connections.values():
            if not connection or not connection.get('app_id') or not connection.get('private_key_env'):
                continue
            ids = connection.get('installation_ids', [connection.get('installation_id')])
            if not isinstance(ids, list) or len(ids) > 100:
                raise AccessError('Mirror installation inventory exceeds its bound')
            for installation_id in ids:
                if type(installation_id) is not int or installation_id <= 0:
                    raise AccessError('Mirror installation identity is invalid')
                key = connection['app_id'], installation_id
                if key in found:
                    continue
                selected = {key: connection[key] for key in ('app_id', 'private_key_env')}
                selected['installation_id'] = installation_id
                value = self.client.request('GET', '/app/installations/' + str(installation_id), self.client.jwt(selected))
                if (value.get('id') != installation_id or value.get('app_id') != selected['app_id']
                        or value.get('suspended_at') is not None):
                    raise AccessError('Mirror installation identity or access changed')
                account = value.get('account') or {}
                if type(account.get('id')) is not int or account.get('type') not in {'Organization', 'User'}:
                    raise AccessError('Mirror installation owner is unverified')
                if value.get('permissions', {}).get('contents') not in {'read', 'write'}:
                    continue
                found[key] = dict(connection=selected, account_id=account['id'], owner_type=account['type'])
        return list(found.values())

    def organizations(self):
        return [dict(account_id=row['account_id'], installation_id=row['connection']['installation_id'],
                     app_id=row['connection']['app_id'])
                for row in self.installations() if row['owner_type'] == 'Organization']

    @contextmanager
    def _token(self, connection, repository_id=None):
        payload = {'permissions': {'contents': 'read', 'metadata': 'read'}}
        if repository_id is not None:
            payload['repository_ids'] = [repository_id]
        value = self.client.request('POST', '/app/installations/' + str(connection['installation_id']) + '/access_tokens',
                                    self.client.jwt(connection), payload)
        token = value.get('token')
        if not isinstance(token, str) or not token:
            raise AccessError('Mirror read credential was not issued')
        try:
            permissions = value.get('permissions', {})
            if permissions.get('contents') != 'read' or any(level not in {'read', 'none'} for level in permissions.values()):
                raise AccessError('Mirror credential did not retain its read-only scope')
            yield token
        finally:
            self.client.request('DELETE', '/installation/token', token)

    @staticmethod
    def _repository(value, installation_id):
        from .mirror.policy import Repository
        owner = value.get('owner') or {}
        name = value.get('full_name')
        if (type(value.get('id')) is not int or value['id'] <= 0 or type(owner.get('id')) is not int
                or owner.get('type') not in {'Organization', 'User'}
                or not isinstance(name, str) or not REPO_PATTERN.fullmatch(name)):
            raise AccessError('Mirror repository identity is unverified')
        return Repository(repository_id=value['id'], canonical_name=name, owner_id=owner['id'],
            owner_type=owner['type'], installation_id=installation_id,
            default_branch=value.get('default_branch') or 'main', actual_access=True)

    def resolve(self, canonical_name):
        if not isinstance(canonical_name, str) or not REPO_PATTERN.fullmatch(canonical_name):
            raise AccessError('Mirror repository name is invalid')
        # Each request revalidates configured installations and actual repository
        # access. A cached Git object never substitutes for current authorization.
        for row in self.installations():
            value = None
            with self._token(row['connection']) as token:
                try:
                    value = self.client.request('GET', '/repos/' + canonical_name, token)
                except AccessError as error:
                    if error.status not in {403, 404}:
                        raise
            if value is not None:
                if value.get('private') is False:
                    return self._repository(value, None)
                if (value.get('owner') or {}).get('id') != row['account_id']:
                    raise AccessError('Mirror repository moved outside its installation owner')
                return self._repository(value, row['connection']['installation_id'])
        # Public declarations remain usable without an App credential. The
        # broker still applies pinned dependency/organization policy and denies.
        value = self.client.request('GET', '/repos/' + canonical_name, None)
        if value.get('private') is not False:
            raise AccessError('Mirror repository is not accessible')
        return self._repository(value, None)

    @contextmanager
    def credentials(self, repository):
        # Resolve again before a host fetch: name reuse/transfer cannot reuse a
        # previously admitted numeric repository identity or installation grant.
        current = self.resolve(repository.canonical_name)
        if (current.repository_id != repository.repository_id or current.owner_id != repository.owner_id
                or current.installation_id != repository.installation_id):
            raise AccessError('Mirror repository access changed before fetch')
        if current.installation_id is None:
            yield None
            return
        matches = [row for row in self.installations() if row['connection']['installation_id'] == current.installation_id]
        if len(matches) != 1:
            raise AccessError('Mirror repository installation is no longer configured')
        with self._token(matches[0]['connection'], current.repository_id) as token:
            yield token
