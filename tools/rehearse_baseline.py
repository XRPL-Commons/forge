#!/usr/bin/env python3
"""Rehearse the one-time baseline rebase (#289) on a copy of a store database.

    python3 tools/rehearse_baseline.py PATH_TO_DB_COPY

The given file is only read: the rebase runs on a private temporary copy that is
deleted afterwards. The report shows the history check, the rows that leave with
the dropped tables, the structural comparison with a new baseline store, the
integrity checks and the row counts of every kept table before and after.
Exit status 0 means `forge migrate upgrade` would rebase this store.
"""
from pathlib import Path
import os
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forge.store import baseline_rebase  # noqa: E402
from forge.store.db import StoreError, _migrations  # noqa: E402
from forge.store.remote_time import register  # noqa: E402
from forge.store.schema_shape import compare  # noqa: E402

# Codes kept because stores may still reference them; shown for a later cleanup.
KEPT_LEGACY = (
    ('events of kind legacy_event', "SELECT count(*) FROM events WHERE kind='legacy_event'"),
    ('gates legacy_reconciliation (open)', "SELECT count(*) FROM unit_gates WHERE code='legacy_reconciliation' AND resolved_at IS NULL"),
    ('gates legacy_reconciliation (all)', "SELECT count(*) FROM unit_gates WHERE code='legacy_reconciliation'"),
    ('gates retry_delay (open)', "SELECT count(*) FROM unit_gates WHERE code='retry_delay' AND resolved_at IS NULL"),
    ('gates retry_delay (all)', "SELECT count(*) FROM unit_gates WHERE code='retry_delay'"),
    ('gates merge_checks (open)', "SELECT count(*) FROM unit_gates WHERE code='merge_checks' AND resolved_at IS NULL"),
    ('gates merge_checks (all)', "SELECT count(*) FROM unit_gates WHERE code='merge_checks'"),
)


def connect(path):
    db = sqlite3.connect(Path(path).as_uri() + '?mode=rw', uri=True, isolation_level=None)
    register(db)
    db.execute('PRAGMA foreign_keys=ON')
    return db


def counts(db):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0] for table in tables}


def rehearse(source):
    ok = True
    baseline = _migrations()[0]
    work = Path(tempfile.mkdtemp(prefix='forge-rehearsal-'))
    try:
        copy = work / 'store.sqlite3'
        reader = sqlite3.connect(Path(source).absolute().as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(copy)
        reader.backup(target)
        reader.close()
        target.close()
        db = connect(copy)
        rows = db.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()
        print(f'History: {len(rows)} applied migrations; matches the frozen 041 manifest: {baseline_rebase.legacy(rows)}')
        if not baseline_rebase.legacy(rows):
            print('REFUSED: forge migrate upgrade will not rebase this store.')
            return False
        print('Schema version metadata:', db.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()[0])
        before = counts(db)
        print('\nRows removed with the dropped tables:')
        for table in baseline_rebase.DROPPED_TABLES:
            print(f'  {table}: {before.get(table, "absent")}')
        interrupted = db.execute(
            "SELECT count(*) FROM imported_operator_requests p JOIN issue_action_requests q ON q.id=p.request_id "
            "JOIN issues i ON i.id=q.issue_id WHERE p.interrupted_at IS NOT NULL AND i.github_state='open'").fetchone()[0]
        print(f'  open issues no longer held back by an interrupted imported request: {interrupted}')
        print('\nReferences to retired gate codes (must be 0):')
        for code in baseline_rebase.RETIRED_GATE_CODES:
            found = db.execute('SELECT count(*) FROM unit_gates WHERE code=?', (code,)).fetchone()[0]
            ok &= found == 0
            print(f'  {code}: {found}')
        print('\nKept legacy codes still referenced (informational):')
        for label, sql in KEPT_LEGACY:
            print(f'  {label}: {db.execute(sql).fetchone()[0]}')

        # 1. Full structural comparison, reported in full.
        db.execute('BEGIN IMMEDIATE')
        try:
            baseline_rebase.retire(db)
            found = compare(db, baseline[2])
        finally:
            db.execute('ROLLBACK')
        print('\nStructural comparison with a new baseline store:', 'equal' if not found else f'{len(found)} differences')
        for line in found:
            print('  ' + line)
        ok &= not found

        # 2. The exact upgrade path: rebase, then the checks apply() runs before commit.
        db.execute('BEGIN IMMEDIATE')
        try:
            baseline_rebase.rebase(db, baseline)
            integrity = db.execute('PRAGMA integrity_check').fetchall()
            foreign = db.execute('PRAGMA foreign_key_check').fetchall()
            db.execute('COMMIT')
        except (StoreError, sqlite3.DatabaseError) as error:
            db.execute('ROLLBACK')
            print(f'\nREBASE FAILED and rolled back: {getattr(error, "code", type(error).__name__)}: {error}')
            return False
        print('\nintegrity_check:', ', '.join(row[0] for row in integrity))
        print('foreign_key_check:', 'ok' if not foreign else f'{len(foreign)} violations')
        ok &= [row[0] for row in integrity] == ['ok'] and not foreign
        history = db.execute('SELECT version,checksum FROM schema_migrations').fetchall()
        version = db.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()[0]
        print(f'History after rebase: {history}; schema_version={version}')
        ok &= history == [(1, baseline[1])] and version == '1'

        after = counts(db)
        changed = {table: (before[table], after[table]) for table in after if before.get(table) != after[table]}
        changed.pop('schema_migrations', None)
        retired = len(baseline_rebase.RETIRED_GATE_CODES)
        if changed.get('gate_codes') == (before['gate_codes'], before['gate_codes'] - retired):
            changed.pop('gate_codes')  # The retired codes, and only those.
        print('\nKept tables:', len(after), '; row counts changed:', changed or 'none')
        ok &= not changed
        db.close()
    finally:
        shutil.rmtree(work)
    print('\nRESULT:', 'the rebase would succeed' if ok else 'the rebase would NOT succeed')
    return ok


if __name__ == '__main__':
    if len(sys.argv) != 2 or not os.path.isfile(sys.argv[1]):
        sys.exit(__doc__)
    sys.exit(0 if rehearse(sys.argv[1]) else 1)
