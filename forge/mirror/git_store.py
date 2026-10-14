"""Bare, host-owned Git cache. No checkout, scripts, filters or worker config.

Only this module updates objects. Serving uses git-http-backend's smart upload
protocol with all write and static-file paths disabled. The trusted credential
provider yields a repository-scoped read token for the duration of one refresh.
"""
from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import hashlib
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time

from .policy import MirrorError, OID, canonical_name

MAX_REQUEST = 8 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024
MAX_REPOSITORY = 512 * 1024 * 1024


@dataclass(frozen=True)
class BaseObjects:
    repository_id: int
    source_input_digest: str
    object_format: str
    pinned_oids: tuple
    git_dir: Path


def environment():
    return {'PATH': '/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin', 'HOME': '/nonexistent',
            'LANG': 'C', 'LC_ALL': 'C', 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null',
            'GIT_TERMINAL_PROMPT': '0', 'GIT_NO_REPLACE_OBJECTS': '1', 'GIT_ATTR_NOSYSTEM': '1',
            'GIT_CONFIG_COUNT': '5', 'GIT_CONFIG_KEY_0': 'core.hooksPath', 'GIT_CONFIG_VALUE_0': '/dev/null',
            'GIT_CONFIG_KEY_1': 'protocol.file.allow', 'GIT_CONFIG_VALUE_1': 'never',
            'GIT_CONFIG_KEY_2': 'protocol.ext.allow', 'GIT_CONFIG_VALUE_2': 'never',
            'GIT_CONFIG_KEY_3': 'http.followRedirects', 'GIT_CONFIG_VALUE_3': 'false',
            'GIT_CONFIG_KEY_4': 'maintenance.auto', 'GIT_CONFIG_VALUE_4': 'false'}


def _disk_size(path):
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())


