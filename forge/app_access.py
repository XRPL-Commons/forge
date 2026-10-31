"""GitHub App authentication and bounded, read-only installation discovery.

Only installation-token creation is a POST. No repository settings are changed.
Secrets remain in memory and are never included in persisted observations.
"""
import base64
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


ROLE_PERMISSIONS = {
    'build': {'contents': 'write', 'issues': 'write', 'pull_requests': 'write'},
    'review': {'contents': 'read', 'issues': 'read', 'pull_requests': 'write', 'checks': 'write'},
    'audit': {'contents': 'read', 'issues': 'read', 'pull_requests': 'write', 'checks': 'write'},
    'controller': {'contents': 'write', 'issues': 'write', 'pull_requests': 'write', 'checks': 'write', 'administration': 'read'},
}
# Recommended, never required: a role must keep minting tokens on an installation
# that predates the grant, so callers ask for these only when they need them.
OPTIONAL_PERMISSIONS = {'controller': {'deployments': 'read'}}
RANK = {'none': 0, 'read': 1, 'write': 2, 'admin': 3}


def _union(*grants):
    result = {}
    for grant in grants:
        for key, level in grant.items():
            if RANK[level] > RANK[result.get(key, 'none')]:
                result[key] = level
    return result


# One Forge App holds every role's grant. It is a credential boundary, not a
# judge: review and audit verdicts are still produced by separate executors and
# every token stays narrowed to one role's permissions when it is minted.
APP_PERMISSIONS = _union(*ROLE_PERMISSIONS.values())
APP_OPTIONAL_PERMISSIONS = _union(*OPTIONAL_PERMISSIONS.values())
REPO_PATTERN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]*/(?!\.{1,2}$)[A-Za-z0-9_.-]+')


class AccessError(ValueError):
    """An intentionally credential-free error suitable for the settings UI."""
    def __init__(self, message, *, status=None, plan_required=False):
        super().__init__(message)
        self.status = status
        self.plan_required = plan_required


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _missing(wanted, permissions):
    return [f'{key}: {level}' for key, level in wanted.items()
            if RANK.get(permissions.get(key), 0) < RANK[level]]


def missing_permissions(role, permissions):
    return _missing(ROLE_PERMISSIONS[role], permissions)


def missing_app_permissions(permissions):
    """Grants the single Forge App lacks before every role can point at it."""
    return _missing(APP_PERMISSIONS, permissions)


def shared_app(connections):
    """The connection every role uses when all of them point at one App, else None."""
    values = list(connections.values())
    if len(values) != len(ROLE_PERMISSIONS) or set(connections) != set(ROLE_PERMISSIONS):
        return None
    return values[0] if all(value == values[0] for value in values) else None


def recommended_permissions(role, permissions):
    """Missing optional grants: a recommendation on the access page, never a block."""
    return _missing(OPTIONAL_PERMISSIONS.get(role, {}), permissions)


