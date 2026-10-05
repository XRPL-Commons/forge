"""Release gate for the phase 1 SQLite store (#209): constraints, transitions,
concurrency, crash recovery, permissions and the code registry."""
import itertools
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from forge.store import (IllegalTransition, InvariantViolation, StaleVersion, Store, StoreBusy, StoreError, codes,
                         schema, status)
from forge.store.store import ANCHOR, DATABASE

ROOT = Path(__file__).resolve().parents[1]
DAY = 86400


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = self.open()
        self.repo = self.store.ensure_repository('example/repo', 1)
        self.numbers = itertools.count(1)
        self.heads = itertools.count(1)

    def open(self, **kwargs):
        store = Store(self.root, create=True, **kwargs)
        self.addCleanup(store.close)
        return store

    def issue(self, state='open'):
        return self.store.observe_issue(self.repo, next(self.numbers), state=state, title='Title', body='Body',
                                        requirements={'goal': 'x'}, remote_updated_at='2026-10-01T00:00:00Z',
                                        generation=1)['issue_id']

    def unit(self, members=1):
        """A queued unit; extra members are joined directly, as a combine would."""
        unit_id = self.store.admit(self.issue(), actor='test')['unit_id']
        for _ in range(members - 1):
            self.join(unit_id, self.issue())
        return unit_id

    def join(self, unit_id, issue_id):
        with self.store.write() as db:
            issue = db.execute('SELECT * FROM issues WHERE id = ?', (issue_id,)).fetchone()
            db.execute("INSERT INTO unit_issues (id, unit_id, issue_id, repository_id, base_branch,"
                       " requirements_revision_id, state, joined_at) VALUES (?, ?, ?, ?, 'main', ?, 'pending', 1)",
                       (f'm-{issue_id}', unit_id, issue_id, issue['repository_id'], issue['current_revision_id']))

    def version(self, unit_id):
        return self.store.unit(unit_id)['version']

    def candidate(self):
        return {'pull_number': 7, 'branch': 'forge/unit', 'head_oid': f'h{next(self.heads)}', 'base_oid': 'base',
                'policy_hash': 'policy', 'requirements_hash': 'req', 'input_manifest_hash': 'manifest'}

    def step(self, unit_id, to_state, **kwargs):
        v = self.version(unit_id)
        common = dict(expected_version=v, actor='test', **kwargs)
        if to_state == 'building':
            return self.store.build(unit_id, **common)
        if to_state == 'integrating':
            return self.store.integrate(unit_id, **common)
        if to_state == 'reviewing':
            return self.store.review(unit_id, candidate=self.candidate(), **common)
        if to_state == 'auditing':
            return self.store.audit(unit_id, **common)
        if to_state == 'merging':
            return self.store.merge(unit_id, **common)
        if to_state == 'merged':
            return self.store.merged(unit_id, merge_sha='m' * 40, **common)
        if to_state == 'closed':
            return self.store.close_unit(unit_id, close_code='operator_closed', **common)
        if to_state == 'dissolved':
            return self.store.dissolve(unit_id, **common)
        raise AssertionError(to_state)

    PATHS = {
        'queued': (),
        'building': ('building',),
        'integrating': ('building', 'integrating'),
        'reviewing': ('building', 'integrating', 'reviewing'),
        'auditing': ('building', 'integrating', 'reviewing', 'auditing'),
        'merging': ('building', 'integrating', 'reviewing', 'auditing', 'merging'),
        'merged': ('building', 'integrating', 'reviewing', 'auditing', 'merging', 'merged'),
        'closed': ('closed',),
        'dissolved': ('building', 'dissolved'),
    }

    def unit_at(self, state, members=2):
        unit_id = self.unit(members)
        for target in self.PATHS[state]:
            if members > 1 or target != 'integrating':  # a unit of one publishes directly
                self.step(unit_id, target)
        self.assertEqual(self.store.unit(unit_id)['state'], state)
        return unit_id

    def raw(self, sql, params=()):
        """Write SQL directly, bypassing the typed API, to test the database's own constraints."""
        with self.store.write() as db:
            return db.execute(sql, params)


