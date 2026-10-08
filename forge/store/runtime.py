"""Explicit runtime selection; an activated instance never falls back to JSON.

The private, write-once storage marker locates the database and binds its instance
identity. Mutable configuration, progress and authorization live in SQLite.
Only the qualified maintenance cutover may publish this marker.
"""
import os
from pathlib import Path
import stat
import threading

from .db import Store, StoreError

_stores = {}
_lock = threading.RLock()


def instance_root(root):
    root = Path(root).absolute()
    return root.parent if root.name == 'setup' else root


def selected(root):
    root = instance_root(root)
    marker = root / 'state/storage'
    if not os.path.lexists(marker):
        return None
    info = marker.lstat()
    if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_size > 4096):
        raise StoreError('runtime_selection_invalid', 'Runtime storage selection is not a private regular file')
    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        actual = os.fstat(stream.fileno())
        if (actual.st_dev, actual.st_ino) != (info.st_dev, info.st_ino):
            raise StoreError('runtime_selection_changed', 'Runtime storage selection changed during the read')
        value = stream.read(4097).splitlines()
    if (len(value) != 3 or value[0] != 'forge-sqlite-v1'
            or not Path(value[1]).is_absolute() or not value[2] or len(value[2]) > 200):
        raise StoreError('runtime_selection_invalid', 'Runtime storage selection is malformed')
    identity = (str(marker), info.st_dev, info.st_ino, info.st_mtime_ns, tuple(value))
    with _lock:
        store = _stores.get(identity)
        if store is None:
            store = Store.open(value[1])
            if store.instance_id != value[2]:
                raise StoreError('instance_mismatch', 'Selected database does not belong to this instance')
            with store.read_snapshot() as db:
                metadata = dict(db.execute("SELECT key,value FROM store_metadata WHERE key IN ('import_only','runtime_root','runtime_active')"))
            if (metadata.get('import_only') == 'true' or metadata.get('runtime_active') != 'true'
                    or metadata.get('runtime_root') != str(root)):
                raise StoreError('runtime_inactive', 'Database has not completed activation for this installation')
            _stores[identity] = store
        return store


def require_legacy(path):
    """Refuse legacy store access below a selected installation, even from old adapters."""
    path = Path(path).absolute()
    for parent in (path, *path.parents):
        if os.path.lexists(parent / 'state/storage'):
            raise StoreError('legacy_runtime_disabled', 'This installation uses SQLite; legacy state access is disabled')


def writable(db):
    if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
        raise StoreError('runtime_inactive', 'Staging imports cannot accept runtime changes')


def control_enabled(root, name):
    markers = {'factory_active': 'factory-v2.enabled', 'onboarding_active': 'factory-onboarding.enabled',
               'factory_paused': 'factory-paused.json', 'harness_login_enabled': 'harness-login-enabled.json'}
    if name not in markers:
        raise ValueError('Unknown runtime activation control')
    store = selected(root)
    if store is not None:
        with store.read_snapshot() as db:
            row = db.execute('SELECT enabled FROM runtime_controls WHERE name=?', (name,)).fetchone()
            return bool(row and row[0])
    return (instance_root(root) / 'state' / markers[name]).exists()
