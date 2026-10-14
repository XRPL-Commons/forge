"""One-time rebase of a store built by the 41 squashed migrations onto 001_baseline.

The 41 migration files were replaced by `schema/001_baseline.sql` (#289). A
store whose history is exactly those 41 files, checksum for checksum, is rebased
inside the `forge migrate upgrade` transaction: the objects the baseline left
out are removed, the result must match a new baseline store structurally, and
the history collapses to the single baseline row. Anything else is refused and
the transaction rolls back, leaving the store at 041.

The next release deletes this module once the installation is rebased.
"""
import time

from .db import StoreError
from .schema_shape import compare

# The applied history this release can rebase: (version, sha256 of the file).
# The files themselves are kept only as the test fixture tests/fixtures/schema_v41.
LEGACY_HISTORY = (
    (1, 'fe17d4daa4e681092d969c68d582a374d491d3e3525e6f8107c3596d29f8a370'),
    (2, '8578c65d9e5961acc36f33d79da3d08d995db3bd16c6e1088ce19504e3441c68'),
    (3, 'b4f94c53494b4e0a5bb0787b7a06be0ebf37831c40ffd8cf26894f79a68101a1'),
    (4, '792884f0acc663d26eef171e9eed7a03aa6b131bf406161e9ede641673e7c81a'),
    (5, '23db8f63c17036488a0eaf1f7ad8b043eef0380eaabbb5950399793c8bb240a0'),
    (6, 'b98af98bb79c9fda922b046208e736f876e3eab599a4d29496c3a54be6c989e0'),
    (7, 'f7502d373d43f9897a7786a31ab83537ba79c21ad7a4221bb0b5446f42d5c7b1'),
    (8, 'b707cc9c2da312b7ab75b23b96c40248e39bfe508ddb8d917142b7236db6cd03'),
    (9, '91cb77b31e0553c4313ac8691de10716f55ad6059ccc13e77ac6ecca877577ea'),
    (10, 'b17b8d933ad621c90c8031066ad95105d6fffd9847cd56f7a9a05eab3066ac48'),
    (11, 'ef4b074935e05ee3e1ad8767e591db1c32e77549b3ff7238316d1f6b120399e9'),
    (12, '3c888d42151f55fce10e286e1d62bd5ce9fc3fbb7650c690548d0292a17a1402'),
    (13, '16332734fbe8ed6fcb59d275340b9723936ca3fa14dd9295dd2069065a1f69e8'),
    (14, '192e2a3fef331645afb64f34dbb73aa3ee11f7bcc1f5bb3589c658683cc844aa'),
    (15, 'b11fcdcb7b0107af40d5d6304b29f674a9af6cd3a59c4f9b5569a8201597e7c0'),
    (16, 'cdd7d65170a1abfdc24a9c7abb8becd6787cb0a25c47a17077ea4c8766fac9a4'),
    (17, 'a03eea8f67442f9fe81e85f70f34b31cf214aa9c6a9e76a0b069b21de365b808'),
    (18, '304f4d0c5c3e802ae978df8a110528d43d9d9631a204c87ed8aabf2892465b6c'),
    (19, '6f37b995dfe9d9a4c6650a0719cca903d718d1b8bbda66d09e3e15b88826e4c8'),
    (20, '1995c7f5fa1d9b2bc0eaa53c35e36fb703773b0ffafc873b1984a0bc6bf599df'),
    (21, '0445b7a48a10216a209df133e1d798f995dc875b5e29eb6cad880ea289c3f3b4'),
    (22, 'c5697159136a8cecc44d4ea238a0851b9bbbf23989c7a4c46bd636330fc68f06'),
    (23, '79f97487df4b5e083cdd035be3fcab59cc69e3c0fa49dd72c80ea06d519c5b25'),
    (24, 'd9760fdd3774b2176e5dff251ec81b4239dcb9f5b48107dc22e0ac73ecea7c69'),
    (25, '122132b6b00ab208bcb68d9bd3180186805b76b05156fac5d1c115c2d48674e6'),
    (26, '99de243922959cfc98b7f34ad3d9d03846be9b732086d0519f7a21b1329bf451'),
    (27, '49621086d9f8adc4abd717ad3c0a123e4990e299a413ffca5d40c26f82276ea9'),
    (28, '7ff58e5277bc236dafd3542c32d5a1b9eac3523305a2c95f3869a7287c8d03e3'),
    (29, '776354c66dc5d7c50297d0c4b760fd4e74068dfb55e1835e7a96dcd1eea91277'),
    (30, '5ff92aedced008ba97306eae066c29d0708fe7aad5f67b0d2ab03b61fad53b24'),
    (31, '45599df7e342629afc4c78707a9a5bb18b1a575a87dbcf9d357c693c84185617'),
    (32, 'b70356e6d2b879375f5a7a6756c4b62dd22ad059aacd6344c8f1c2e485a31e4b'),
    (33, '25d6b2634e110cab91b0bb74a35f5c02c613fc5c8dff8c0074efadfafc26f783'),
    (34, 'e1ac52360b3c8c99a9b2583e3b00350cc6e63b4405a2bd8fd3fb4ce66fa95109'),
    (35, 'ceac2550bc2cae2b8153c57218b3f144e02dc7667a19238591ba7a345237b4fd'),
    (36, '1aaa1a2ab2d263a189c529edc5f7ca4e8147ed8d38c0e6be66645cb755b35406'),
    (37, '4073906e501b4039e5d5f8cb3b75156cddf5042c90df7c6772d9e3075fd736c6'),
    (38, 'e640be76a0627fbfcaed433ad44ba294fa23df75dfb7e3a695b8b1eda393f1bd'),
    (39, 'ef85be8bf0d0615aea8a1a8cefc2a6f4b52acadd964fb4c2c7e28923fa290b78'),
    (40, '9fab74b81f37fe35e22e9fc81a4d411466d4a88dcebf9ebf6652502d2504644c'),
    (41, '5c7c22811c9d9ea7abc986d5ad402afcdc642c95b4d3265060b5cbc396dbe570'),
)