def run_git(args, *, env=None, data=b'', limit=MAX_RESPONSE, timeout=120, alive=lambda: True, disk=None):
    """Bound buffered CGI bytes and disk growth; cancel child process group."""
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as error:
        incoming.write(data)
        incoming.seek(0)
        try:
            process = subprocess.Popen(['git', *args], stdin=incoming, stdout=output, stderr=error,
                                       env=env or environment(), start_new_session=True)
        except OSError:
            raise MirrorError('mirror_unavailable', transient=True) from None
        started = time.monotonic()
        reason = None
        try:
            while True:
                if not alive():
                    reason = 'session_revoked'
                elif output.tell() > limit or error.tell() > 65536 or disk is not None and _disk_size(disk) > MAX_REPOSITORY:
                    reason = 'mirror_capacity'
                elif time.monotonic() - started > timeout:
                    reason = 'mirror_timeout'
                if reason:
                    raise MirrorError(reason, transient=reason == 'mirror_timeout')
                if process.poll() is not None:
                    break
                time.sleep(.05)
            if process.returncode:
                # Neither stderr nor command environment is safe to persist.
                raise MirrorError('mirror_fetch_failed', transient=True)
            output.seek(0)
            result = output.read(limit + 1)
            if len(result) > limit:
                raise MirrorError('mirror_capacity')
            return result
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class GitStore:
    def __init__(self, root, *, credentials, refresh_seconds=30, max_cache_bytes=4 * 1024 ** 3):
        self.root = Path(root)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.stat().st_uid != os.geteuid() or self.root.stat().st_mode & 0o077:
            raise MirrorError('mirror_storage_invalid')
        self.credentials = credentials
        self.refresh_seconds = refresh_seconds
        self.max_cache_bytes = max_cache_bytes

    def path(self, repository_id):
        if type(repository_id) is not int or repository_id <= 0:
            raise MirrorError('repository_unavailable')
        return self.root / f'{repository_id}.git'

    @contextmanager
    def locked(self, repository_id, *, alive=lambda: True):
        path = self.root / f'{repository_id}.lock'
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            start = time.monotonic()
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not alive():
                        raise MirrorError('session_revoked')
                    if time.monotonic() - start > 120:
                        raise MirrorError('mirror_capacity')
                    time.sleep(.05)
            yield self.path(repository_id)
        finally:
            os.close(descriptor)

    @contextmanager
    def _authenticated(self, repository):
        with self.credentials(repository) as token:
            env = environment()
            if token:
                # A host-only askpass script reads a private environment value.
                # No token in argv, Git URL, mirror config or command diagnostics.
                with tempfile.TemporaryDirectory(prefix='forge-mirror-auth-') as directory:
                    script = Path(directory) / 'askpass'
                    script.write_text('#!/bin/sh\ncase "$1" in *Username*) printf "%s\\n" x-access-token;; *) printf "%s\\n" "$FORGE_MIRROR_TOKEN";; esac\n')
                    script.chmod(0o700)
                    env.update(GIT_ASKPASS=str(script), FORGE_MIRROR_TOKEN=token)
                    yield env
            else:
                yield env

    def ensure(self, repository, *, alive=lambda: True, pinned_oids=()):
        canonical_name(repository.canonical_name)
        if any(not OID.fullmatch(oid) for oid in pinned_oids):
            raise MirrorError('revision_unavailable')
        with self.locked(repository.repository_id, alive=alive) as path:
            existed = path.exists()
            if not existed:
                if _disk_size(self.root) >= self.max_cache_bytes:
                    raise MirrorError('mirror_capacity')
                run_git(['init', '--bare', str(path)], alive=alive)
                for key, value in [('http.receivepack', 'false'), ('http.getanyfile', 'false'),
                                   ('http.uploadpack', 'true'), ('gc.auto', '0'), ('core.hooksPath', '/dev/null')]:
                    run_git(['--git-dir', str(path), 'config', key, value], alive=alive)
                (path / 'git-daemon-export-ok').touch(mode=0o600)
            if path.is_symlink():
                raise MirrorError('mirror_storage_invalid')
            refreshed = path / 'forge-refreshed'
            fetch = not refreshed.exists() or time.time() - refreshed.stat().st_mtime >= self.refresh_seconds
            # Required pinned commits might no longer be reachable from live refs.
            missing = []
            for oid in pinned_oids:
                try:
                    run_git(['--git-dir', str(path), 'cat-file', '-e', oid + '^{commit}'], alive=alive)
                except MirrorError:
                    missing.append(oid)
            if fetch or missing:
                with self._authenticated(repository) as env:
                    url = 'https://github.com/' + canonical_name(repository.canonical_name) + '.git'
                    refs = ['+refs/heads/*:refs/heads/*', '+refs/tags/*:refs/tags/*']
                    if fetch:
                        run_git(['--git-dir', str(path), 'fetch', '--atomic', '--no-recurse-submodules', '--no-write-fetch-head',
                                 url, *refs], env=env, alive=alive, disk=path)
                    for oid in missing:
                        run_git(['--git-dir', str(path), 'fetch', '--no-recurse-submodules', '--no-write-fetch-head', url, oid],
                                env=env, alive=alive, disk=path)
                    refreshed.touch(mode=0o600)
                branch = repository.default_branch
                if not isinstance(branch, str) or not branch or branch.startswith('-'):
                    raise MirrorError('repository_unavailable')
                run_git(['check-ref-format', 'refs/heads/' + branch], alive=alive)
                run_git(['--git-dir', str(path), 'symbolic-ref', 'HEAD', 'refs/heads/' + branch], alive=alive)
            for oid in pinned_oids:
                run_git(['--git-dir', str(path), 'cat-file', '-e', oid + '^{commit}'], alive=alive)
                run_git(['--git-dir', str(path), 'update-ref', 'refs/forge/pins/' + oid, oid], alive=alive)
            if _disk_size(self.root) > self.max_cache_bytes:
                raise MirrorError('mirror_capacity')
            return {'cache': existed, 'host_fetch': fetch or bool(missing)}

    def http(self, repository, kind, *, body=b'', protocol='', alive=lambda: True):
        if kind not in ('advertisement', 'upload') or len(body) > MAX_REQUEST or protocol not in ('', 'version=1', 'version=2'):
            raise MirrorError('request_invalid')
        with self.locked(repository.repository_id, alive=alive) as path:
            env = environment()
            env.update(GIT_PROJECT_ROOT=str(self.root), REQUEST_METHOD='GET' if kind == 'advertisement' else 'POST',
                       PATH_INFO=f'/{repository.repository_id}.git/' + ('info/refs' if kind == 'advertisement' else 'git-upload-pack'),
                       QUERY_STRING='service=git-upload-pack' if kind == 'advertisement' else '',
                       CONTENT_TYPE='application/x-git-upload-pack-request' if kind == 'upload' else '',
                       CONTENT_LENGTH=str(len(body)), GIT_PROTOCOL=protocol,
                       SERVER_PROTOCOL='HTTP/1.1', REMOTE_ADDR='127.0.0.1')
            result = run_git(['http-backend'], env=env, data=body, alive=alive)
            header, separator, payload = result.partition(b'\r\n\r\n')
            if not separator or len(header) > 8192:
                raise MirrorError('mirror_protocol_failed', transient=True)
            headers = {}
            status = 200
            for line in header.decode('ascii').split('\r\n'):
                key, _, value = line.partition(':')
                if key.lower() == 'status':
                    status = int(value.strip().split(' ', 1)[0])
                elif key.lower() in {'content-type', 'cache-control', 'expires', 'pragma'}:
                    headers[key.lower()] = value.strip()
            if status != 200:
                raise MirrorError('revision_unavailable')
            return headers, payload

    @contextmanager
    def base_objects(self, repository, pinned_oids, source_input_digest, *, alive=lambda: True):
        self.ensure(repository, alive=alive, pinned_oids=pinned_oids)
        with self.locked(repository.repository_id, alive=alive) as path:
            object_format = run_git(['--git-dir', str(path), 'rev-parse', '--show-object-format']).decode().strip()
            yield BaseObjects(repository.repository_id, source_input_digest, object_format, tuple(pinned_oids), path)

    def reader(self, repository_id):
        return TreeReader(self.path(repository_id))


class TreeReader:
    def __init__(self, path):
        self.path = path

    def list_tree(self, commit):
        if not OID.fullmatch(commit):
            raise MirrorError('revision_unavailable')
        raw = run_git(['--git-dir', str(self.path), 'ls-tree', '-r', '-z', commit], limit=4 * 1024 * 1024)
        result = []
        for row in raw.split(b'\0'):
            if not row:
                continue
            metadata, path = row.split(b'\t', 1)
            mode, kind, oid = metadata.decode('ascii').split()
            result.append({'path': path.decode('utf-8'), 'mode': mode, 'oid': oid})
        return result

    def read_blob(self, oid):
        if not OID.fullmatch(oid):
            raise MirrorError('revision_unavailable')
        size = int(run_git(['--git-dir', str(self.path), 'cat-file', '-s', oid]))
        if size > 4 * 1024 * 1024:
            raise MirrorError('mirror_capacity')
        return run_git(['--git-dir', str(self.path), 'cat-file', 'blob', oid], limit=4 * 1024 * 1024)