class PermissionTests(StoreCase):
    def test_database_and_its_journal_files_are_private(self):
        state = self.root / 'state'
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)
        for name in (DATABASE, f'{DATABASE}-wal', f'{DATABASE}-shm', ANCHOR):
            self.assertEqual(os.stat(state / name).st_mode & 0o777, 0o600, name)

    def test_connection_settings(self):
        db = self.store.db
        self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
        self.assertEqual(db.execute('PRAGMA synchronous').fetchone()[0], 2)  # FULL
        self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0], 1)
        self.assertEqual(db.execute('PRAGMA busy_timeout').fetchone()[0], 5000)

    def test_refuses_symlinked_database(self):
        self.store.close()
        real = self.root / 'elsewhere.sqlite3'
        (self.root / 'state' / DATABASE).rename(real)
        (self.root / 'state' / DATABASE).symlink_to(real)
        with self.assertRaises(StoreError) as caught:
            Store(self.root)
        self.assertEqual(caught.exception.code, 'unsafe_path')

    def test_refuses_symlinked_state_directory(self):
        self.store.close()
        (self.root / 'state').rename(self.root / 'real-state')
        (self.root / 'state').symlink_to(self.root / 'real-state')
        with self.assertRaises(StoreError) as caught:
            Store(self.root)
        self.assertEqual(caught.exception.code, 'unsafe_path')

    def test_refuses_group_readable_database_or_directory(self):
        self.store.close()
        for path, mode, restore in ((self.root / 'state' / DATABASE, 0o640, 0o600), (self.root / 'state', 0o750, 0o700)):
            os.chmod(path, mode)
            with self.assertRaises(StoreError) as caught:
                Store(self.root)
            self.assertEqual(caught.exception.code, 'unsafe_path')
            os.chmod(path, restore)
        Store(self.root).close()

    def test_refuses_to_start_over_after_the_database_is_lost(self):
        self.store.close()
        for suffix in ('', '-wal', '-shm'):
            Path(f'{self.root / "state" / DATABASE}{suffix}').unlink(missing_ok=True)
        with self.assertRaises(StoreError) as caught:
            Store(self.root, create=True)
        self.assertEqual(caught.exception.code, 'store_lost')

    def test_an_uninitialized_store_needs_explicit_creation(self):
        with tempfile.TemporaryDirectory() as other:
            with self.assertRaises(StoreError) as caught:
                Store(other)
            self.assertEqual(caught.exception.code, 'store_missing')

    def test_refuses_a_database_from_another_installation(self):
        self.store.close()
        (self.root / 'state' / ANCHOR).write_text(json.dumps({'format': 'forge-store/1', 'instance_id': 'other'}))
        with self.assertRaises(StoreError) as caught:
            Store(self.root)
        self.assertEqual(caught.exception.code, 'anchor_mismatch')

    def test_refuses_a_newer_or_altered_schema(self):
        self.raw("INSERT INTO schema_migrations VALUES (99, 'future', 'x', 1)")
        self.store.close()
        with self.assertRaises(StoreError) as caught:
            Store(self.root)
        self.assertEqual(caught.exception.code, 'schema_newer')
        db = sqlite3.connect(self.root / 'state' / DATABASE)
        db.execute('DELETE FROM schema_migrations WHERE version = 99')
        db.execute("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = 1")
        db.commit()
        db.close()
        with self.assertRaises(StoreError) as caught:
            Store(self.root)
        self.assertEqual(caught.exception.code, 'schema_mismatch')

    def test_reopening_applies_no_migration_twice(self):
        self.store.close()
        store = self.open()
        with store.read() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT version, name FROM schema_migrations')], [(1, 'initial')])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)


class RegistryTests(StoreCase):
    def test_every_code_has_wording_and_no_wording_is_orphaned(self):
        self.assertEqual(set(status.WORDING), status.registered_codes())
        for code in status.registered_codes():
            plain = status.describe(code)
            self.assertTrue(plain and plain[0].isupper() and '{' not in plain, code)

    def test_every_gate_has_a_release_rule_kind_and_scope(self):
        for code, gate in codes.GATES.items():
            self.assertIn(gate.release, codes.RELEASE_RULES, code)
            self.assertIn(gate.kind, codes.GATE_KINDS, code)
            self.assertTrue(gate.scopes and gate.scopes <= {'unit', 'member'}, code)
            if gate.candidate:
                self.assertEqual(gate.scopes, {'unit'}, code)

    def test_every_close_code_maps_to_an_issue_outcome(self):
        for code, close in codes.CLOSE_CODES.items():
            self.assertIn(close.issue_outcome, ('done', 'dropped', None), code)
        self.assertEqual(codes.CLOSE_CODES['github_completed'].issue_outcome, 'done')

    def test_transitions_cover_every_state_and_terminal_states_have_no_exit(self):
        self.assertEqual(set(codes.TRANSITIONS), set(codes.UNIT_STATES))
        for state, targets in codes.TRANSITIONS.items():
            self.assertTrue(targets <= set(codes.UNIT_STATES))
            self.assertNotIn(state, targets)
            self.assertEqual(not targets, state in codes.TERMINAL_STATES, state)
            if state not in codes.TERMINAL_STATES:
                self.assertIn('closed', targets)  # every open unit can end

    def test_database_registry_matches_codes(self):
        with self.store.read() as db:
            states = {row[0]: bool(row[1]) for row in db.execute('SELECT code, terminal FROM unit_states')}
            pairs = {tuple(row) for row in db.execute('SELECT from_state, to_state FROM unit_transitions')}
            gates = {row[0]: tuple(row[1:]) for row in db.execute('SELECT code, kind, priority, release, member_scope FROM gate_codes')}
            closes = {row[0]: row[1] for row in db.execute('SELECT code, issue_outcome FROM close_codes')}
        self.assertEqual(states, {state: state in codes.TERMINAL_STATES for state in codes.UNIT_STATES})
        self.assertEqual(pairs, {(a, b) for a, targets in codes.TRANSITIONS.items() for b in targets})
        self.assertEqual(gates, {code: (g.kind, g.priority, g.release, int('member' in g.scopes))
                                 for code, g in codes.GATES.items()})
        self.assertEqual(closes, {code: c.issue_outcome for code, c in codes.CLOSE_CODES.items()})

    def test_describe_uses_parameters_when_given(self):
        self.assertEqual(status.describe('dependency', issues='#4 and #5'), 'Waiting for #4 and #5 to close')
        self.assertEqual(status.describe('dependency'), 'Waiting for other issues to close')
        self.assertEqual(status.describe('dependency', other='x'), 'Waiting for other issues to close')
        with self.assertRaises(KeyError):
            status.describe('blocked_auth')

    def test_the_registry_itself_is_fixed_outside_migrations(self):
        for sql in ("DELETE FROM gate_codes WHERE code = 'capacity'", "UPDATE unit_transitions SET to_state = 'merged'",
                    "INSERT INTO unit_gates (id, unit_id, subject_key, code, created_at) VALUES ('g', 'u', 'unit', 'invented', 1)"):
            with self.assertRaises(InvariantViolation, msg=sql):
                self.raw(sql)


