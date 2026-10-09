"""Publish the initial SQLite authority before a fresh host starts services."""
import os
from pathlib import Path
import shutil
import tempfile

from .db import Store, StoreError, _private, _new_file, _fsync_directory
from .runtime import selected
from .migration.snapshot import operation_lock, read_regular


def initialize(root):
    root = Path(root).absolute()
    _private(root, directory=True)
    with operation_lock(root, '.fresh-install.lock'):
        if os.path.lexists(root/'state/storage'):
            return selected(root)
        # Fresh initialization is not an import or an implicit reset. An old
        # installation must use the explicit migration protocol instead.
        allowed = {'runtimes', 'state', 'sqlite', '.fresh-install.lock', '.sqlite-runtime.lock'}
        if any(path.name not in allowed and not path.name.startswith('.fresh-sqlite-') for path in root.iterdir()):
            raise StoreError('existing_runtime', 'Use the migration CLI for an existing installation')
        state = root/'state'
        if state.exists() and any(path.name != 'storage.pending' for path in state.iterdir()):
            raise StoreError('existing_runtime', 'Use the migration CLI for an existing installation')
        destination = root/'sqlite'
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(prefix='.fresh-sqlite-', dir=root))
            try:
                store = Store.create(staging)
                with store.write() as db:
                    db.executemany('INSERT INTO store_metadata VALUES(?,?)', [
                        ('runtime_root',str(root)), ('runtime_active','true'), ('fresh_installation','true')])
                # No services can read this store until the final selection
                # marker exists. Publishing the directory preserves its WAL.
                os.rename(staging, destination)
                _fsync_directory(root)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        store = Store.open(destination)
        with store.read_snapshot() as db:
            metadata = dict(db.execute("SELECT key,value FROM store_metadata WHERE key IN ('fresh_installation','runtime_root','runtime_active')"))
            if metadata != {'fresh_installation':'true','runtime_root':str(root),'runtime_active':'true'}:
                raise StoreError('existing_runtime', 'The unpublished store belongs to another installation')
        state.mkdir(mode=0o700, exist_ok=True)
        _private(state, directory=True)
        pending = state/'storage.pending'
        content = ('forge-sqlite-v1\n'+str(destination)+'\n'+store.instance_id+'\n').encode()
        if pending.exists():
            _private(pending)
            if read_regular(pending, limit=4096) != content:
                raise StoreError('instance_mismatch', 'Pending selection belongs to another instance')
        else:
            _new_file(pending, content)
        try:
            os.link(pending, state/'storage')
        finally:
            pending.unlink()
        _fsync_directory(state)
        return selected(root)