# Objects the baseline does not contain. Children are dropped before parents;
# dropping a table also drops its indexes and triggers.
DROPPED_TABLES = (
    # The JSON import archive and the provenance of migrated investigations.
    'import_records', 'import_documents', 'import_findings', 'import_progress', 'import_sources',
    'migration_recovery_adoptions', 'import_runs',
    # Imported operator requests (closed by migration 041).
    'imported_operator_requests',
    # The notification outbox, whose sender was removed.
    'notification_deliveries', 'notification_outbox',
    # Tables no code has ever read or written since the SQLite runtime started.
    'check_requests', 'inventory_scan_issues', 'observation_requests', 'runtime_operations', 'setup_operations',
)

# Gate codes no code has ever opened. A row that still referenced one would fail
# the foreign-key check and roll the rebase back.
RETIRED_GATE_CODES = ('capacity', 'changes_requested', 'merge_conflict')

REWRITE = """
DROP TRIGGER recovery_assignment_valid;
CREATE TRIGGER recovery_assignment_valid BEFORE INSERT ON recovery_assignments
WHEN NOT EXISTS (SELECT 1 FROM jobs r JOIN jobs f ON f.id=NEW.failed_job_id
 JOIN attempt_failures a ON a.attempt_id=f.attempt_id
 WHERE r.id=NEW.job_id AND r.role='recovery' AND r.unit_id=f.unit_id
 AND f.role!='recovery' AND f.cleanup=1 AND f.outcome IN ('failure','rejected')
 AND a.attempt_id=NEW.failed_attempt_id AND a.recovery_required=1)
BEGIN SELECT RAISE(ABORT,'recovery must own a cleaned confirmed failure'); END;
DROP INDEX unit_completion_events;
CREATE INDEX unit_completion_events ON events(unit_id,sequence DESC,at)
WHERE kind IN ('merged','closed','dissolved');
DROP TRIGGER gate_codes_no_delete;
DELETE FROM gate_codes WHERE code IN ({codes});
CREATE TRIGGER gate_codes_no_delete BEFORE DELETE ON gate_codes BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
"""


def legacy(rows):
    """Whether an applied history is exactly the frozen one this release rebases."""
    return [tuple(row) for row in rows] == list(LEGACY_HISTORY)


def retire(db):
    """Remove and rewrite what the baseline left out. No checks; see rebase()."""
    from .db import _execute_script
    for table in DROPPED_TABLES:
        db.execute(f'DROP TABLE {table}')
    _execute_script(db, REWRITE.format(codes=','.join(f"'{code}'" for code in RETIRED_GATE_CODES)))


def rebase(db, baseline):
    """Within the caller's write transaction, turn the 041 store into the baseline.

    `baseline` is the catalog's (1, checksum, script). Raises on any difference.
    """
    version, checksum, script = baseline
    if version != 1:
        raise StoreError('schema_invalid', 'The baseline must be migration 1')
    rows = db.execute('SELECT version,checksum FROM schema_migrations ORDER BY version').fetchall()
    if not legacy(rows):
        raise StoreError('schema_checksum', 'Only the frozen 041 history can be rebased onto the baseline')
    retire(db)
    found = compare(db, script)
    if found:
        raise StoreError('schema_rebase_mismatch', 'The rebased store differs from the baseline: ' + '; '.join(found[:20]))
    db.execute('DELETE FROM schema_migrations')
    db.execute('INSERT INTO schema_migrations VALUES(?,?,?)', (version, checksum, time.time()))
    db.execute("UPDATE store_metadata SET value=? WHERE key='schema_version'", (str(version),))