class ConstraintTests(StoreCase):
    """One test per invariant that `Ledger.validate()` checked over the whole ledger, and the rest."""

    def test_one_job_per_slot(self):
        first, second = self.unit_at('building'), self.unit_at('building')
        member = self.store.unit(second)['members'][0]['id']
        self.store.reserve_job(first, role='build', slot='build-1', member_id=self.store.unit(first)['members'][0]['id'])
        with self.assertRaises(InvariantViolation):
            self.store.reserve_job(second, role='build', slot='build-1', member_id=member)

    def test_one_live_job_per_part(self):
        unit_id = self.unit_at('building')
        member = self.store.unit(unit_id)['members'][0]['id']
        job = self.store.reserve_job(unit_id, role='build', slot='build-1', member_id=member)
        with self.assertRaises(InvariantViolation):
            self.store.reserve_job(unit_id, role='recovery', slot='build-2', member_id=member)
        # A different part of the same unit may run in parallel.
        other = self.store.unit(unit_id)['members'][1]['id']
        self.store.reserve_job(unit_id, role='build', slot='build-3', member_id=other)
        # Once the job finishes, the part may start its next job before cleanup completes.
        self.store.finish_job(job['job_id'], 'failed')
        self.store.reserve_job(unit_id, role='recovery', slot='build-2', member_id=member)

    def test_one_live_unit_level_job(self):
        unit_id = self.unit_at('reviewing')
        self.store.reserve_job(unit_id, role='review', slot='review-1')
        with self.assertRaises(InvariantViolation):
            self.store.reserve_job(unit_id, role='review', slot='review-2')

    def test_one_open_unit_per_issue(self):
        issue = self.issue()
        self.store.admit(issue, actor='test')
        with self.assertRaises(InvariantViolation):
            self.store.admit(issue, actor='test')
        unit_id = self.unit()
        with self.assertRaises(InvariantViolation):
            self.join(unit_id, issue)  # the database refuses it too

    def test_an_issue_is_free_again_once_its_unit_ends(self):
        unit_id = self.unit()
        issue = self.store.unit(unit_id)['members'][0]['issue_id']
        self.step(unit_id, 'closed')
        self.store.admit(issue, actor='test')

    def test_one_open_gate_per_code_and_subject(self):
        unit_id = self.unit(2)
        first = self.store.open_gate(unit_id, 'dependency')
        self.assertEqual(self.store.open_gate(unit_id, 'dependency'), first)
        members = [m['id'] for m in self.store.unit(unit_id)['members']]
        # Independent same-code member gates are allowed.
        a = self.store.open_gate(unit_id, 'dependency', member_id=members[0])
        b = self.store.open_gate(unit_id, 'dependency', member_id=members[1])
        self.assertEqual(len({first, a, b}), 3)
        with self.assertRaises(InvariantViolation):
            self.raw("INSERT INTO unit_gates (id, unit_id, subject_key, code, created_at) VALUES ('dup', ?, 'unit', 'dependency', 1)",
                     (unit_id,))
        self.store.resolve_gate(first, release='observation')
        self.assertNotEqual(self.store.open_gate(unit_id, 'dependency'), first)

    def test_gate_scope_fits_its_code(self):
        unit_id = self.unit()
        member = self.store.unit(unit_id)['members'][0]['id']
        with self.assertRaises(InvariantViolation):
            self.store.open_gate(unit_id, 'operator_pause', member_id=member)
        with self.assertRaises(InvariantViolation):
            self.raw("INSERT INTO unit_gates (id, unit_id, member_id, subject_key, code, created_at)"
                     " VALUES ('g', ?, ?, ?, 'budget_exhausted', 1)", (unit_id, member, f'member:{member}'))
        with self.assertRaises(InvariantViolation):  # candidate gates need a candidate
            self.raw("INSERT INTO unit_gates (id, unit_id, subject_key, code, created_at) VALUES ('g', ?, 'unit', 'merge_checks', 1)",
                     (unit_id,))
        with self.assertRaises(IllegalTransition):
            self.store.open_gate(unit_id, 'merge_checks')

    def test_a_finished_unit_has_no_live_members_gates_or_jobs(self):
        unit_id = self.unit_at('reviewing')
        self.store.reserve_job(unit_id, role='review', slot='review-1')
        self.store.open_gate(unit_id, 'operator_pause')
        self.step(unit_id, 'closed')
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM unit_issues WHERE unit_id = ? AND left_at IS NULL', (unit_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM unit_gates WHERE unit_id = ? AND resolved_at IS NULL', (unit_id,)).fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM jobs WHERE unit_id = ? AND fenced = 0', (unit_id,)).fetchone()[0], 0)
        issue = self.issue()
        for sql, params in (
                ("INSERT INTO unit_issues (id, unit_id, issue_id, repository_id, base_branch, requirements_revision_id, state,"
                 " joined_at) SELECT 'late', ?, id, repository_id, 'main', current_revision_id, 'pending', 1 FROM issues WHERE id = ?",
                 (unit_id, issue)),
                ("INSERT INTO unit_gates (id, unit_id, subject_key, code, created_at) VALUES ('late', ?, 'unit', 'capacity', 1)", (unit_id,))):
            with self.assertRaises(InvariantViolation):
                self.raw(sql, params)

    def test_the_database_refuses_to_end_a_unit_with_live_members(self):
        unit_id = self.unit()
        with self.assertRaises(InvariantViolation):
            self.raw("UPDATE units SET state = 'closed', close_code = 'operator_closed', version = version + 1 WHERE id = ?", (unit_id,))

    def test_unit_versions_advance_by_one(self):
        unit_id = self.unit()
        with self.assertRaises(InvariantViolation):
            self.raw("UPDATE units SET updated_at = 5 WHERE id = ?", (unit_id,))
        with self.assertRaises(InvariantViolation):
            self.raw("UPDATE units SET version = version + 2 WHERE id = ?", (unit_id,))

    def test_closed_needs_a_close_code_and_merged_a_merge_commit(self):
        unit_id = self.unit()
        for sql in ("INSERT INTO units (id, repository_id, base_branch, state, created_at, updated_at) VALUES ('x', 1, 'main', 'closed', 1, 1)",
                    "INSERT INTO units (id, repository_id, base_branch, state, created_at, updated_at) VALUES ('x', 1, 'main', 'merged', 1, 1)",
                    "INSERT INTO units (id, repository_id, base_branch, state, close_code, created_at, updated_at)"
                    " VALUES ('x', 1, 'main', 'queued', 'combined', 1, 1)"):
            with self.assertRaises(InvariantViolation, msg=sql):
                self.raw(sql)
        with self.assertRaises(StoreError):
            self.store.close_unit(unit_id, close_code='because', expected_version=1, actor='test')

    def test_history_is_append_only(self):
        unit_id = self.unit_at('reviewing')
        job = self.store.reserve_job(unit_id, role='review', slot='review-1')
        self.store.put_artifact(job['job_id'], 'result', b'{}', media_type='application/json')
        for sql in ('UPDATE events SET kind = "other"', 'DELETE FROM events', 'UPDATE candidates SET head_oid = "x"',
                    'DELETE FROM candidates', 'UPDATE issue_revisions SET body = "x"', 'DELETE FROM unit_issues',
                    'UPDATE job_artifacts SET content = NULL, expired_at = 1', 'DELETE FROM job_artifacts', 'DELETE FROM jobs',
                    'UPDATE unit_issues SET issue_id = "x"'):
            with self.assertRaises(InvariantViolation, msg=sql):
                self.raw(sql.replace('"', "'"))

    def test_leases_are_released_only_after_verified_cleanup(self):
        unit_id = self.unit_at('reviewing')
        job = self.store.reserve_job(unit_id, role='review', slot='review-1')
        with self.assertRaises(InvariantViolation):
            self.raw('DELETE FROM job_leases')
        with self.assertRaises(InvariantViolation):
            self.raw("UPDATE job_leases SET slot = 'review-2'")
        self.store.finish_job(job['job_id'], 'passed')
        self.assertTrue(self.store.release_lease(job['job_id']))
        with self.assertRaises(InvariantViolation):  # a cleaned job cannot take a slot again
            self.raw("INSERT INTO job_leases VALUES ('review-1', ?, 'f2', 1)", (job['job_id'],))
        with self.assertRaises(InvariantViolation):  # jobs only move forward
            self.raw("UPDATE jobs SET phase = 'running', fenced = 0, cleanup = 0 WHERE id = ?", (job['job_id'],))

    def test_budget_charges_are_unique_per_attempt_and_corrections_reference_them(self):
        unit_id = self.unit_at('reviewing')
        job = self.store.reserve_job(unit_id, role='review', slot='review-1')
        attempt = job['attempt_id']
        self.raw("INSERT INTO budget_entries VALUES ('b1', ?, 'charge', NULL, 'code_rejected', 1)", (attempt,))
        for sql in ("INSERT INTO budget_entries VALUES ('b2', ?, 'charge', NULL, 'code_rejected', 2)",
                    "INSERT INTO budget_entries VALUES ('b3', ?, 'correction', 'missing', 'environment', 2)",
                    "UPDATE budget_entries SET reason_code = 'x' WHERE attempt_id = ?"):
            with self.assertRaises(InvariantViolation, msg=sql):
                self.raw(sql, (attempt,))
        self.raw("INSERT INTO budget_entries VALUES ('b4', ?, 'correction', 'b1', 'environment', 2)", (attempt,))

    def test_a_build_job_names_its_part(self):
        unit_id = self.unit_at('building')
        with self.assertRaises(InvariantViolation):
            self.store.reserve_job(unit_id, role='build', slot='build-1')

    def test_review_jobs_use_the_current_candidate(self):
        unit_id = self.unit_at('reviewing')
        with self.assertRaises(IllegalTransition):
            self.store.reserve_job(unit_id, role='review', slot='review-1', candidate_id='old')

    def test_an_older_issue_observation_never_replaces_a_newer_one(self):
        issue = self.store.observe_issue(self.repo, 50, state='open', title='New', body='B', requirements={},
                                         remote_updated_at='2026-10-02T00:00:00Z', generation=2)
        older = self.store.observe_issue(self.repo, 50, state='closed', title='Old', body='B', requirements={},
                                         remote_updated_at='2026-10-01T00:00:00Z', generation=3)
        self.assertTrue(older['stale'])
        with self.store.read() as db:
            row = db.execute('SELECT github_state, current_revision_id FROM issues WHERE id = ?', (issue['issue_id'],)).fetchone()
        self.assertEqual(tuple(row), ('open', issue['revision_id']))
        with self.assertRaises(InvariantViolation):
            self.raw("UPDATE issues SET remote_updated_at = '2026-01-01', version = version + 1 WHERE id = ?", (issue['issue_id'],))

    def test_a_requirements_change_keeps_the_pinned_revision(self):
        unit_id = self.unit()
        member = self.store.unit(unit_id)['members'][0]
        number = int(member['issue_id'].split(':')[1])
        changed = self.store.observe_issue(self.repo, number, state='open', title='Title', body='Changed', requirements={'goal': 'y'},
                                           remote_updated_at='2026-10-03T00:00:00Z', generation=2)
        self.assertNotEqual(changed['revision_id'], member['requirements_revision_id'])
        self.assertEqual(self.store.unit(unit_id)['members'][0]['requirements_revision_id'], member['requirements_revision_id'])

    def test_closed_issues_are_not_admitted(self):
        with self.assertRaises(IllegalTransition):
            self.store.admit(self.issue(state='closed'), actor='test')


