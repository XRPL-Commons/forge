"""Shared fixture: a selected SQLite store under a runtime root."""
from pathlib import Path

from forge.store.db import Store


def selected_store(root, instance_id=None):
    store = Store.create(root, instance_id)
    with store.write() as db:
        db.executemany('INSERT INTO store_metadata VALUES(?,?)', [('runtime_root', str(root)), ('runtime_active', 'true')])
    marker = Path(root) / 'state/storage'
    marker.write_text('forge-sqlite-v1\n' + str(store.root) + '\n' + store.instance_id + '\n')
    marker.chmod(0o600)
    return store
