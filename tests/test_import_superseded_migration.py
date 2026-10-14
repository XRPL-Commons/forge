"""Migration 041 retires imported deliveries and leaves every other row alone."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge.store import db as storage, upgrade
from forge.store.models import Command
from forge.store.runtime import selected
from forge.store.transitions import Lifecycle, issue_state, project_unit
from tests.test_sqlite_runtime_support import RuntimeFixture

ROOT = Path(__file__).resolve().parents[1]
LEDGER = 'state/factory/factory-ledger.json'
# Tables the repair may change. Everything else must be byte-identical.
REPAIRED = {'units', 'unit_gates', 'unit_issues', 'events', 'attempt_failures', 'issue_action_requests',
            'close_codes', 'schema_migrations', 'store_metadata'}


def migration_041():
    version, _, script = storage._migrations()[40]
    assert version == 41
    return script


def admit(life, unit, number):
    life.admit_issue(request_id='admit-' + unit, actor_id='operator', unit_id=unit, issue_id=f'1:{number}',
                     repository_id=1, repository_name='owner/repo', issue_number=number, title='Issue',
                     body='Body', requirements='required', observed_at=90)


def version(store, unit):
    with store.read_snapshot() as db:
        return project_unit(db, unit)['version']


def seed(root):
    """A schema-40 store as production has it: one imported delivery still waiting
    on legacy_reconciliation, a native live delivery, and an ended imported one."""
    with patch('forge.store.db._migrations', return_value=storage._migrations()[:40]):
        store = storage.Store.create(root)
        life = Lifecycle(store, clock=lambda: 100)
        admit(life, 'imported', 585)
        life.open_gate(Command('legacy-gate', 'importer', 'imported', version(store, 'imported')), 'legacy_reconciliation',
                       details={'source': LEDGER, 'issue_key': '1:585', 'legacy_state': 'hold'})
        life.pause(Command('legacy-pause', 'importer', 'imported', version(store, 'imported')))
        admit(life, 'native', 2)
        life.pause(Command('native-pause', 'operator', 'native', version(store, 'native')))
        admit(life, 'ended', 3)
        life.close_unit(Command('ended-close', 'operator', 'ended', version(store, 'ended')),
                        close_code='operator_closed', evidence={'approved': True})
        with store.write() as db:
            db.execute("UPDATE issues SET labels_json=? WHERE id='1:585'", (json.dumps(['forge:hold', 'hold']),))
            member = db.execute("SELECT id FROM unit_issues WHERE unit_id='ended'").fetchone()[0]
            for attempt, manifest, investigation in (
                    ('imported-attempt', {'legacy_job_id': 'imported-attempt', 'source_manifest': 'manifest'}, None),
                    ('native-attempt', {'manifest_version': 2}, 'pending')):
                db.execute('INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,'
                           "input_manifest_json,budget_generation,created_at) VALUES(?,'ended',?,1,1,?,1,95)",
                           (attempt, member, json.dumps(manifest)))
                db.execute("INSERT INTO attempt_failures(attempt_id,unit_id,position,investigation,evidence_kind,at) "
                           "VALUES(?,'ended',(SELECT COUNT(*) FROM attempt_failures),?,'failure_evidence',95)",
                           (attempt, investigation))
            for request, imported in (('imported-request', True), ('native-request', False)):
                db.execute("INSERT INTO issue_action_requests(id,issue_id,action,expected_revision,status,reason,created_at) "
                           "VALUES(?,'1:2','resume','revision','pending','operator',96)", (request,))
                if imported:
                    db.execute("INSERT INTO imported_operator_requests(request_id,source_manifest,source_status) "
                               "VALUES(?,'manifest','pending')", (request,))
                else:
                    db.execute("INSERT INTO operator_action_authority(request_id,actor_id,unit_id,expected_issue_version,"
                               "payload_digest,updated_at) VALUES(?,'operator','native',1,'digest',96)", (request,))
    return store


def dump(db, *, skip=()):
    tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    return {table: sorted(map(tuple, db.execute(f'SELECT * FROM "{table}"'))) for table in tables if table not in skip}


class ImportSupersededMigrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_imported_delivery_closes_and_everything_else_is_untouched(self):
        store = seed(self.root)
        with store.read_snapshot() as db:
            before = dump(db)
            native = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE {column} IN ('native','1:2') ORDER BY rowid")]
                      for table, column in (('units', 'id'), ('unit_gates', 'unit_id'), ('unit_issues', 'unit_id'),
                                            ('events', 'unit_id'), ('issues', 'id'))}
            imported_issue = tuple(db.execute("SELECT * FROM issues WHERE id='1:585'").fetchone())
            events = db.execute('SELECT MAX(sequence) FROM events').fetchone()[0]
        upgrade.apply(storage.Store(self.root))
        store = storage.Store.open(self.root)
        with store.read_snapshot() as db:
            after = dump(db)
            for table in set(before) - REPAIRED:
                self.assertEqual(after[table], before[table], table)
            self.assertEqual({table: [tuple(row) for row in db.execute(f"SELECT * FROM {table} WHERE {column} IN ('native','1:2') ORDER BY rowid")]
                              for table, column in (('units', 'id'), ('unit_gates', 'unit_id'), ('unit_issues', 'unit_id'),
                                                    ('events', 'unit_id'), ('issues', 'id'))}, native)
            unit = db.execute("SELECT * FROM units WHERE id='imported'").fetchone()
            self.assertEqual((unit['state'], unit['close_code'], unit['primary_gate_id']), ('closed', 'import_superseded', None))
            gates = db.execute("SELECT code,resolution FROM unit_gates WHERE unit_id='imported' ORDER BY code").fetchall()
            self.assertEqual([tuple(row) for row in gates],
                             [('legacy_reconciliation', 'superseded'), ('operator_pause', 'superseded')])
            member = db.execute("SELECT * FROM unit_issues WHERE unit_id='imported'").fetchone()
            self.assertEqual((member['state'], member['exit_reason']), ('removed', 'closed'))
            self.assertIsNotNone(member['left_at'])
            # GitHub facts, labels included, are not touched: the hold stays.
            self.assertEqual(tuple(db.execute("SELECT * FROM issues WHERE id='1:585'").fetchone()), imported_issue)
            added = [(row['kind'], json.loads(row['payload_json'])) for row in
                     db.execute("SELECT kind,payload_json FROM events WHERE sequence>? ORDER BY sequence", (events,))]
            self.assertEqual([kind for kind, _ in added], ['gate_resolved', 'gate_resolved', 'membership_changed', 'closed'])
            self.assertTrue(all(payload['evidence']['import_superseded'] is True for kind, payload in added if kind == 'gate_resolved'))
            self.assertEqual(added[-1][1], {'close_code': 'import_superseded', 'merge_sha': None})
            failures = dict(db.execute('SELECT attempt_id,investigation FROM attempt_failures'))
            self.assertEqual(failures, {'imported-attempt': 'complete', 'native-attempt': 'pending'})
            self.assertEqual(dict(db.execute('SELECT id,status FROM issue_action_requests')),
                             {'imported-request': 'rejected', 'native-request': 'pending'})
            self.assertEqual(db.execute("SELECT issue_outcome FROM close_codes WHERE code='import_superseded'").fetchone()[0], None)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            issue = dict(db.execute("SELECT * FROM issues WHERE id='1:585'").fetchone())
            self.assertEqual(issue_state(issue, dict(unit)), 'untracked')
        # The open issue starts again as a new delivery; the closed one stays closed.
        Lifecycle(store, clock=lambda: 200).admit_known_issue(request_id='again', actor_id='supervisor:discovery',
            unit_id='fresh', issue_id='1:585', expected_issue_version=issue['version'])
        with store.read_snapshot() as db:
            self.assertEqual(project_unit(db, 'fresh')['state'], 'queued')
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='imported'").fetchone()[0], 'closed')

    def test_second_run_of_the_repair_changes_nothing(self):
        seed(self.root)
        store = storage.Store(self.root)
        upgrade.apply(store)
        with store.read_snapshot() as db:
            first = dump(db)
        with upgrade.connection(store, write=True) as db:
            db.execute('BEGIN IMMEDIATE')
            storage._execute_script(db, migration_041())
            db.commit()
        with store.read_snapshot() as db:
            self.assertEqual(dump(db), first)

    def test_fresh_store_applies_the_repair_without_rows(self):
        store = storage.Store.create(self.root)
        with store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM units").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT value FROM store_metadata WHERE key='schema_version'").fetchone()[0], '41')


class SupersededDiscoveryTests(RuntimeFixture):
    """Discovery admits a superseded import again; an ordinary closed delivery stays closed."""

    def test_discovery_readmits_only_the_superseded_import(self):
        from unittest.mock import Mock
        from forge.factory.config import validate
        from forge.factory.store_inventory import DiscoveryController
        self.prepare()
        config = validate({'enabled': True, 'image': 'sha256:' + 'a' * 64})
        remote = dict(number=1, title='Ready work', body='Implement this', state='open',
                      updated_at='2026-10-08T10:00:00Z', labels=['ready'], assignees=[])
        github = Mock()
        github.call.return_value = [remote]
        github.issue.return_value = remote, {'acceptance': ['complete requirement']}
        github.pulls.return_value = []
        github.target.return_value = 'main'
        github.protections.return_value = True
        github.deadline = float('inf')
        discovery = DiscoveryController(self.database, self.settings, github, config=config, clock=lambda: self.now)
        first = discovery.cycle()['items'][0]
        with self.database.read_snapshot() as db:
            unit = db.execute('SELECT id,version FROM units').fetchone()
        Lifecycle(self.database, clock=lambda: self.now).open_gate(Command('legacy', 'importer', unit['id'], unit['version']),
            'legacy_reconciliation', details={'source': LEDGER})
        self.assertEqual(discovery.cycle()['items'], [])
        with self.database.write() as db:
            storage._execute_script(db, migration_041())
        again = discovery.cycle()['items']
        self.assertEqual([item['issue_id'] for item in again], [first['issue_id']])
        with self.database.read_snapshot() as db:
            rows = dict(db.execute('SELECT id,COALESCE(close_code,state) FROM units'))
        self.assertEqual(sorted(rows.values()), ['import_superseded', 'queued'])
        fresh = next(identifier for identifier, state in rows.items() if state == 'queued')
        with self.database.read_snapshot() as db:
            version = db.execute('SELECT version FROM units WHERE id=?', (fresh,)).fetchone()[0]
        Lifecycle(self.database, clock=lambda: self.now).close_unit(Command('stop', 'operator', fresh, version),
            close_code='operator_closed', evidence={'approved': True})
        self.assertEqual(discovery.cycle()['items'], [])


class PendingMigrationLaunchTests(unittest.TestCase):
    """Regression for #283: the launcher starts the schema upgrade while 041 is pending."""

    def test_launcher_starts_the_upgrade_host_and_the_upgrade_applies_041(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            store = seed(root / 'storage')
            (root / 'state').mkdir(mode=0o700)
            storage._new_file(root / 'state/storage',
                              ('forge-sqlite-v1\n' + str(store.root) + '\n' + store.instance_id + '\n').encode())
            with store.write() as db:
                db.executemany('INSERT INTO store_metadata VALUES(?,?)', [('runtime_root', str(root)), ('runtime_active', 'true')])
            with self.assertRaises(storage.StoreError) as refused:
                selected(root)
            self.assertEqual(refused.exception.code, 'schema_incomplete')

            def launch():
                return subprocess.run([sys.executable, '-m', 'forge.launch', 'forge.store.migration.host', '--root', str(root)],
                                      cwd=ROOT, capture_output=True, text=True, timeout=120,
                                      env={key: value for key, value in os.environ.items() if key != 'FORGE_ROOT'})
            started = launch()
            self.assertEqual(started.returncode, 0, started.stderr)

            from forge.store.migration.upgrade import Upgrade
            active = ['forge-supervisor.service']
            def command(*args):
                if 'stop' in args:
                    active.clear()
                if 'start' in args:
                    active[:] = args[3:]
                return ''
            host = SimpleNamespace(root=root, source=root / 'source', identity=lambda: 'b' * 40,
                                   units=lambda: list(active), prepare_source_update=lambda *args, **kwargs: None,
                                   install_source_update=lambda previous, target: None, command=command,
                                   run=lambda *args, **kwargs: SimpleNamespace(returncode=0), quiet=lambda: None)
            with patch('forge.store.migration.upgrade.package_version', return_value='tool'):
                upgrader = Upgrade(host)
                self.assertEqual(upgrader.status()['pending_migrations'], [41])
                result = upgrader.run(revision='b' * 40, apply=True)
            self.assertEqual((result['status'], result['current_schema'], result['pending_migrations']), ('upgraded', 41, []))
            with selected(root).read_snapshot() as db:
                self.assertEqual(db.execute("SELECT close_code FROM units WHERE id='imported'").fetchone()[0], 'import_superseded')
            self.assertEqual(launch().returncode, 0)


if __name__ == '__main__':
    unittest.main()