class TransitionTests(StoreCase):
    def test_every_legal_transition(self):
        for frm, targets in codes.TRANSITIONS.items():
            for to in targets:
                with self.subTest(frm=frm, to=to):
                    unit_id = self.unit_at(frm)
                    before = self.store.unit(unit_id)
                    result = self.step(unit_id, to)
                    after = self.store.unit(unit_id)
                    self.assertEqual((result['state'], after['state']), (to, to))
                    self.assertEqual(after['version'], before['version'] + 1)
                    event = [e for e in self.store.events_after(0, 1000) if e['kind'] == 'phase_changed'][-1]
                    self.assertEqual((event['payload']['before'], event['payload']['after']), (frm, to))
                    if to in codes.TERMINAL_STATES:
                        self.assertEqual(after['members'], [])

    def test_every_illegal_transition_is_rejected_by_the_store(self):
        reachable = {'building', 'integrating', 'reviewing', 'auditing', 'merging', 'merged', 'closed', 'dissolved'}
        for frm in codes.UNIT_STATES:
            for to in sorted(reachable - codes.TRANSITIONS[frm]):
                with self.subTest(frm=frm, to=to):
                    unit_id = self.unit_at(frm)
                    version = self.version(unit_id)
                    with self.assertRaises(IllegalTransition):
                        self.step(unit_id, to)
                    self.assertEqual(self.version(unit_id), version)

    def test_every_illegal_transition_is_rejected_by_the_database(self):
        for frm, to in itertools.product(codes.UNIT_STATES, repeat=2):
            if frm == to:
                continue
            with self.subTest(frm=frm, to=to):
                unit = f'raw-{frm}-{to}'
                self.raw("INSERT INTO units (id, repository_id, base_branch, state, close_code, merge_sha, created_at, updated_at)"
                         " VALUES (?, 1, 'main', ?, ?, ?, 1, 1)",
                         (unit, frm, 'operator_closed' if frm == 'closed' else None, 'sha' if frm == 'merged' else None))
                update = ("UPDATE units SET state = ?, close_code = ?, merge_sha = ?, version = version + 1 WHERE id = ?",
                          (to, 'operator_closed' if to == 'closed' else None, 'sha' if to == 'merged' else None, unit))
                if to in codes.TRANSITIONS[frm]:
                    self.raw(*update)
                else:
                    with self.assertRaisesRegex(InvariantViolation, 'illegal unit transition'):
                        self.raw(*update)

    def test_a_stale_version_is_rejected(self):
        unit_id = self.unit()
        with self.assertRaises(StaleVersion):
            self.store.build(unit_id, expected_version=2, actor='test')
        self.store.build(unit_id, expected_version=1, actor='test')
        with self.assertRaises(StaleVersion):
            self.store.close_unit(unit_id, close_code='operator_closed', expected_version=1, actor='test')

    def test_a_unit_of_one_reworks_by_building_and_never_integrates(self):
        unit_id = self.unit_at('reviewing', members=1)
        with self.assertRaises(IllegalTransition):
            self.step(unit_id, 'integrating')
        self.step(unit_id, 'building')
        self.step(unit_id, 'reviewing')

    def test_audit_and_merge_need_a_current_candidate(self):
        unit_id = self.unit_at('building')
        with self.assertRaises(IllegalTransition):
            self.step(unit_id, 'auditing')

    def test_dissolve_releases_members_to_new_units_of_one(self):
        unit_id = self.unit_at('building', members=3)
        issues = sorted(m['issue_id'] for m in self.store.unit(unit_id)['members'])
        result = self.step(unit_id, 'dissolved')
        self.assertEqual(len(result['released']), 3)
        released = sorted(self.store.unit(u)['members'][0]['issue_id'] for u in result['released'])
        self.assertEqual(released, issues)
        with self.store.read() as db:
            reasons = {row[0] for row in db.execute('SELECT exit_reason FROM unit_issues WHERE unit_id = ?', (unit_id,))}
        self.assertEqual(reasons, {'dissolved'})
        with self.assertRaises(IllegalTransition):
            self.step(self.unit_at('building', members=1), 'dissolved')

    def test_merged_records_done_members_and_fences_live_jobs(self):
        unit_id = self.unit_at('merging')
        job = self.store.reserve_job(unit_id, role='merge', slot='merge-1')
        self.step(unit_id, 'merged')
        with self.store.read() as db:
            parts = {tuple(row) for row in db.execute('SELECT state, exit_reason FROM unit_issues WHERE unit_id = ?', (unit_id,))}
            fenced = db.execute('SELECT fenced FROM jobs WHERE id = ?', (job['job_id'],)).fetchone()[0]
        self.assertEqual(parts, {('done', 'merged')})
        self.assertEqual(fenced, 1)
        self.assertEqual(self.store.unit(unit_id)['plain'], 'Merged in pull request #7')

    def test_a_new_candidate_supersedes_the_old_candidates_gates(self):
        unit_id = self.unit_at('reviewing', members=1)
        old = self.store.unit(unit_id)['current_candidate_id']
        gate = self.store.open_gate(unit_id, 'verification_human')
        self.step(unit_id, 'building')
        self.step(unit_id, 'reviewing')
        unit = self.store.unit(unit_id)
        self.assertNotEqual(unit['current_candidate_id'], old)
        self.assertIsNone(unit['blocker'])
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT resolution FROM unit_gates WHERE id = ?', (gate,)).fetchone()[0], 'superseded')

    def test_a_gate_is_released_only_by_its_own_rule(self):
        unit_id = self.unit()
        pause = self.store.open_gate(unit_id, 'operator_pause')
        environment = self.store.open_gate(unit_id, 'environment')
        with self.assertRaises(StoreError) as caught:
            self.store.resolve_gate(pause, release='recheck')  # a recheck never releases a pause
        self.assertEqual(caught.exception.code, 'release_refused')
        self.assertTrue(self.store.resolve_gate(environment, release='recheck'))
        self.assertEqual(self.store.unit(unit_id)['blocker']['code'], 'operator_pause')
        self.assertFalse(self.store.resolve_gate(environment, release='recheck'))

    def test_one_current_blocker_by_precedence(self):
        unit_id = self.unit(2)
        member = self.store.unit(unit_id)['members'][0]['id']
        self.store.open_gate(unit_id, 'retry_delay', details={'until': '10:00'})
        self.store.open_gate(unit_id, 'dependency', member_id=member, details={'issues': '#3'})
        self.assertEqual(self.store.unit(unit_id)['blocker']['plain'], 'Waiting for #3 to close')
        label = self.store.open_gate(unit_id, 'label_hold', details={'label': 'hold'})
        blocker = self.store.unit(unit_id)['blocker']
        self.assertEqual((blocker['code'], blocker['plain']), ('label_hold', 'On hold by the "hold" label; removing it resumes the work'))
        self.store.resolve_gate(label, release='observation')
        self.assertEqual(self.store.unit(unit_id)['blocker']['code'], 'dependency')

    def test_gates_advance_the_unit_version(self):
        unit_id = self.unit()
        gate = self.store.open_gate(unit_id, 'operator_pause')
        with self.assertRaises(StaleVersion):
            self.store.build(unit_id, expected_version=1, actor='test')
        self.store.resolve_gate(gate, release='operator')
        self.assertEqual(self.version(unit_id), 3)


