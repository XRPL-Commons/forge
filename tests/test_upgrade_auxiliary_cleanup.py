from copy import deepcopy
from pathlib import Path
import tempfile
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.deployments import Deployments
from forge.factory.repository_planner import RepositoryPlanner
from forge.factory.runner import ContainerRunner, RunnerError
from forge.store.configuration import Configuration
from forge.store.migration.auxiliary_cleanup import cleanup_unstarted_auxiliary
from forge.store.repository_planning import RepositoryPlanning
from forge.store.transitions import Lifecycle, manifest_hash
from tests.test_deployments import selected_store


class UpgradeAuxiliaryCleanupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name); self.store = selected_store(self.root)
        Configuration(self.store).replace('runtime.json', {'enabled': False}, expected_revision=0)
        self.deployments = Deployments(self.root, runner=None, github=None, settings=None, clock=lambda: 100)
        self.job = self.deployments.new_job({}, {'id': 1, 'name': 'owner/repo'}, 'a'*40, 1000)
        self.deployments.save(1, 'owner/repo', job=self.job, status='running')
        self.runner = Mock()
        self.runner.inspect.return_value = {'id': 'exact', 'status': 'created', 'running': False, 'started_at': None}
        self.patch = patch('forge.factory.runner.ContainerRunner', return_value=self.runner)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def row(self):
        with self.store.read_snapshot() as db:
            return dict(db.execute('SELECT * FROM auxiliary_executions WHERE id=?', (self.job['id'],)).fetchone())

    def leases(self):
        with self.store.read_snapshot() as db:
            return db.execute('SELECT count(*) FROM auxiliary_leases').fetchone()[0]

    def triage(self):
        with self.store.write() as db:
            db.execute('UPDATE repositories SET enabled=1 WHERE id=1')
        Lifecycle(self.store, clock=lambda: 100).admit_issue(request_id='admit', actor_id='operator', unit_id='unit',
            issue_id='1:1', repository_id=1, repository_name='owner/repo', issue_number=1,
            title='Work', body='Details', requirements='digest', observed_at=90)
        plans = RepositoryPlanning(self.store, clock=lambda: 100)
        run = plans.queue(1, request_id='triage', actor_id='operator', base_branch='main')
        context = plans.context(run['proposal_id'], head='a'*40)
        job = dict(id='triage', proposal_id=run['proposal_id'], slot='repository-triage', role='triage',
            repo_id=1, repo='owner/repo', head='a'*40, deadline=1900, fenced=False, cleanup=False,
            runner_config={}, context_hash=manifest_hash(context))
        plans.reserve(run['proposal_id'], job, context)
        return plans, job

    def test_created_worker_relinquishes_verified_ownership_without_changing_manifest(self):
        with self.store.read_snapshot() as db:
            before = db.execute('SELECT manifest_json FROM auxiliary_runtime').fetchone()[0]
        def reset(job):
            self.assertTrue(self.row()['fenced'])
            self.assertEqual(self.leases(), 1)
            job['source_input'] = 'runner observation must not change launch manifest'
        self.runner.reset.side_effect = reset
        self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 1)
        self.assertEqual(self.leases(), 0)
        self.assertEqual((self.row()['fenced'], self.row()['cleanup']), (1, 1))
        entry = self.deployments.read()['repositories']['1']
        self.assertEqual(entry['reason'], 'maintenance_interrupted')
        self.assertTrue(entry['reread_requested'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT manifest_json FROM auxiliary_runtime').fetchone()[0], before)
        self.runner.start.assert_not_called(); self.runner.prepare.assert_not_called(); self.runner.create.assert_not_called()
        self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 0)

    def test_absent_never_created_worker_is_released_but_recorded_missing_worker_is_not(self):
        self.runner.inspect.return_value = None
        self.job['container_id'] = 'previously-observed'
        self.deployments.save(1, job=self.job)
        self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 0)
        self.runner.reset.assert_not_called()
        self.job.pop('container_id'); self.deployments.save(1, job=self.job)
        self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 1)

    def test_running_exited_and_ambiguous_workers_stay_owned(self):
        for current in ({'status': 'running', 'running': True, 'started_at': 10},
                        {'status': 'exited', 'running': False, 'started_at': 10},
                        {'status': 'created', 'running': False, 'started_at': 10},
                        {'status': 'created', 'running': False}):
            with self.subTest(current=current):
                self.runner.inspect.return_value = current
                self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 0)
                self.assertEqual((self.row()['fenced'], self.leases()), (0, 1))
        self.runner.reset.assert_not_called()
        self.runner.inspect.side_effect = RunnerError('ownership changed')
        with self.assertRaises(RunnerError): cleanup_unstarted_auxiliary(self.root, self.store)
        self.assertFalse(self.row()['fenced'])

    def test_reset_failure_keeps_fence_and_lease_then_replays_teardown(self):
        self.job['container_id'] = 'exact'; self.deployments.save(1, job=self.job)
        self.runner.reset.side_effect = RunnerError('cleanup unconfirmed')
        with self.assertRaises(RunnerError): cleanup_unstarted_auxiliary(self.root, self.store)
        self.assertEqual((self.row()['fenced'], self.row()['cleanup'], self.leases()), (1, 0, 1))
        self.runner.reset.side_effect = None; self.runner.inspect.return_value = None
        self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 1)
        self.assertEqual(self.leases(), 0)

    def test_real_runner_requires_exact_owned_container_removal_before_releasing_lease(self):
        self.patch.stop()
        runner = ContainerRunner({}, environ={})
        name = 'forge-job-' + self.job['id']
        physical = {'Id': 'c'*64, 'Name': '/' + name,
            'Config': {'Labels': {'forge.execution': self.job['id'], 'forge.slot': self.job['slot']}},
            'State': {'Status': 'created', 'Running': False, 'ExitCode': 0,
                      'StartedAt': '0001-01-01T00:00:00Z', 'FinishedAt': '0001-01-01T00:00:00Z'}}
        remaining = [physical]
        remove = False
        def call(args, **kwargs):
            if args[:2] == ['container', 'ls']:
                return SimpleNamespace(stdout=(json.dumps({'Names': name, 'ID': 'c'*64}).encode() if remaining else b''))
            if args[:2] == ['container', 'inspect']:
                return SimpleNamespace(stdout=json.dumps(remaining).encode())
            if args[0] == 'rm':
                if remove: remaining.clear()
                return SimpleNamespace(stdout=b'')
            if args[0] == 'info': return SimpleNamespace(stdout=b'29')
            raise AssertionError(args)
        runner.call = call
        with patch('forge.factory.runner.ContainerRunner', return_value=runner):
            physical['Config']['Labels']['forge.execution'] = 'foreign'
            with self.assertRaisesRegex(RunnerError, 'ownership'):
                cleanup_unstarted_auxiliary(self.root, self.store)
            self.assertFalse(self.row()['fenced'])
            physical['Config']['Labels']['forge.execution'] = self.job['id']
            with self.assertRaisesRegex(RunnerError, 'Teardown remains unverified'):
                cleanup_unstarted_auxiliary(self.root, self.store)
            self.assertEqual((self.row()['cleanup'], self.leases()), (0, 1))
            remove = True
            self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 1)
        self.assertFalse(remaining)
        self.assertEqual((self.row()['cleanup'], self.leases()), (1, 0))

    def test_admission_enabled_and_old_schema_never_touch_workers(self):
        Configuration(self.store).replace('runtime.json', {'enabled': True}, expected_revision=1)
        self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 0)
        with patch('forge.store.migration.auxiliary_cleanup.schema.inspect', return_value={'current_schema': 39, 'target_schema': 40}):
            self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 0)
        self.runner.inspect.assert_not_called()

    def test_triage_interruption_keeps_context_history_without_model_failure(self):
        plans, job = self.triage()
        before = deepcopy(plans.job('triage', context=True))
        self.runner.inspect.return_value = None
        self.assertEqual(cleanup_unstarted_auxiliary(self.root, self.store), 2)
        saved = plans.job('triage', context=True)
        self.assertEqual(saved['context'], before['context'])
        self.assertTrue(saved['cleanup']); self.assertTrue(saved['fenced'])
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT status,error FROM repository_triage_runs').fetchone()
            self.assertEqual(tuple(row), ('failed', 'maintenance_interrupted'))
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='lifecycle_changed' AND json_extract(payload_json,'$.reason')='maintenance_interrupted'").fetchone()[0], 1)
        self.assertEqual(plans.invocation_count(job['proposal_id']), 1)
        self.assertEqual(self.leases(), 0)

    def test_planner_finishes_interrupted_teardown_without_fault_classification(self):
        plans, job = self.triage()
        plans.interrupt_for_maintenance('triage')
        planner = RepositoryPlanner(self.store, Mock(), config={'enabled': False}, settings=Mock(),
            observations=Mock(), runner=lambda config: self.runner)
        planner.problems = Mock()
        self.assertEqual(planner.drive(job)['phase'], 'interrupted')
        planner.problems.record.assert_not_called()
        self.runner.start.assert_not_called()
        self.assertTrue(plans.job('triage')['cleanup'])
        with self.store.read_snapshot() as db:
            self.assertEqual(tuple(db.execute('SELECT status,error FROM repository_triage_runs').fetchone()),
                             ('failed', 'maintenance_interrupted'))
