"""Bounded local GitHub broker; GitHub credentials stay inside the gh process."""
import json
import os
import selectors
import shutil
import subprocess
import time

from .contracts import NAME, SetupError, baseline, positive, require

LIMIT = 4 * 1024 * 1024


def run_bounded(argv, *, payload=None, environ=None, timeout=30):
    """Drain both pipes with a total cap. Never echo a child's diagnostics."""
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if k in {'PATH', 'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'GH_CONFIG_DIR',
                    'GH_TOKEN', 'GITHUB_TOKEN', 'LANG', 'LC_ALL', 'TMPDIR',
                    'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'}}
    env.update(GH_HOST='github.com', GH_PROMPT_DISABLED='1', GH_PAGER='cat', GH_DEBUG='')
    data = None if payload is None else json.dumps(payload, allow_nan=False).encode()
    require(data is None or len(data) <= 65536)
    process = None
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if data is not None else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        # Payloads are small fixed ruleset objects; still multiplex stdin so a
        # blocked child cannot outlive the operation deadline.
        output = bytearray()
        total = 0
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            selector.register(process.stderr, selectors.EVENT_READ, 'stderr')
            if data is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            offset = 0
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SetupError('github_timeout', 'GitHub request timed out; re-read state before retrying a write.')
                for key, _ in selector.select(min(remaining, 1)):
                    if key.data == 'stdin':
                        offset += os.write(key.fileobj.fileno(), data[offset:offset + 4096])
                        if offset == len(data):
                            selector.unregister(key.fileobj); key.fileobj.close()
                        continue
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj); key.fileobj.close(); continue
                    total += len(chunk)
                    if total > LIMIT:
                        raise SetupError('github_response_too_large', 'GitHub response exceeded the size limit.')
                    if key.data == 'stdout':
                        output.extend(chunk)
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
        if code:
            raise SetupError('github_request_failed', 'GitHub request failed; check local login, permissions and the requested operation.')
        return bytes(output)
    except (OSError, subprocess.SubprocessError):
        raise SetupError('github_unavailable', 'The local GitHub CLI could not complete the request.') from None
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()


class GitHub:
    def __init__(self, *, run=run_bounded, executable=None):
        self.run = run
        self.executable = executable or shutil.which('gh')
        require(bool(self.executable), 'github_cli_missing', 'Install the GitHub CLI and sign in locally.')

    def _request(self, method, path, payload=None):
        # Only this module constructs paths. Do not offer a public arbitrary-API command.
        argv = [self.executable, 'api', '--hostname', 'github.com', '--method', method,
                '-H', 'Accept: application/vnd.github+json', '-H', 'X-GitHub-Api-Version: 2022-11-28', path]
        if payload is not None:
            argv.extend(['--input', '-'])
        raw = self.run(argv, payload=payload)
        if len(raw) > LIMIT:
            raise SetupError('github_response_too_large', 'GitHub response exceeded the size limit.')
        try:
            return json.loads(raw) if raw else None
        except (ValueError, UnicodeError):
            raise SetupError('github_invalid_response', 'GitHub returned an unreadable response.') from None

    def repository(self, name):
        require(isinstance(name, str) and len(name) <= 200 and NAME.fullmatch(name))
        value = self._request('GET', '/repos/' + name)
        require(isinstance(value, dict), 'github_invalid_response')
        owner = value.get('owner') or {}
        full_name = value.get('full_name')
        require(positive(value.get('id')) and positive(owner.get('id'))
                and isinstance(full_name, str) and NAME.fullmatch(full_name)
                and full_name.lower() == name.lower()
                and owner.get('type') in ('User', 'Organization'), 'repository_identity_changed',
                'GitHub repository identity changed; resolve the intended repository again.')
        branch = value.get('default_branch')
        baseline(branch)  # Validate exact branch matching before considering rule writes.
        require(value.get('archived') is False and value.get('disabled') is False,
                'repository_unavailable', 'The repository is archived or disabled.')
        permissions = value.get('permissions') or {}
        require(isinstance(permissions, dict), 'github_invalid_response')
        return {'id': value['id'], 'name': full_name, 'owner_id': owner['id'],
                'owner_type': owner['type'], 'branch': branch, 'admin': permissions.get('admin') is True}

    def rulesets(self, name):
        require(isinstance(name, str) and NAME.fullmatch(name))
        result = []
        for page in range(1, 11):
            values = self._request('GET', f'/repos/{name}/rulesets?includes_parents=true&per_page=100&page={page}')
            require(isinstance(values, list) and len(values) <= 100, 'github_invalid_response')
            for value in values:
                require(isinstance(value, dict) and positive(value.get('id'))
                        and isinstance(value.get('name'), str)
                        and value.get('source_type') in ('Repository', 'Organization', 'Enterprise'),
                        'github_invalid_response')
                result.append({'id': value['id'], 'name': value['name'], 'source_type': value['source_type']})
            if len(values) < 100:
                return result
        raise SetupError('github_inventory_limit', 'Too many rulesets to plan safely.')

    def create_baseline(self, name, branch):
        require(isinstance(name, str) and NAME.fullmatch(name))
        value = self._request('POST', '/repos/' + name + '/rulesets', baseline(branch))
        require(isinstance(value, dict) and positive(value.get('id')), 'github_write_unconfirmed',
                'The rule write is unconfirmed; inspect GitHub before retrying.')
        return {'id': value['id']}

    def grant_repository(self, installation_id, repository_id):
        require(positive(installation_id) and positive(repository_id))
        # GitHub supports this operation only with a suitable local classic PAT.
        # Caller handles a refused grant as a browser handoff, never token upload.
        self._request('PUT', f'/user/installations/{installation_id}/repositories/{repository_id}')

    @staticmethod
    def installation_url(target, installation_id):
        require(positive(installation_id))
        name = target.get('name')
        require(isinstance(name, str) and NAME.fullmatch(name)
                and target.get('owner_type') in ('Organization', 'User'))
        prefix = 'organizations/' + name.split('/')[0] + '/' if target['owner_type'] == 'Organization' else ''
        return f'https://github.com/{prefix}settings/installations/{installation_id}'