class DecisionTests(StoreCase):
    def test_a_replayed_request_returns_the_original_result_without_another_event(self):
        unit_id = self.unit()
        first = self.store.build(unit_id, expected_version=1, actor='op', request_id='r1')
        count = len(self.store.events_after(0, 1000))
        self.assertEqual(self.store.build(unit_id, expected_version=1, actor='op', request_id='r1'), first)
        self.assertEqual(len(self.store.events_after(0, 1000)), count)
        self.assertEqual(self.version(unit_id), 2)
        for sql in ('DELETE FROM decisions', "UPDATE decisions SET result_json = '{}'"):
            with self.assertRaises(InvariantViolation, msg=sql):
                self.raw(sql)

    def test_a_rejected_request_is_recorded_and_replays_its_rejection(self):
        unit_id = self.unit()
        with self.assertRaises(IllegalTransition):
            self.store.merge(unit_id, expected_version=1, actor='op', request_id='r2')
        with self.assertRaises(IllegalTransition):
            self.store.merge(unit_id, expected_version=1, actor='op', request_id='r2')
        with self.store.read() as db:
            row = db.execute("SELECT status, error_code FROM decisions WHERE request_id = 'r2'").fetchone()
        self.assertEqual(tuple(row), ('rejected', 'illegal_transition'))
        kinds = [e['kind'] for e in self.store.events_after(0, 1000)]
        self.assertEqual(kinds.count('decision_rejected'), 1)
        self.assertNotIn('phase_changed', kinds)

    def test_a_rejection_leaves_no_partial_write(self):
        unit_id = self.unit_at('reviewing', members=1)
        with self.store.write() as db:
            candidates = db.execute('SELECT count(*) FROM candidates').fetchone()[0]
        with self.assertRaises(IllegalTransition):
            self.store.review(unit_id, candidate=self.candidate(), expected_version=self.version(unit_id), actor='op',
                              request_id='r3')
        with self.store.read() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone()[0], candidates)

    def test_a_request_id_cannot_be_reused_for_another_command(self):
        unit_id = self.unit()
        self.store.build(unit_id, expected_version=1, actor='op', request_id='r4')
        with self.assertRaises(StoreError) as caught:
            self.store.close_unit(unit_id, close_code='operator_closed', expected_version=2, actor='op', request_id='r4')
        self.assertEqual(caught.exception.code, 'request_reused')

    def test_events_have_a_monotonic_sequence(self):
        unit_id = self.unit()
        self.store.build(unit_id, expected_version=1, actor='op')
        events = self.store.events_after(0, 1000)
        self.assertEqual([e['sequence'] for e in events], sorted({e['sequence'] for e in events}))
        self.assertEqual(self.store.events_after(events[-2]['sequence']), events[-1:])


