"""Explicit atomic schema upgrades. Normal runtime readers never migrate a store."""
from contextlib import contextmanager
import time

from . import baseline_rebase, db as storage


@contextmanager
def connection(store, *, write=False):
    storage._qualify()
    store._paths()
    db = store._connect_path(store.path, read_only=not write)
    try:
        yield db
    finally:
        db.close()


def validate_prefix(store, db):
    """Return (catalog, applied count, legacy) for a store this release can upgrade.

    `legacy` marks the frozen 041 history, which `apply` first rebases onto the
    baseline (migration 1); its applied count is then 0 against this catalog.
    """
    # Maintenance constructs this handle from the pinned target release. Use
    # the same catalog for prefix validation, application and final validation.
    migrations = store._migration_catalog
    rows = [(row[0], row[1]) for row in db.execute('SELECT version,checksum FROM schema_migrations ORDER BY version')]
    legacy = baseline_rebase.legacy(rows)
    if not legacy:
        if not rows or len(rows) > len(migrations):
            raise storage.StoreError('schema_newer' if rows else 'schema_incomplete', 'Cannot upgrade this schema')
        if rows != [(version, checksum) for version, checksum, _ in migrations[:len(rows)]]:
            raise storage.StoreError('schema_checksum', 'Applied migration checksums do not match this release')
    metadata = dict(db.execute("SELECT key,value FROM store_metadata WHERE key IN ('instance_id','schema_version')"))
    if metadata.get('schema_version') != str(len(rows)):
        raise storage.StoreError('schema_incomplete', 'Schema metadata does not match its history')
    if metadata.get('instance_id') != store._anchor() or store.instance_id not in (None, metadata['instance_id']):
        raise storage.StoreError('instance_mismatch', 'Store identity changed')
    store.instance_id = metadata['instance_id']
    return migrations, 0 if legacy else len(rows), legacy


def inspect(store):
    with connection(store) as db:
        migrations, current, legacy = validate_prefix(store, db)
        if legacy:
            # Rebasing onto the baseline is the first pending step.
            return {'current_schema': len(baseline_rebase.LEGACY_HISTORY), 'target_schema': len(migrations),
                    'legacy_history': True, 'pending_migrations': [version for version, _, _ in migrations]}
        return {'current_schema': current, 'target_schema': len(migrations),
                'pending_migrations': [version for version, _, _ in migrations[current:]]}


def apply(store):
    """Caller has stopped all writers; the entire forward change commits once."""
    with connection(store, write=True) as db:
        try:
            db.execute('BEGIN IMMEDIATE')
            migrations, current, legacy = validate_prefix(store, db)
            if legacy:
                baseline_rebase.rebase(db, migrations[0])
                current = 1
            for version, checksum, script in migrations[current:]:
                storage._execute_script(db, script)
                db.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (version, checksum, time.time()))
            db.execute("UPDATE store_metadata SET value=? WHERE key='schema_version'", (str(len(migrations)),))
            if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or db.execute('PRAGMA foreign_key_check').fetchone():
                raise storage.StoreError('schema_upgrade_invalid', 'Upgraded store failed integrity validation')
            store._validate(db)
            db.commit()
        except BaseException:
            db.rollback()
            raise
    return inspect(store)