class AppClient:
    def __init__(self, *, environ=None, run=subprocess.run, clock=time.time, request=None):
        self.environ = os.environ if environ is None else environ
        self.run, self.clock = run, clock
        self.request = request or self._request

    def _request(self, method, path, token, payload=None):
        if not path.startswith('/') or path.startswith('//'):
            raise AccessError('Invalid GitHub API path')
        data = None if payload is None else json.dumps(payload).encode()
        request = Request('https://api.github.com' + path, data=data, method=method,
                          headers={**({'Authorization': 'Bearer ' + token} if token else {}),
                                   'Accept': 'application/vnd.github+json',
                                   'X-GitHub-Api-Version': '2022-11-28',
                                   'User-Agent': 'forge-settings', 'Content-Type': 'application/json'})
        from .request_metrics import request_attempt, deadline, RequestBudgetExhausted
        with request_attempt() as (timeout, receipt):
            try:
                with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                    receipt.update(status=response.status, headers=response.headers)
                    raw = response.read(4 * 1024 * 1024 + 1)
                    if len(raw) > 4 * 1024 * 1024:
                        raise AccessError('GitHub response exceeded the size limit')
                    return json.loads(raw) if raw else None
            except HTTPError as error:
                receipt.update(status=error.code, headers=error.headers)
                # Retain only an allowlisted plan signal, never the response body.
                plan_required = False
                if error.code == 403:
                    try:
                        body = json.loads(error.read(16385))
                        message = body.get('message') if isinstance(body, dict) else None
                        plan_required = message in (
                            'Upgrade to GitHub Pro or make this repository public to enable this feature.',
                            'Upgrade to GitHub Pro or make this repository public to enable this feature',
                            'Upgrade to GitHub Team or make this repository public to enable this feature.',
                        )
                    except (ValueError, OSError):
                        pass
                raise AccessError(f'GitHub returned HTTP {error.code}; check installation access',
                                  status=error.code, plan_required=plan_required) from None
            except (URLError, TimeoutError, OSError):
                if time.monotonic() >= deadline():
                    raise RequestBudgetExhausted('GitHub request budget exhausted') from None
                raise AccessError('GitHub access could not be verified; retry the sync') from None
            except ValueError:
                raise AccessError('GitHub access could not be verified; retry the sync') from None

    def jwt(self, connection):
        key_path = self.environ.get(connection['private_key_env'], '')
        if not key_path or not Path(key_path).is_absolute():
            raise AccessError('Private key reference is not configured on the server')
        def encode(value):
            return base64.urlsafe_b64encode(value).rstrip(b'=')
        now = int(self.clock())
        message = b'.'.join(encode(json.dumps(value, separators=(',', ':')).encode()) for value in
                            ({'alg': 'RS256', 'typ': 'JWT'},
                             {'iat': now - 60, 'exp': now + 540, 'iss': str(connection['app_id'])}))
        try:
            result = self.run(['openssl', 'dgst', '-sha256', '-sign', key_path],
                              input=message, capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            raise AccessError('App authentication failed; check the server key reference') from None
        if result.returncode or not result.stdout:
            raise AccessError('App authentication failed; check the server key reference')
        return (message + b'.' + encode(result.stdout)).decode()

    def discover(self, connection):
        if 'installation_ids' not in connection:
            return self._discover_one(connection)
        installations = [self._discover_one({'app_id': connection['app_id'], 'installation_id': identifier,
                                             'private_key_env': connection['private_key_env']})
                         for identifier in connection['installation_ids']]
        repositories = []
        for identifier, installation in zip(connection['installation_ids'], installations):
            repositories.extend({**r, 'installation_id': identifier} for r in installation['repositories'])
        if len({r['id'] for r in repositories}) != len(repositories):
            raise AccessError('A repository appears in multiple installations; refresh access')
        levels = {'none': 0, 'read': 1, 'write': 2, 'admin': 3}
        names = set().union(*(set(i['permissions']) for i in installations))
        permissions = {name: min((i['permissions'].get(name, 'none') for i in installations), key=levels.get) for name in names}
        return {**installations[0], 'repositories': repositories, 'permissions': permissions,
                'account': ', '.join(i['account'] for i in installations),
                'installation_urls': [i['installation_url'] for i in installations]}

    def _discover_one(self, connection):
        deadline = time.monotonic() + 90
        def request(method, path, token, payload=None):
            if time.monotonic() >= deadline:
                raise AccessError('Access sync deadline reached; retry the sync')
            return self.request(method, path, token, payload)
        jwt = self.jwt(connection)
        app = request('GET', '/app', jwt)
        if not isinstance(app, dict) or app.get('id') != connection['app_id']:
            raise AccessError('Authenticated App does not match the configured App ID')
        installation = request('GET', f"/app/installations/{connection['installation_id']}", jwt)
        if (not isinstance(installation, dict) or installation.get('id') != connection['installation_id']
                or installation.get('app_id') != connection['app_id']):
            raise AccessError('Installation does not match the configured App')
        if installation.get('suspended_at'):
            raise AccessError('GitHub App installation is suspended')
        permissions = installation.get('permissions')
        account = installation.get('account') or {}
        login, slug = account.get('login'), app.get('slug')
        if (not isinstance(permissions, dict)
                or any(not isinstance(k, str) or v not in ('read', 'write', 'admin') for k, v in permissions.items())
                or not isinstance(login, str)
                or not re.fullmatch(r'[A-Za-z0-9_.-]+', login)
                or not isinstance(slug, str) or not re.fullmatch(r'[A-Za-z0-9-]+', slug)):
            raise AccessError('GitHub installation response is incomplete')
        # This token is deliberately read-only. Installation grants, rather than
        # this reduced token, establish each role's configured capabilities.
        minted = request('POST', f"/app/installations/{connection['installation_id']}/access_tokens",
                              jwt, {'permissions': {'metadata': 'read'}})
        token = minted.get('token') if isinstance(minted, dict) else None
        if not isinstance(token, str) or not token:
            raise AccessError('GitHub did not return an installation token')
        repositories, seen, seen_names, expected_count = [], set(), set(), None
        for page in range(1, 101):
            data = request('GET', f'/installation/repositories?per_page=100&page={page}', token)
            rows = data.get('repositories') if isinstance(data, dict) else None
            count = data.get('total_count') if isinstance(data, dict) else None
            if not isinstance(rows, list) or type(count) is not int or count < 0 or len(rows) > 100:
                raise AccessError('Repository inventory response is incomplete')
            if expected_count is not None and expected_count != count:
                raise AccessError('Repository inventory changed during sync; refresh access')
            expected_count = count
            for row in rows:
                if (not isinstance(row, dict) or type(row.get('id')) is not int
                        or row['id'] <= 0 or not isinstance(row.get('full_name'), str)
                        or not REPO_PATTERN.fullmatch(row['full_name']) or row['id'] in seen
                        or row['full_name'].lower() in seen_names):
                    raise AccessError('Repository inventory is inconsistent; refresh access')
                seen.add(row['id'])
                seen_names.add(row['full_name'].lower())
                repositories.append({'id': row['id'], 'name': row['full_name'],
                                     'archived': row.get('archived') is True,
                                     'disabled': row.get('disabled') is True})
            if len(rows) < 100:
                break
        else:
            raise AccessError('Repository inventory exceeded the pagination limit')
        if len(repositories) != expected_count:
            raise AccessError('Repository inventory is incomplete; refresh access')
        return {'app_slug': slug, 'account': login, 'permissions': permissions,
                'installation_url': (f'https://github.com/organizations/{login}/settings/installations/'
                                     if account.get('type') == 'Organization' else
                                     'https://github.com/settings/installations/') + str(connection['installation_id']),
                'repositories': repositories}
