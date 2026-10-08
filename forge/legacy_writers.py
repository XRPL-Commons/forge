"""Cross-process writer barrier for the one-way legacy storage cutover.

Operational JSON writers take a shared lease before changing state. Maintenance
owns the exclusive lease from source capture through SQLite marker publication.
The durable journal keeps writers fenced if maintenance crashes between stages.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import threading

_held = threading.local()
_CONFIG = {'factory.json', 'runtime.json', 'host-runtime.json', 'host.json'}


def operational_root(path):
    path = Path(path).absolute()
    for parent in path.parents:
        if parent.name == 'state':
            root = parent.parent
            return root.parent if root.name == 'setup' else root
        if parent.name == 'setup':
            return parent.parent
    return path.parent if path.name in _CONFIG else None


def _check(root, *, allow_drain):
    from .store.runtime import require_legacy
    from .store.migration.snapshot import MigrationError, decode, read_regular
    require_legacy(root)
    journal = root / '.migration-cutover.json'
    if os.path.lexists(journal):
        stage = decode(read_regular(journal, limit=1048576)).get('stage')
        if stage != 'complete' and not (allow_drain and stage == 'draining'):
            raise MigrationError('cutover_maintenance_in_progress')


def _drain_path(path, root):
    relative = Path(path).absolute().relative_to(root).as_posix()
    # These paths admit new operator/configuration authority, never cleanup.
    return not (relative in _CONFIG or relative.startswith(('setup/', 'state/operator-auth/',
        'state/client-connections/', 'state/client-operations/')) or relative == 'state/factory-paused.json')


@contextmanager
def mutation(path, *, allow_drain=None):
    root = operational_root(path)
    if root is None:
        # Independent diagnostic files and private credential copies are not
        # operational JSON stores; the selected runtime may still write them.
        yield
        return
    if allow_drain is None:
        allow_drain = _drain_path(path, root)
    held = getattr(_held, 'roots', {})
    key = str(root)
    if key in held:
        yield
        return
    _check(root, allow_drain=allow_drain)
    root.mkdir(parents=True, exist_ok=True)
    fd = os.open(root / '.runtime-writers.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            from .store.migration.snapshot import MigrationError
            raise MigrationError('cutover_maintenance_in_progress') from None
        _check(root, allow_drain=allow_drain)
        _held.roots = {**held, key: True}
        try:
            yield
        finally:
            _held.roots = held
    finally:
        os.close(fd)


@contextmanager
def exclusive(root):
    from .store.migration.snapshot import MigrationError
    root = Path(root).absolute()
    fd = os.open(root / '.runtime-writers.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise MigrationError('cutover_writers_busy') from None
        yield
    finally:
        os.close(fd)
