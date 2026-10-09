from dataclasses import replace
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.contracts import assignment_note
from forge.factory.execution_controller import ExecutionController
from forge.store.artifacts import Artifacts
from forge.store.db import Store, StoreError
from forge.store.executions import Executions, ExecutionQueries, Launch
from forge.store.models import Candidate, Command, IdempotencyConflict, TransitionError
from forge.store.transitions import manifest_hash, project_unit


class Engine:
    def __init__(self):
        self.container = None
        self.creates = self.starts = self.resets = 0
        self.after_create = self.after_start = self.after_reset = None
        self.receipt = {'container_absent': True, 'capacity_ready': True}

    def inspect(self, job):
        return self.container

    def create(self, job):
        self.creates += 1
        self.container = {'id': 'container', 'status': 'created', 'running': False}
        if self.after_create:
            self.after_create()
        return 'container'

    def prepare(self, job, token):
        assert token == 'ephemeral-token'
        assert assignment_note(job)['id'] == job['id']

    def start(self, job):
        self.starts += 1
        self.container.update(status='running', running=True)
        if self.after_start:
            self.after_start()

    def capture(self, job):
        return {'result': {'summary': 'worker claim'}, 'logs': 'bounded log', 'missing': []}

    def reset(self, job):
        assert job['fenced']
        self.resets += 1
        self.container = None
        if self.after_reset:
            self.after_reset()
        return self.receipt


