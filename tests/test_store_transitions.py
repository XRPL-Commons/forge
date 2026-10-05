import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3
import tempfile
import unittest
from dataclasses import replace

from forge.store.db import Store
from forge.store.models import Candidate, Command, IdempotencyConflict, TransitionError, VersionConflict
from forge.store.transitions import Lifecycle, manifest_hash, project_unit


class StoreTransitionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store.create(self.directory.name)
        self.lifecycle = Lifecycle(self.store, clock=lambda: 100.0)
        self.counter = 0
        self.admission = self.admit()
        self.member = self.admission['member_id']
        self.manifest = {'requirements_revision_id': self.admission['revision_id'], 'base_oid': 'base', 'policy_hash': 'policy'}

    def admit(self, number=1):
        return self.lifecycle.admit_issue(
            request_id=f'admit-{number}', actor_id='operator', unit_id=f'unit-{number}',
            issue_id=f'issue-{number}', repository_id=1, repository_name='owner/repository',
            issue_number=number, title=f'Issue {number}', body='Implement the requirement',
            requirements={'acceptance': ['works']}, observed_at=90.0,
        )

    def view(self, unit_id='unit-1'):
        with self.store.read_snapshot() as db:
            return project_unit(db, unit_id)

    def command(self, *, unit_id='unit-1'):
        self.counter += 1
        return Command(f'command-{self.counter}', 'operator', unit_id, self.view(unit_id)['version'])

    def reserve(self, member=None, manifest=None, slot='slot-1'):
        return self.lifecycle.reserve_job(self.command(), 'build', slot=slot, execution_fence=f'fence-{slot}',
                                          input_manifest=manifest or self.manifest, member_id=member or self.member)

    def cleanup(self, job, slot='slot-1'):
        return self.lifecycle.verify_cleanup(self.command(), job['job_id'], execution_fence=f'fence-{slot}',
                                             evidence={'process_stopped': True, 'resources_released': True})

    def fail(self, job, manifest=None, slot='slot-1'):
        return self.lifecycle.record_failure(self.command(), job['job_id'], execution_fence=f'fence-{slot}',
                                             input_manifest=manifest or self.manifest, evidence={'diagnosis': 'code_failure'})

    def resolve_recovery(self, member=None):
        with self.store.read_snapshot() as db:
            gates = db.execute("SELECT id FROM unit_gates WHERE unit_id = 'unit-1' AND code = 'recovery_pending' AND resolved_at IS NULL AND member_id IS ?", (member or self.member,)).fetchall()
        for gate in gates:
            self.lifecycle.resolve_gate(self.command(), gate[0], evidence={'repair_verified': True})

    def accept_build(self, job):
        candidate = Candidate(1, 'forge/issue-1', 'head', 'base', 'policy', manifest_hash({'acceptance': ['works']}), manifest_hash(self.manifest))
        return self.lifecycle.finish_job(self.command(), job['job_id'], execution_fence='fence-slot-1',
                                         input_manifest=self.manifest, evidence={'head_oid': 'head', 'base_oid': 'base', 'pull_number': 1}, candidate=candidate)

    def test_unit_of_one_advances_only_with_exact_candidate_evidence(self):
        build = self.reserve()
        self.assertEqual(self.view()['stage_code'], 'building')
        self.accept_build(build)
        self.assertEqual(self.view()['stage_code'], 'reviewing')
        # A successful process still holds its capacity lease until verified cleanup.
        with self.assertRaises(TransitionError):
            self.lifecycle.verify_cleanup(self.command(), build['job_id'], execution_fence='wrong', evidence={'process_stopped': True, 'resources_released': True})
        self.cleanup(build)
        for role, target in [('review', 'auditing'), ('audit', 'merging')]:
            with self.store.read_snapshot() as db:
                identity = dict(db.execute('SELECT * FROM candidates WHERE id = ?', (self.view()['current_candidate_id'],)).fetchone())
            manifest = {key: identity[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
            manifest['candidate_id'] = identity['id']
            job = self.lifecycle.reserve_job(self.command(), role, slot='slot-1', execution_fence='fence-slot-1', input_manifest=manifest, candidate_id=identity['id'])
            with self.assertRaises(TransitionError):
                self.lifecycle.finish_job(self.command(), job['job_id'], execution_fence='fence-slot-1', input_manifest=manifest, evidence={'candidate_id': 'old', 'verdict': 'pass'})
            self.lifecycle.finish_job(self.command(), job['job_id'], execution_fence='fence-slot-1', input_manifest=manifest, evidence={'candidate_id': identity['id'], 'verdict': 'pass'})
            self.assertEqual(self.view()['stage_code'], target)
            self.cleanup(job)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candidate_evidence').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0], 0)

    def test_idempotency_replays_original_result_and_rejects_changed_actor_payload_or_version(self):
        command = self.command()
        first = self.lifecycle.open_gate(command, 'environment', details={'capability': 'database'})
        self.assertEqual(first, self.lifecycle.open_gate(command, 'environment', details={'capability': 'database'}))
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE transaction_id = ?", (command.request_id,)).fetchone()[0], 3)
        for altered, details in [(replace(command, actor_id='other'), {'capability': 'database'}),
                                 (replace(command, expected_version=99), {'capability': 'database'}),
                                 (command, {'capability': 'browser'})]:
            with self.assertRaises(IdempotencyConflict):
                self.lifecycle.open_gate(altered, 'environment', details=details)
        with self.assertRaises(VersionConflict):
            self.lifecycle.open_gate(replace(command, request_id='new'), 'dependency', details={})

    def test_pause_fences_worker_without_closing_open_issue_and_resume_preserves_other_gates(self):
        job = self.reserve()
        self.lifecycle.open_gate(self.command(), 'dependency', details={'issue': 'external'})
        self.lifecycle.pause(self.command())
        view = self.view()
        self.assertEqual(view['state'], 'building')
        self.assertEqual(view['members'][0]['github_state'], 'open')
        self.assertEqual(view['current_blocker']['code'], 'operator_pause')
        result = self.accept_build(job)
        self.assertFalse(result['accepted'])
        self.assertIsNone(self.view()['current_candidate_id'])
        self.lifecycle.resume(self.command())
        self.assertEqual(self.view()['current_blocker']['code'], 'dependency')
        self.cleanup(job)

    def test_stale_assignment_manifest_and_version_cannot_advance_or_consume_result(self):
        job = self.reserve()
        command = self.command()
        self.lifecycle.open_gate(self.command(), 'environment', details={})
        with self.assertRaises(VersionConflict):
            self.lifecycle.record_failure(command, job['job_id'], execution_fence='fence-slot-1', input_manifest=self.manifest, evidence={'failure': True})
        with self.assertRaises(TransitionError):
            self.lifecycle.record_failure(self.command(), job['job_id'], execution_fence='fence-slot-1', input_manifest={**self.manifest, 'base_oid': 'changed'}, evidence={'failure': True})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_artifacts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)

    def test_failed_result_and_gate_changes_rollback_if_lease_capacity_is_taken(self):
        self.reserve()
        other = self.admit(2)
        version = self.view('unit-2')['version']
        with self.assertRaises(sqlite3.IntegrityError):
            self.lifecycle.reserve_job(self.command(unit_id='unit-2'), 'build', slot='slot-1', execution_fence='other',
                                       input_manifest={**self.manifest, 'requirements_revision_id': other['revision_id']}, member_id=other['member_id'])
        self.assertEqual(self.view('unit-2')['version'], version)
        self.assertEqual(self.view('unit-2')['state'], 'queued')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM attempts WHERE unit_id = 'unit-2'").fetchone()[0], 0)

    def add_independent_member(self, number):
        # Fixture only: batch composition commands remain a later delivery phase.
        admitted = self.admit(number)
        with self.store.write() as db:
            db.execute('UPDATE unit_issues SET unit_id = ? WHERE id = ?', ('unit-1', admitted['member_id']))
            db.execute("UPDATE units SET state = 'dissolved' WHERE id = ?", (f'unit-{number}',))
        return admitted['member_id'], {**self.manifest, 'requirements_revision_id': admitted['revision_id']}

    def test_member_gate_blocks_only_that_part_and_projection_agrees_with_dispatch(self):
        sibling, manifest = self.add_independent_member(2)
        self.lifecycle.open_gate(self.command(), 'environment', details={'database': 'missing'}, member_id=self.member)
        self.assertIsNone(self.view()['current_blocker'])
        with self.assertRaises(TransitionError):
            self.reserve()
        job = self.reserve(sibling, manifest)
        self.assertIsNotNone(job['job_id'])
        self.assertIsNone(self.view()['current_blocker'])
        self.fail(job, manifest)
        self.assertIsNotNone(self.view()['current_blocker'])

    def test_unvalidated_current_dependency_rows_block_only_dependent_members(self):
        sibling, manifest = self.add_independent_member(2)
        self.admit(3)
        with self.store.write() as db:
            db.execute("INSERT INTO issue_dependencies(id,issue_id,requirements_revision_id,prerequisite_issue_id,source,evidence,created_at) VALUES('edge','issue-1',?,'issue-3','requirements','Requires prerequisite',100)", (self.admission['revision_id'],))
        view = self.view()
        self.assertIsNone(view['current_blocker'])
        dependent = next(m for m in view['members'] if m['id'] == self.member)
        self.assertEqual(dependent['current_blocker']['code'], 'dependency')
        self.assertEqual(dependent['projected_state'], 'blocked')
        with self.assertRaisesRegex(TransitionError, 'applicable gate'):
            self.reserve()
        self.reserve(sibling, manifest)
        with self.store.write() as db:
            db.execute("INSERT INTO member_dependencies(unit_id,member_id,prerequisite_member_id,membership_generation) VALUES('unit-1',?,?,1)", (sibling, self.member))
        self.assertEqual(self.view()['current_blocker']['code'], 'dependency')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)

    def test_third_failure_atomically_fences_other_assignments_and_preserves_uncharged_late_failure(self):
        parts = [(self.member, self.manifest)] + [self.add_independent_member(n) for n in (2, 3, 4)]
        jobs = [self.reserve(member, manifest, slot=f'slot-{n}') for n, (member, manifest) in enumerate(parts, 1)]
        for n in range(3):
            result = self.fail(jobs[n], parts[n][1], slot=f'slot-{n+1}')
            self.assertTrue(result['charged'])
        self.assertEqual(self.view()['charged_failures'], 3)
        self.assertEqual(self.view()['current_blocker']['code'], 'budget_exhausted')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs WHERE fenced = 1').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0], 4)
        result = self.fail(jobs[3], parts[3][1], slot='slot-4')
        self.assertFalse(result['accepted'])
        self.assertFalse(result['charged'])
        self.assertEqual(self.view()['charged_failures'], 3)
        with self.store.read_snapshot() as db:
            raw = db.execute("SELECT content FROM job_artifacts WHERE job_id = ?", (jobs[3]['job_id'],)).fetchone()[0]
            self.assertEqual(json.loads(raw), {'diagnosis': 'code_failure'})
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 3)
        for n, job in enumerate(jobs, 1):
            self.cleanup(job, slot=f'slot-{n}')
        with self.assertRaises(TransitionError):
            self.reserve()

    def test_concurrent_third_failures_serialize_and_retry_preserves_fourth_raw_result(self):
        parts = [(self.member, self.manifest)] + [self.add_independent_member(n) for n in (2, 3, 4)]
        jobs = [self.reserve(member, manifest, slot=f'slot-{n}') for n, (member, manifest) in enumerate(parts, 1)]
        for n in range(2):
            self.fail(jobs[n], parts[n][1], slot=f'slot-{n+1}')
        version = self.view()['version']
        barrier = Barrier(2)
        def submit(n):
            barrier.wait()
            try:
                return self.lifecycle.record_failure(Command(f'race-{n}', 'operator', 'unit-1', version), jobs[n]['job_id'],
                    execution_fence=f'fence-slot-{n+1}', input_manifest=parts[n][1], evidence={'failure': n})
            except VersionConflict:
                return 'conflict'
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, (2, 3)))
        self.assertEqual(outcomes.count('conflict'), 1)
        late = 2 + outcomes.index('conflict')
        result = self.lifecycle.record_failure(self.command(), jobs[late]['job_id'], execution_fence=f'fence-slot-{late+1}',
                    input_manifest=parts[late][1], evidence={'failure': late})
        self.assertFalse(result['charged'])
        self.assertEqual(self.view()['charged_failures'], 3)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_artifacts').fetchone()[0], 4)

    def test_resuming_paused_worker_keeps_cleanup_blocker_and_atomic_event_history(self):
        job = self.reserve()
        pause = self.command()
        self.lifecycle.pause(pause)
        self.lifecycle.resume(self.command())
        self.assertEqual(self.view()['current_blocker']['code'], 'cleanup_pending')
        with self.assertRaises(TransitionError):
            self.reserve()
        self.cleanup(job)
        self.assertIsNone(self.view()['current_blocker'])
        self.reserve()
        with self.store.read_snapshot() as db:
            event = db.execute("SELECT payload_json FROM events WHERE transaction_id = ? AND kind = 'lifecycle_changed'", (pause.request_id,)).fetchone()
            payload = json.loads(event[0])
            self.assertEqual(payload['before']['stage_code'], 'building')
            self.assertIsNone(payload['before']['current_blocker'])
            self.assertEqual(payload['after']['current_blocker']['code'], 'operator_pause')

    def test_charge_correction_append_only_no_double_charge_and_no_implicit_pause_resume(self):
        job = self.reserve()
        self.fail(job)
        self.lifecycle.pause(self.command())
        with self.store.read_snapshot() as db:
            charge = db.execute("SELECT id FROM budget_entries WHERE kind = 'charge'").fetchone()[0]
        command = self.command()
        result = self.lifecycle.correct_charge(command, charge, evidence={'environment_verified': True})
        self.assertEqual(result, self.lifecycle.correct_charge(command, charge, evidence={'environment_verified': True}))
        self.assertEqual(self.view()['charged_failures'], 0)
        self.assertEqual(self.view()['current_blocker']['code'], 'operator_pause')
        with self.assertRaises(sqlite3.IntegrityError):
            self.lifecycle.correct_charge(self.command(), charge, evidence={'environment_verified': True})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 2)

    def test_completed_external_closure_and_hold_label_resolution_keep_pause(self):
        gate = self.lifecycle.open_gate(self.command(), 'label_hold', details={'label': 'hold'})
        self.lifecycle.pause(self.command())
        self.lifecycle.resolve_gate(self.command(), gate['gate_id'], evidence={'observed_labels': [], 'observed_at': 101})
        self.assertEqual(self.view()['current_blocker']['code'], 'operator_pause')
        with self.store.write() as db:
            db.execute("UPDATE issues SET github_state = 'closed', state_reason = 'completed' WHERE id = 'issue-1'")
        self.assertEqual(self.view()['members'][0]['projected_state'], 'done')


if __name__ == '__main__':
    unittest.main()