class ConcurrencyTests(StoreCase):
    def test_a_second_writer_waits_or_gets_a_retryable_busy_error(self):
        unit_id = self.unit()
        quick = self.open(busy_timeout=0.05)
        entered, release, results = threading.Event(), threading.Event(), {}

        def hold():
            other = Store(self.root)
            try:
                with other.write():
                    other.db.execute('UPDATE units SET updated_at = 9, version = version + 1 WHERE id = ?', (unit_id,))
                    entered.set()
                    release.wait(10)
            finally:
                other.close()

        def wait_then_write():
            with Store(self.root, busy_timeout=10) as patient:  # one Store per thread
                results['started'] = True
                results['patient'] = patient.build(unit_id, expected_version=2, actor='b')

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(entered.wait(10))
        with self.assertRaises(StoreBusy) as caught:
            quick.build(unit_id, expected_version=2, actor='a')
        self.assertTrue(caught.exception.retryable)
        waiter = threading.Thread(target=wait_then_write)
        waiter.start()
        time.sleep(0.2)
        self.assertNotIn('patient', results)  # still waiting for the first writer
        release.set()
        holder.join(10)
        waiter.join(10)
        self.assertEqual(results['patient']['version'], 3)

    def test_competing_writers_with_the_same_expected_version(self):
        unit_id = self.unit()
        outcomes, barrier = [], threading.Barrier(2)

        def attempt(command):
            with Store(self.root, busy_timeout=10) as store:
                barrier.wait()
                try:
                    command(store)
                    outcomes.append('applied')
                except StaleVersion:
                    outcomes.append('stale')

        threads = [threading.Thread(target=attempt, args=(lambda s: s.build(unit_id, expected_version=1, actor='a'),)),
                   threading.Thread(target=attempt, args=(lambda s: s.close_unit(
                       unit_id, close_code='operator_closed', expected_version=1, actor='b'),))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        self.assertEqual(sorted(outcomes), ['applied', 'stale'])
        self.assertEqual(self.version(unit_id), 2)

    def test_a_reader_sees_one_consistent_snapshot_during_a_write(self):
        unit_id = self.unit()
        writer, reader = self.open(), self.open()
        events_before = len(reader.events_after(0, 1000))
        with reader.read() as snapshot:
            first = snapshot.execute('SELECT state, version FROM units WHERE id = ?', (unit_id,)).fetchone()
            with writer.write():
                writer.build(unit_id, expected_version=1, actor='w')
                # Uncommitted: invisible to the reader.
                self.assertEqual(tuple(snapshot.execute('SELECT state, version FROM units WHERE id = ?', (unit_id,)).fetchone()),
                                 tuple(first))
            # Committed after the snapshot began: still invisible, and the event count agrees.
            self.assertEqual(tuple(snapshot.execute('SELECT state, version FROM units WHERE id = ?', (unit_id,)).fetchone()),
                             ('queued', 1))
            self.assertEqual(snapshot.execute('SELECT count(*) FROM events').fetchone()[0], events_before)
            with self.assertRaises(sqlite3.OperationalError):  # snapshots are read-only
                snapshot.execute("UPDATE units SET updated_at = 1, version = version + 1")
        self.assertEqual(reader.unit(unit_id)['state'], 'building')
        self.assertEqual(len(reader.events_after(0, 1000)), events_before + 1)


CRASH_CHILD = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from forge.store import Store
store = Store(sys.argv[2])
unit_id, mode = sys.argv[3], sys.argv[4]
if mode == 'hold':
    with store.write() as db:
        store.build(unit_id, expected_version=1, actor='child')
        store.open_gate(unit_id, 'capacity')
        db.execute("INSERT INTO repositories (id, full_name) VALUES (77, 'partial/write')")
        print('inside', flush=True)
        time.sleep(60)
else:
    issue = int(sys.argv[5])
    print('looping', flush=True)
    while True:
        result = store.observe_issue(1, issue, state='open', title='t', body='b', requirements={},
                                     remote_updated_at='2026-10-01', generation=1)
        unit = store.admit(result['issue_id'], actor='child')['unit_id']
        store.build(unit, expected_version=1, actor='child')
        store.close_unit(unit, close_code='operator_closed', expected_version=2, actor='child')
        issue += 1
'''


class CrashTests(StoreCase):
    def child(self, *args):
        process = subprocess.Popen([sys.executable, '-c', CRASH_CHILD, str(ROOT), str(self.root), *args],
                                   stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return process

    def check_consistent(self, store):
        with store.read() as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            # Every committed unit write appended exactly one phase event in the same transaction.
            rows = db.execute("SELECT u.id, u.version, u.state, (SELECT count(*) FROM events e WHERE e.unit_id = u.id"
                              " AND e.kind = 'phase_changed') FROM units u").fetchall()
            gates = dict(db.execute("SELECT unit_id, count(*) FROM events WHERE kind IN ('gate_opened', 'gate_resolved')"
                                    " GROUP BY unit_id").fetchall())
            for unit_id, version, state, phases in rows:
                self.assertEqual(version, 1 + phases + gates.get(unit_id, 0), unit_id)
                live = db.execute('SELECT count(*) FROM unit_issues WHERE unit_id = ? AND left_at IS NULL', (unit_id,)).fetchone()[0]
                self.assertEqual(live == 0, state in codes.TERMINAL_STATES)
        return rows

    def test_a_writer_killed_mid_transaction_leaves_nothing_behind(self):
        unit_id = self.unit()
        process = self.child(unit_id, 'hold')
        self.assertEqual(process.stdout.readline().strip(), 'inside')
        os.kill(process.pid, signal.SIGKILL)
        process.wait(10)
        self.store.close()
        store = self.open()
        self.check_consistent(store)
        unit = store.unit(unit_id)
        self.assertEqual((unit['state'], unit['version'], unit['blocker']), ('queued', 1, None))
        with store.read() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM repositories WHERE id = 77').fetchone())
        store.build(unit_id, expected_version=1, actor='after')  # the lock was released with the process

    def test_writers_killed_at_random_points_leave_whole_transactions(self):
        for round, delay in enumerate((0.05, 0.13, 0.31)):
            process = self.child('-', 'loop', str(1000 * (round + 1)))
            self.assertEqual(process.stdout.readline().strip(), 'looping')
            time.sleep(delay)
            os.kill(process.pid, signal.SIGKILL)
            process.wait(10)
            store = Store(self.root)
            try:
                rows = self.check_consistent(store)
            finally:
                store.close()
            self.assertTrue(rows)


class RetentionTests(StoreCase):
    def setUp(self):
        self.now = 1_000 * DAY
        self.clock = [self.now - 40 * DAY]
        super().setUp()

    def open(self, **kwargs):
        return super().open(clock=lambda: self.clock[0], **kwargs)

    def job_with_logs(self, unit_id, role='review', clean=True, **kwargs):
        job = self.store.reserve_job(unit_id, role=role, slot=f'slot-{next(self.heads)}', **kwargs)['job_id']
        self.store.put_artifact(job, 'output', b'log line\n' * 1000)
        self.store.put_artifact(job, 'result', b'{"verdict": "pass"}', media_type='application/json')
        self.store.finish_job(job, 'passed')
        if clean:
            self.store.release_lease(job)
        return job

    def test_raw_logs_expire_after_thirty_days_and_durable_facts_remain(self):
        finished = self.unit_at('reviewing')
        old = self.job_with_logs(finished)
        uncleaned = self.job_with_logs(self.unit_at('reviewing'), clean=False)
        current = self.job_with_logs(self.unit_at('reviewing'))  # current candidate of an open unit
        self.step(finished, 'closed')
        self.clock[0] = self.now - 10 * DAY
        recent = self.job_with_logs(self.unit_at('merging'))
        self.assertEqual(self.store.prune_raw_logs(now=self.now), 1)
        self.assertEqual(self.store.read_artifact(old, 'output')['status'], 'expired')
        self.assertEqual(self.store.read_artifact(old, 'result'),
                         {'status': 'available', 'content': b'{"verdict": "pass"}'})
        for job in (uncleaned, current, recent):
            self.assertEqual(self.store.read_artifact(job, 'output')['status'], 'available')
        self.assertEqual(self.store.prune_raw_logs(now=self.now), 0)

    def test_logs_a_pending_recovery_needs_are_kept(self):
        unit_id = self.unit_at('building')
        member = self.store.unit(unit_id)['members'][0]['id']
        job = self.job_with_logs(unit_id, role='build', member_id=member)
        self.store.open_gate(unit_id, 'recovery_pending', member_id=member)
        self.assertEqual(self.store.prune_raw_logs(now=self.now), 0)
        self.assertEqual(self.store.read_artifact(job, 'output')['status'], 'available')

    def test_artifacts_are_compressed_and_reads_are_bounded(self):
        unit_id = self.unit_at('reviewing')
        job = self.job_with_logs(unit_id, clean=False)
        with self.store.read() as db:
            row = db.execute("SELECT codec, size, raw_size FROM job_artifacts WHERE job_id = ? AND kind = 'output'", (job,)).fetchone()
        self.assertEqual(row['codec'], 'zlib')
        self.assertLess(row['size'], row['raw_size'] // 10)
        self.assertEqual(self.store.read_artifact(job, 'output', limit=100)['status'], 'too_large')
        self.assertEqual(self.store.read_artifact(job, 'summary')['status'], 'missing')


if __name__ == '__main__':
    unittest.main()
