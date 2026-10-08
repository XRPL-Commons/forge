"""Hash-bound private source snapshots; capture requires all writers quiescent."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import time

from ..db import _fsync_directory, _new_file

MAX_SOURCE = 512 * 1024 * 1024


class MigrationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def decode(raw):
    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise MigrationError('duplicate_json_key')
            result[key] = value
        return result
    def invalid(value):
        raise MigrationError('nonfinite_json_number')
    try:
        return json.loads(raw, object_pairs_hook=pairs, parse_constant=invalid)
    except (ValueError, UnicodeError) as error:
        raise MigrationError('invalid_source_json') from error


def safe_name(name):
    if (not isinstance(name, str) or not name or PurePosixPath(name).is_absolute()
            or any(p in ('', '.', '..') for p in name.split('/')) or '\\' in name):
        raise MigrationError('unsafe_source_path')
    return name


def read_regular(path, *, limit=MAX_SOURCE):
    # Validate every ancestor as well: O_NOFOLLOW alone covers only the final path.
    path = Path(path).absolute()
    if any(parent.is_symlink() for parent in (path, *path.parents)):
        raise MigrationError('symlink_source')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise MigrationError('unsafe_source_file')
        with os.fdopen(fd, 'rb', closefd=False) as stream:
            raw = stream.read(limit + 1)
        after = os.fstat(fd)
        if len(raw) > limit or (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise MigrationError('source_changed')
        return raw
    finally:
        os.close(fd)


def domain(name):
    if name.startswith('state/observer/'):
        return 'independent_observer'
    if name.startswith('state/operator-auth/'):
        return 'operator_auth'
    if name.startswith('state/client-operations/'):
        return 'client_operations'
    if name.startswith('state/factory/'):
        return 'factory'
    if name.startswith('state/issues/'):
        return 'issues'
    if name.startswith('state/notifications/'):
        return 'notifications'
    if name.startswith('state/github-access/'):
        return 'repository_access'
    if name.startswith('state/onboarding/') or name.startswith('setup/'):
        return 'setup'
    if name in ('factory.json', 'runtime.json', 'host-runtime.json', 'host.json'):
        return 'configuration'
    if name.startswith('state/environment-'):
        return 'environment'
    if name.startswith('state/deployments.'):
        return 'deployments'
    if name.endswith('-deployment.json') or name.startswith('state/deploy-'):
        return 'diagnostic'
    return 'unclassified'


def sources(root):
    root = Path(root).absolute()
    if root.is_symlink():
        raise MigrationError('symlink_source')
    result = {}
    for directory in ('state', 'setup'):
        base = root / directory
        if base.exists():
            for current, directories, files in os.walk(base, followlinks=False):
                if any((Path(current)/d).is_symlink() for d in directories):
                    raise MigrationError('symlink_source')
                for name in files:
                    path = Path(current) / name
                    # Locks are IPC, SQLite files belong to the new backend, and
                    # interrupted atomic-write temporaries are not authoritative.
                    if name.endswith('.lock') or name.startswith('.pending-') or name.startswith('forge.sqlite3'):
                        continue
                    if path.suffix in ('.json', '.jsonl', '.initialized', '.enabled'):
                        relative = path.relative_to(root).as_posix()
                        result[relative] = read_regular(path)
    for name in ('factory.json', 'runtime.json', 'host-runtime.json', 'host.json'):
        if (root/name).exists():
            result[name] = read_regular(root/name)
    for name in result:
        if name.endswith('.initialized') and name.removesuffix('.initialized')+'.json' not in result:
            raise MigrationError('initialized_source_missing')
    if 'state/factory/factory-ledger.json' not in result:
        raise MigrationError('ledger_missing')
    return result


def uncleaned_auxiliary(data):
    """Count every known auxiliary execution owner, including setup/reset probes."""
    jobs = []
    def value(name):
        return decode(data[name]) if name in data else {}
    jobs.extend(row.get('job') for row in value('state/deployments.json').get('repositories',{}).values())
    jobs.extend(row.get('job') for row in value('state/issues/discussions.json').values())
    setup = value('state/onboarding/setup.json')
    jobs.extend(setup.get('probe_jobs',[]))
    jobs.extend((setup.get('operation') or {}).get('jobs',[]))
    jobs.append(value('state/discussion-qualification.json').get('job'))
    return sum(1 for job in jobs if isinstance(job,dict) and not job.get('cleanup',False))


@contextmanager
def operation_lock(root, name='.migration.lock'):
    path = Path(root)/name
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError('migration_busy') from None
        yield
    finally:
        os.close(fd)


def capture(root, destination, *, source_revision, storage_generation, quiescent):
    """Caller owns the maintenance barrier for the entire call, including fsync.

    quiescent is a trusted host-adapter callback, not a user-supplied boolean.
    A two-pass digest check detects changes; it is not a substitute for the barrier.
    """
    destination = Path(destination).absolute()
    root = Path(root).absolute()
    if destination == root or root in destination.parents:
        raise MigrationError('snapshot_inside_source')
    if not isinstance(source_revision, str) or not source_revision or type(storage_generation) is not int or storage_generation < 1:
        raise MigrationError('invalid_snapshot_identity')
    with operation_lock(root):
        quiescent()
        data = sources(root)
        from ...factory.ledger import Ledger
        ledger = decode(data['state/factory/factory-ledger.json'])
        Ledger.validate(ledger)
        if ledger['slots'] or any(not j['cleanup'] for j in ledger['jobs'].values()) or uncleaned_auxiliary(data):
            raise MigrationError('uncleaned_jobs')
        destination.mkdir(mode=0o700)  # Explicitly new; never overwrite an earlier capture.
        os.chmod(destination, 0o700)
        entries = []
        for index, (name, raw) in enumerate(sorted(data.items())):
            filename = f'{index:06d}.source'
            _new_file(destination/filename, raw)
            entries.append(dict(name=name, file=filename, domain=domain(name), size=len(raw), digest=digest(raw)))
        quiescent()
        second = sources(root)
        if set(second) != set(data) or any(digest(second[n]) != digest(raw) for n, raw in data.items()):
            raise MigrationError('source_changed')
        manifest = dict(format=1, source_revision=source_revision, storage_generation=storage_generation,
                        captured_at=time.time(), sources=entries)
        raw = encode(manifest)
        _new_file(destination/'manifest.json', raw)
        _new_file(destination/'sealed', digest(raw).encode())
        _fsync_directory(destination)
        _fsync_directory(destination.parent)
    return Snapshot(destination)


class Snapshot:
    def __init__(self, directory):
        self.root = Path(directory).absolute()
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700 or info.st_uid != os.geteuid():
            raise MigrationError('snapshot_not_private')
        raw = read_regular(self.root/'manifest.json', limit=4*1024*1024)
        self.id = digest(raw)
        if read_regular(self.root/'sealed', limit=64) != self.id.encode():
            raise MigrationError('snapshot_unsealed')
        self.manifest = decode(raw)
        if self.manifest.get('format') != 1:
            raise MigrationError('snapshot_version')
        self.entries = {}
        files = set()
        for row in self.manifest['sources']:
            name, filename = safe_name(row['name']), safe_name(row['file'])
            if '/' in filename or name in self.entries or filename in files or row['domain'] != domain(name):
                raise MigrationError('invalid_manifest')
            self.entries[name] = row
            files.add(filename)
        for name in self.entries:
            self.read(name)

    def read(self, name):
        row = self.entries[name]
        raw = read_regular(self.root/row['file'])
        if len(raw) != row['size'] or digest(raw) != row['digest']:
            raise MigrationError('snapshot_changed')
        return raw

    def json(self, name, default=None):
        return decode(self.read(name)) if name in self.entries else default