class GitHub:
    def token(self, role, repo_id, worker):
        return 'ephemeral-token'


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now = 100.0
        self.life = Executions(self.store, clock=lambda: self.now)
        self.queries = ExecutionQueries(self.store)
        self.serial = 0
        admitted = self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
            repository_id=1, repository_name='owner/repo', issue_number=1, title='Issue', body='Body',
            requirements={'acceptance': ['works']}, observed_at=90)
        self.member = admitted['member_id']
        self.manifest = dict(requirements_revision_id=admitted['revision_id'], base_oid='base', policy_hash='policy')
        self.launch = Launch('forge/issue-1', 'builder[bot]', 'codex', 200.0, {}, {})
        self.engine = Engine()
        self.controller = self.driver()

    def driver(self):
        return ExecutionController(self.store, self.engine, GitHub(), enabled=lambda: True, admission=lambda job: True, clock=lambda: self.now)

    def view(self):
        with self.store.read_snapshot() as db:
            return project_unit(db, 'u')

    def command(self):
        self.serial += 1
        return Command(str(self.serial), 'operator', 'u', self.view()['version'])

    def reserve(self, command=None, launch=None):
        return self.life.reserve_job(command or self.command(), 'build', slot='slot', execution_fence='fence',
                                     input_manifest=self.manifest, member_id=self.member, launch=launch or self.launch)['job_id']

    def collect(self, job):
        return self.life.collect(self.command(), job, execution_fence='fence', capture=self.engine.capture({}))

    def cleanup(self, job):
        return self.life.verify_cleanup(self.command(), job, execution_fence='fence',
                                       evidence={'process_stopped': True, 'resources_released': True})

    def accept(self, job, fence='fence'):
        candidate = Candidate(1, 'forge/issue-1', 'head', 'base', 'policy',
                              manifest_hash({'acceptance': ['works']}), manifest_hash(self.manifest))
        return self.life.finish_job(self.command(), job, execution_fence=fence, input_manifest=self.manifest,
            evidence={'head_oid': 'head', 'base_oid': 'base', 'pull_number': 1}, candidate=candidate)

    def test_live_observation_records_engine_time_and_refreshes_without_changing_assignment(self):
        from forge.factory.store_views import PipelineViews
        job = self.reserve()
        views = PipelineViews(self.store, clock=lambda: self.now)
        self.assertIsNone(views.job(job)['observed_at'])
        self.assertIsNone(views.job(job)['started_at'])
        self.engine.after_start = lambda: self.engine.container.update(started_at=97.0)
        self.controller.step(job)
        self.assertEqual((views.job(job)['observed_at'], views.job(job)['observed_status'], views.job(job)['started_at']),
                         (100.0, 'running', 97.0))
        version = self.view()['version']
        self.now = 110.0
        self.controller.step(job)
        self.assertEqual(views.job(job)['observed_at'], 110.0)
        self.assertEqual(self.view()['version'], version)
        self.now = 150.0
        self.engine.inspect = lambda job: (_ for _ in ()).throw(OSError('engine unavailable'))
        with self.assertRaises(OSError): self.controller.step(job)
        self.assertEqual(views.job(job)['observed_at'], 110.0, 'unavailable observations must age normally')

    def test_direct_observation_cannot_mutate_an_import_only_store(self):
        job = self.reserve()
        self.controller.step(job)
        with self.store.write() as db:
            before = dict(db.execute('SELECT * FROM job_execution_state WHERE job_id=?', (job,)).fetchone())
            db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        self.now = 110.0
        with self.assertRaises(StoreError) as denied:
            self.life.observe(job, execution_fence='fence', observation={**self.engine.container, 'status':'paused'})
        self.assertEqual(denied.exception.code, 'runtime_inactive')
        with self.store.read_snapshot() as db:
            self.assertEqual(dict(db.execute('SELECT * FROM job_execution_state WHERE job_id=?', (job,)).fetchone()), before)

    def test_later_serial_inspection_can_have_an_earlier_wall_clock_timestamp(self):
        from forge.factory.store_views import PipelineViews
        job = self.reserve()
        self.controller.step(job)
        version = self.view()['version']
        self.now = 95.0
        self.engine.container.update(status='paused', running=False)
        self.controller.step(job)
        observed = PipelineViews(self.store).job(job)
        self.assertEqual((observed['observed_at'], observed['observed_status']), (95.0, 'paused'))
        self.assertEqual(self.view()['version'], version)

    def test_observation_requires_time_and_status_together(self):
        job = self.reserve()
        self.controller.step(job)
        for assignment in ('observed_at=NULL', 'observed_status=NULL'):
            with self.store.write() as db:
                with self.assertRaisesRegex(sqlite3.IntegrityError, 'both time and status'):
                    db.execute('UPDATE job_execution_state SET '+assignment+' WHERE job_id=?', (job,))
                row = db.execute('SELECT observed_at,observed_status FROM job_execution_state WHERE job_id=?', (job,)).fetchone()
                self.assertEqual(tuple(row), (100.0, 'running'))

    def test_observation_is_lease_and_container_fenced_and_retains_actual_paused_state(self):
        from forge.factory.store_views import PipelineViews
        job = self.reserve()
        self.controller.step(job)
        self.engine.container.update(status='paused', running=False)
        self.now = 105.0
        self.controller.step(job)
        self.assertEqual(PipelineViews(self.store).job(job)['observed_status'], 'paused')
        with self.assertRaises(TransitionError):
            self.life.observe(job, execution_fence='wrong', observation=self.engine.container)
        with self.assertRaises(TransitionError):
            self.life.observe(job, execution_fence='fence', observation={**self.engine.container, 'id':'other'})
        self.collect(job)
        self.cleanup(job)
        with self.assertRaises(TransitionError):
            self.life.observe(job, execution_fence='fence', observation=self.engine.container)

    def test_worker_summary_counts_leases_even_outside_the_requested_issue_page(self):
        from forge.factory.store_views import PipelineViews
        job = self.reserve()
        self.controller.step(job)
        views = PipelineViews(self.store)
        full = views.factory()
        scoped = views.factory(issue='absent')
        self.assertEqual(scoped['jobs'], [])
        self.assertEqual(scoped['summary']['workers'], full['summary']['workers'])
        self.assertEqual([(row['id'],row['slot'],row['observed_status']) for row in scoped['summary']['workers']],
                         [(job, 'slot', 'running')])
        self.assertEqual(scoped['slots'], {'slot': job})

    def test_disabled_runtime_fences_reserved_job_without_starting_it(self):
        job = self.reserve()
        self.controller.enabled = lambda: False
        self.controller.step(job)
        self.assertEqual(self.engine.creates, 0)
        self.assertEqual(self.engine.starts, 0)
        self.assertTrue(self.queries.job(job)['cleanup'])

    def test_launch_and_lease_are_one_transaction_and_idempotency_binds_config(self):
        command = self.command()
        job = self.reserve(command)
        self.assertEqual(job, self.reserve(command))
        with self.assertRaises(IdempotencyConflict):
            self.reserve(command, replace(self.launch, provider='other'))
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_launches').fetchone()[0], 1)
        note = assignment_note(self.queries.job(job))
        self.assertEqual(note['repo'], 'owner/repo')
        self.assertEqual(note['issue_number'], 1)

    def test_invalid_launch_rolls_back_job_attempt_and_lease(self):
        with self.assertRaises(TransitionError):
            self.reserve(launch=replace(self.launch, deadline=99))
        with self.store.read_snapshot() as db:
            for table in ('jobs', 'attempts', 'job_leases', 'job_launches'):
                self.assertEqual(db.execute('SELECT count(*) FROM ' + table).fetchone()[0], 0)
        self.assertEqual(self.view()['state'], 'queued')

    def test_capture_releases_capacity_before_result_validation_without_losing_authority(self):
        job = self.reserve()
        command = self.command()
        first = self.life.collect(command, job, execution_fence='fence', capture=self.engine.capture({}))
        self.assertEqual(first, self.life.collect(command, job, execution_fence='fence', capture=self.engine.capture({})))
        self.cleanup(job)
        self.assertEqual(self.view()['reason_code'], 'result_pending')
        self.assertEqual(self.queries.pending()['items'], [])
        self.assertEqual(self.queries.pending(results=True)['items'], [job])
        with self.assertRaisesRegex(TransitionError, 'gate'):
            self.reserve()
        self.assertTrue(self.accept(job)['accepted'])
        self.assertEqual(self.view()['state'], 'reviewing')
        self.assertIsNone(self.view()['reason_code'])
        self.assertEqual(self.queries.pending(results=True)['items'], [])
        self.assertEqual(self.queries.job(job)['phase'], 'cleaned')

    def test_result_cannot_steal_a_cleaned_jobs_execution_fence(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        with self.assertRaises(TransitionError):
            self.accept(job, fence='another')
        self.assertIsNone(self.view()['current_candidate_id'])

    def test_operator_pause_after_cleanup_supersedes_saved_result(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        self.life.pause(self.command())
        self.assertFalse(self.accept(job)['accepted'])
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        self.assertEqual(self.view()['charged_failures'], 0)

    def test_failure_after_cleanup_charges_once_and_preserves_recovery_gate(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        command = self.command()
        kwargs = dict(execution_fence='fence', input_manifest=self.manifest, evidence={'diagnosis': 'code'})
        first = self.life.record_failure(command, job, **kwargs)
        self.assertEqual(first, self.life.record_failure(command, job, **kwargs))
        self.assertEqual(self.view()['charged_failures'], 1)
        self.assertEqual(self.view()['reason_code'], 'recovery_pending')

    def test_launch_and_capture_authority_are_immutable_in_sql(self):
        job = self.reserve()
        self.collect(job)
        with self.store.write() as db:
            for sql in ("UPDATE job_launches SET branch='other'", 'DELETE FROM job_launches',
                        'UPDATE job_execution_state SET capture_authorized=0',
                        'UPDATE job_execution_state SET captured_at=NULL'):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(sql)

    def test_lists_and_assignments_never_load_artifacts_or_import_archive(self):
        job = self.reserve()
        original = self.store._connect_path
        statements = []
        def connection(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch.object(self.store, '_connect_path', side_effect=connection):
            self.queries.pending()
            self.queries.job(job)
        self.assertFalse(any('job_artifacts' in sql or 'import_records' in sql for sql in statements))

    def test_crash_after_create_resumes_existing_container(self):
        job = self.reserve()
        self.engine.after_create = lambda: (_ for _ in ()).throw(OSError('lost create response'))
        with self.assertRaises(OSError):
            self.controller.step(job)
        self.engine.after_create = None
        self.driver().step(job)
        self.assertEqual((self.engine.creates, self.engine.starts), (1, 1))
        self.assertEqual(self.queries.job(job)['phase'], 'running')

    def test_crash_after_start_does_not_start_twice(self):
        job = self.reserve()
        self.engine.after_start = lambda: (_ for _ in ()).throw(OSError('lost start response'))
        with self.assertRaises(OSError):
            self.controller.step(job)
        self.engine.after_start = None
        self.driver().step(job)
        self.assertEqual(self.engine.starts, 1)

    def test_crash_after_reset_keeps_lease_and_capture_until_verified_retry(self):
        job = self.reserve()
        self.controller.step(job)
        self.engine.container.update(status='exited', running=False)
        self.engine.after_reset = lambda: (_ for _ in ()).throw(OSError('lost reset response'))
        with self.assertRaises(OSError):
            self.controller.step(job)
        self.assertFalse(self.queries.job(job)['cleanup'])
        self.assertEqual(self.queries.capture(job)['result'], {'summary': 'worker claim'})
        self.engine.after_reset = None
        self.driver().step(job)
        self.assertTrue(self.queries.job(job)['cleanup'])
        self.assertTrue(self.accept(job)['accepted'])

    def test_pause_during_create_never_prepares_or_starts_worker(self):
        job = self.reserve()
        self.engine.after_create = lambda: self.life.pause(self.command())
        self.controller.step(job)
        self.assertEqual(self.engine.starts, 0)
        self.assertTrue(self.queries.job(job)['cleanup'])
        self.assertFalse(self.accept(job)['accepted'])

    def test_failed_cleanup_receipt_never_releases_slot(self):
        job = self.reserve()
        self.life.pause(self.command())
        self.engine.receipt = {'container_absent': True, 'capacity_ready': False}
        from forge.factory.runner import RunnerError
        with self.assertRaises(RunnerError):
            self.controller.step(job)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT job_id FROM job_leases').fetchone()[0], job)

    def test_staging_store_cannot_launch_even_with_live_job_fixture(self):
        job = self.reserve()
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        with self.assertRaisesRegex(TransitionError, 'staging'):
            self.controller.step(job)
        self.assertEqual(self.engine.creates, 0)

    def test_raw_logs_remain_pinned_by_pending_validation(self):
        job = self.reserve()
        self.collect(job)
        self.cleanup(job)
        self.assertEqual(Artifacts(self.store).prune_raw_logs(now=100 + 31 * 86400), 0)
        self.assertEqual(Artifacts(self.store).read(job, 'logs').content, b'bounded log')
        with self.store.write() as db, self.assertRaises(sqlite3.IntegrityError):
            db.execute("UPDATE job_artifacts SET content=NULL,expired_at=? WHERE job_id=? AND kind='logs'",
                       (100 + 31 * 86400, job))

    def test_controller_serializes_engine_effects_without_holding_sql_transactions(self):
        job = self.reserve()
        def during_create():
            # A separate write connection works during external I/O.
            with self.store.write() as db:
                db.execute("INSERT INTO store_metadata VALUES('engine-probe','true')")
            with self.assertRaises(BlockingIOError):
                self.driver().step(job)
        self.engine.after_create = during_create
        self.controller.step(job)
        self.assertEqual(self.engine.creates, 1)
        self.assertEqual(self.engine.starts, 1)

    def test_capture_commit_failure_prevents_reset(self):
        job = self.reserve()
        self.controller.step(job)
        self.engine.container.update(status='exited', running=False)
        with patch.object(self.controller.commands, 'collect', side_effect=OSError('commit unavailable')):
            with self.assertRaises(OSError):
                self.controller.step(job)
        self.assertEqual(self.engine.resets, 0)
        self.assertFalse(self.queries.job(job)['cleanup'])
        self.driver().step(job)
        self.assertTrue(self.queries.job(job)['cleanup'])

    def test_credential_token_never_enters_sqlite(self):
        job = self.reserve()
        self.controller.step(job)
        with self.store.read_snapshot() as db:
            self.assertNotIn('ephemeral-token', '\n'.join(db.iterdump()))

    def test_requirements_change_during_launch_keeps_pinned_worker_context_and_rejects_result(self):
        job = self.reserve()
        def changed():
            self.life.observe_issue(self.command(), 'i', state='open', title='Issue', body='New body',
                requirements={'acceptance': ['changed']}, remote_updated_at='2026-10-07T12:00:00Z', generation=1)
        self.engine.after_create = changed
        self.controller.step(job)
        self.assertEqual(self.engine.starts, 0)
        self.assertEqual(self.queries.job(job)['requirements'], {'acceptance': ['works']})
        self.assertFalse(self.accept(job)['accepted'])

    def test_explicit_fence_after_capture_revokes_result_authority(self):
        job = self.reserve()
        self.collect(job)
        self.life.fence_execution(self.command(), job, execution_fence='fence', reason='admission_withdrawn')
        self.cleanup(job)
        self.assertFalse(self.accept(job)['accepted'])

    def test_complete_unit_of_one_with_cleanup_before_each_verdict(self):
        job = self.reserve()
        self.controller.step(job)
        self.engine.container.update(status='exited', running=False)
        self.controller.step(job)
        self.accept(job)
        for role, target in [('review', 'auditing'), ('audit', 'merging')]:
            with self.store.read_snapshot() as db:
                identity = dict(db.execute('SELECT * FROM candidates WHERE id=?', (self.view()['current_candidate_id'],)).fetchone())
            manifest = {key: identity[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
            manifest['candidate_id'] = identity['id']
            job = self.life.reserve_job(self.command(), role, slot='slot', execution_fence='fence-' + role,
                                       input_manifest=manifest, candidate_id=identity['id'], launch=self.launch)['job_id']
            self.controller.step(job)
            self.engine.container.update(status='exited', running=False)
            self.controller.step(job)
            self.life.finish_job(self.command(), job, execution_fence='fence-' + role, input_manifest=manifest,
                                 evidence={'candidate_id': identity['id'], 'verdict': 'pass'})
            self.assertEqual(self.view()['state'], target)
        self.life.confirm_merged(self.command(), identity['id'], merge_sha='merged-sha', evidence={
            'verified': True, 'merged': True, 'merge_sha': 'merged-sha', 'pull_number': 1, 'head_oid': 'head', 'base_oid': 'base'})
        self.assertEqual(self.view()['state'], 'merged')
        self.assertEqual(self.view()['members'][0]['projected_state'], 'done')
        self.assertEqual(self.queries.pending()['items'], [])
        self.assertEqual(self.queries.pending(results=True)['items'], [])


if __name__ == '__main__':
    unittest.main()
