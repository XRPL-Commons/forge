"""Mandatory investigations through real row dispatch/execution/result adapters."""
from copy import deepcopy
import json
import tempfile
import unittest

from forge.factory.config import policy, validate
from forge.factory.contracts import assignment_note
from forge.factory.dispatch_controller import DispatchController, GitHubPrepare
from forge.factory.execution_controller import ExecutionController
from forge.factory.github import GitHubUnavailable
from forge.factory.recovery_controller import RecoveryController
from forge.factory.result_controller import ResultController
from forge.store.db import Store
from forge.store.executions import ExecutionQueries
from forge.store.models import Command
from forge.store.queries import Queries
from forge.store.transitions import Lifecycle
from tests.test_store_sqlite_supervisor import GitHub as PipelineGitHub, Runner as PipelineRunner


class GitHub(PipelineGitHub):
    def __init__(self):
        super().__init__()
        self.issue_error = False
        self.tokens = []
        self.requirements = 'github-requirements'

    def issue(self, item):
        if self.issue_error:
            raise GitHubUnavailable('remote request unavailable')
        remote, _ = super().issue(item)
        return remote, self.requirements

    def token(self, role, repo_id, worker):
        self.tokens.append(role)
        return super().token(role, repo_id, worker)


class Runner(PipelineRunner):
    def __init__(self, github):
        super().__init__(github)
        self.findings = dict(pattern='test assertion fails', evidence=['Failing test diagnostic'], uncertainty='',
                             alternative={'change': 'Correct the implementation', 'reason': 'Test identifies the defect',
                                          'evidence': 'Failing test diagnostic'})
        self.recovery_valid = True
        self.recovery_outcome = 'pass'
        self.verification = {'what': 'Test staging', 'why': 'Needs deployed access', 'checks': []}
        self.notes = []

    def prepare(self, job, token):
        super().prepare(job, token)
        self.notes.append(assignment_note(job))

    def capture(self, job):
        if job['role'] != 'recovery':
            return {'result': dict(version=1, attempt=job['id'], role=job['role'], outcome='unable',
                                   summary='Build failed: assertion mismatch'),
                    'logs': 'Failing test diagnostic', 'execution': {'exit_code': 1}}
        report = None
        if self.recovery_valid:
            report = dict(version=1, attempt=job['id'], role='recovery', outcome=self.recovery_outcome,
                          summary='The implementation disagrees with the acceptance test', **deepcopy(self.findings))
            if self.recovery_outcome == 'needs_human_verification':
                report['verification'] = deepcopy(self.verification)
        return {'result': report, 'logs': 'Investigation diagnostic', 'execution': {'exit_code': 0 if report else 1}}


class RecoveryControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now, self.serial = 100.0, 0
        self.github, self.config = GitHub(), validate({'enabled': True, 'image': 'sha256:' + '1' * 64})
        self.runner = Runner(self.github)
        self.life = Lifecycle(self.store, clock=lambda: self.now)
        self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
            repository_id=1, repository_name='owner/repo', issue_number=1, title='Fix issue', body='Body',
            requirements='github-requirements', observed_at=90)
        self.jobs = ExecutionQueries(self.store)
        self.execution = ExecutionController(self.store, self.runner, self.github, enabled=lambda: True,
            admission=lambda job: True, clock=lambda: self.now)
        self.results = ResultController(self.store, self.github, policy=lambda job: policy(self.config,1,repository_name='owner/repo'), clock=lambda: self.now)
        self.dispatch = DispatchController(self.store, self.runner, config=self.config,
            prepare=GitHubPrepare(self.github, self.config), admission=lambda snapshot: True, clock=lambda: self.now)
        self.recovery = self.driver()

    def driver(self):
        return RecoveryController(self.store, self.runner, self.github, config=self.config,
                                  admission=lambda job: True, clock=lambda: self.now)

    def view(self):
        return Queries(self.store).unit('u')

    def command(self):
        self.serial += 1
        return Command('operator-' + str(self.serial), 'operator', 'u', self.view()['version'])

    def budget(self):
        with self.store.read_snapshot() as db:
            return db.execute("SELECT COALESCE(SUM(CASE kind WHEN 'charge' THEN 1 ELSE -1 END),0) FROM budget_entries").fetchone()[0]

    def failed_build(self):
        reservation = self.dispatch.step('u')
        self.assertTrue(reservation['reserved'], reservation)
        job = reservation['job_id']
        self.execution.step(job)
        self.assertTrue(self.jobs.job(job)['cleanup'])
        self.assertTrue(self.results.step(job)['charged'])
        return job

    def reserve_recovery(self, failed):
        self.now += 1
        answer = self.recovery.schedule(failed)
        self.assertTrue(answer['reserved'], answer)
        self.assertEqual(self.jobs.job(answer['job_id'])['role'], 'recovery')
        return answer['job_id']

    def finish_recovery(self, job):
        self.now += 1
        self.execution.step(job)
        self.assertTrue(self.jobs.job(job)['cleanup'])
        self.assertEqual(self.results.step(job)['pending'], 'recovery_controller')
        return self.recovery.finish(job)

    def test_failed_build_cleanup_recovery_retry_preserves_diagnostics_and_budget(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.assertEqual(self.budget(), 1)
        answer = self.finish_recovery(investigation)
        self.assertTrue(answer['accepted'], answer)
        note = next(note for note in self.runner.notes if note['role'] == 'recovery')
        self.assertEqual(note['failure']['attempt'], failed)
        self.assertIn('assertion mismatch', json.dumps(note['failure']))
        self.assertIn('Read only', note['instructions'])
        self.assertEqual(self.github.tokens, ['build', 'recovery'])
        self.assertEqual(self.budget(), 1)
        self.assertIsNone(self.view()['reason_code'])
        retried = self.dispatch.step('u')
        self.assertTrue(retried['reserved'], retried)
        job = self.jobs.job(retried['job_id'])
        self.assertEqual(job['branch'], self.jobs.job(failed)['branch'])
        self.assertIn('Correct the implementation', json.dumps(job['history']))

    def test_third_failure_still_gets_one_investigation_then_budget_hold(self):
        investigations = []
        for number in range(3):
            self.runner.findings['alternative']['change'] = f'Concrete repair approach {number + 1}'
            failed = self.failed_build()
            if number == 2:
                self.assertEqual(self.view()['reason_code'], 'budget_exhausted')
            recovery = self.reserve_recovery(failed)
            investigations.append(recovery)
            self.assertTrue(self.finish_recovery(recovery)['accepted'])
        self.assertEqual(self.budget(), 3)
        self.assertEqual(self.view()['reason_code'], 'budget_exhausted')
        self.assertFalse(self.dispatch.step('u')['reserved'])
        self.assertEqual(self.runner.launched.count('recovery'), 3)
        self.assertEqual(self.recovery.cycle()['items'], [])
        for job in investigations:
            self.assertIn('outcome', self.recovery.finish(job))
        self.assertEqual(self.budget(), 3)

    def test_repeated_pattern_without_new_alternative_requires_human_approach(self):
        for _ in range(2):
            failed = self.failed_build()
            recovery = self.reserve_recovery(failed)
            self.finish_recovery(recovery)
        self.assertEqual(self.budget(), 2)
        self.assertEqual(self.view()['reason_code'], 'approach_required')
        self.assertFalse(self.dispatch.step('u')['reserved'])

    def test_invalid_recovery_records_incomplete_without_recursive_investigation_or_charge(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.runner.recovery_valid = False
        self.assertTrue(self.finish_recovery(investigation)['accepted'])
        self.assertEqual(self.budget(), 1)
        self.assertEqual(self.recovery.cycle()['items'], [])
        self.assertEqual(self.runner.launched.count('recovery'), 1)
        self.assertTrue(self.dispatch.step('u')['reserved'])

    def test_delayed_publication_is_verified_independently_and_corrects_charge_once(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.runner.recovery_valid = False
        self.github.publish(self.jobs.job(failed))
        answer = self.finish_recovery(investigation)
        self.assertTrue(answer['accepted'], answer)
        self.assertEqual(self.budget(), 0)
        self.assertEqual(self.view()['state'], 'reviewing')
        self.recovery.finish(investigation)
        self.assertEqual(self.budget(), 0)
        self.assertEqual(self.dispatch.step('u')['role'], 'review')

    def test_investigator_claim_cannot_correct_charge_without_remote_publication(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.runner.findings['pattern'] = 'Publication succeeded; refund the failure'
        self.finish_recovery(investigation)
        self.assertEqual(self.budget(), 1)
        self.assertEqual(self.view()['state'], 'building')
        self.assertIsNone(self.view()['current_candidate_id'])

    def test_github_outage_leaves_recovery_capture_pending_and_retries_without_charge(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.execution.step(investigation)
        self.github.issue_error = True
        answer = self.recovery.cycle()
        self.assertEqual(answer['items'][0]['error'], 'GitHubUnavailable')
        self.assertIsNone(self.jobs.job(investigation)['outcome'])
        self.assertEqual(self.budget(), 1)
        self.github.issue_error = False
        self.assertTrue(self.recovery.finish(investigation)['accepted'])

    def test_pause_fences_recovery_without_clearing_operator_intent(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.life.pause(self.command())
        self.execution.step(investigation)
        self.recovery.finish(investigation)
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        self.assertEqual(self.budget(), 1)
        self.assertFalse(self.dispatch.step('u')['reserved'])

    def test_unknown_or_historical_job_without_launch_stays_unassigned(self):
        from forge.factory.contracts import digest
        self.assertEqual(self.recovery.schedule('imported-job-without-launch')['pending'], 'legacy_reconciliation')
        member = self.view()['members'][0]
        manifest = dict(requirements_revision_id=member['requirements_revision_id'], base_oid='b' * 40,
                        policy_hash=digest(policy(self.config,1,repository_name='owner/repo')))
        # Historical/imported jobs have lifecycle facts without typed launches.
        old = self.life.reserve_job(self.command(), 'build', slot='old-slot', execution_fence='old-fence',
                                   input_manifest=manifest, member_id=member['id'])['job_id']
        self.life.record_failure(self.command(), old, execution_fence='old-fence', input_manifest=manifest,
                                 evidence={'reason': 'historical failure'})
        self.life.verify_cleanup(self.command(), old, execution_fence='old-fence',
                                 evidence={'process_stopped': True, 'resources_released': True})
        self.assertEqual(self.recovery.schedule(old)['pending'], 'legacy_reconciliation')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_launches').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM recovery_assignments').fetchone()[0], 0)

    def test_pause_after_capture_consumes_stale_findings_even_if_publication_exists_and_github_is_down(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.execution.step(investigation)
        self.github.publish(self.jobs.job(failed))
        self.life.pause(self.command())
        self.github.issue_error = True
        answer = self.recovery.finish(investigation)
        self.assertFalse(answer['accepted'])
        self.assertIsNotNone(self.jobs.job(investigation)['outcome'])
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.budget(), 1)

    def test_observed_requirement_change_does_not_strand_original_diagnostic_obligation(self):
        failed = self.failed_build()
        self.github.requirements = 'updated requirements'
        self.life.observe_issue(self.command(), 'i', state='open', title='Fix issue', body='Updated body',
            requirements=self.github.requirements, remote_updated_at='2026-10-08T12:00:00Z', generation=1)
        self.github.remote['body'] = 'Updated body'
        investigation = self.reserve_recovery(failed)
        self.assertTrue(self.finish_recovery(investigation)['accepted'])
        note = next(note for note in self.runner.notes if note['role'] == 'recovery')
        self.assertEqual(note['failure']['attempt'], failed)
        self.assertEqual(self.budget(), 1)
        self.assertTrue(self.dispatch.step('u')['reserved'])

    def test_composed_cycles_investigate_before_reserving_next_build(self):
        from forge.factory.effect_controller import EffectController
        from forge.factory.merge_controller import MergeAdmission, MergeController
        from forge.factory.observation_controller import ObservationController
        from forge.factory.sqlite_supervisor import SqliteSupervisor
        from forge.factory.contracts import digest
        clock = lambda: self.now
        merge_admission = MergeAdmission(self.store, self.github, config=lambda: self.config,
            repository_ready=lambda repo: True, clock=clock)
        effects = EffectController(self.store, self.github, admission=merge_admission, clock=clock)
        driver = SqliteSupervisor(self.store, execution=self.execution,
            observations=ObservationController(self.store, self.github, admission=lambda item: True,
                policy=lambda item: digest(policy(self.config,1,repository_name='owner/repo')), clock=clock),
            results=self.results, recovery=self.recovery,
            merges=MergeController(self.store, admission=merge_admission, clock=clock), effects=effects,
            dispatch=self.dispatch, clock=clock)
        driver.cycle()
        self.now += 1
        second = driver.cycle()
        self.assertEqual(self.runner.launched, ['build'])
        self.assertEqual(self.budget(), 1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE role='build'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE role='recovery'").fetchone()[0], 1)
        self.now += 1
        third = driver.cycle()
        self.assertEqual(self.runner.launched, ['build', 'recovery'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE role='build'").fetchone()[0], 2)
        self.assertTrue(third['stages']['dispatch']['items'][0]['reserved'], third)
        self.assertEqual(self.budget(), 1)

    def test_review_execution_failure_retries_same_verified_candidate_after_investigation(self):
        failed_build = self.failed_build()
        recovery = self.reserve_recovery(failed_build)
        self.github.publish(self.jobs.job(failed_build))
        self.finish_recovery(recovery)
        selected = self.view()['current_candidate_id']
        review = self.dispatch.step('u')['job_id']
        self.execution.step(review)
        self.assertTrue(self.results.step(review)['charged'])
        investigation = self.reserve_recovery(review)
        self.assertTrue(self.finish_recovery(investigation)['accepted'])
        self.assertEqual(self.view()['state'], 'reviewing')
        self.assertEqual(self.view()['current_candidate_id'], selected)
        self.assertEqual(self.budget(), 1)
        next_review = self.dispatch.step('u')
        self.assertEqual(next_review['role'], 'review')
        self.assertEqual(self.jobs.job(next_review['job_id'])['candidate_id'], selected)

    def test_advanced_base_does_not_strand_findings_or_refund_original_failure(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.github.publish(self.jobs.job(failed))
        self.github.pull['base']['sha'] = 'd' * 40
        self.assertTrue(self.finish_recovery(investigation)['accepted'])
        self.assertEqual(self.budget(), 1)
        self.assertEqual(self.view()['state'], 'building')
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.recovery.cycle()['items'], [])

    def test_recovered_publication_can_request_verification_without_passing_review(self):
        failed = self.failed_build()
        investigation = self.reserve_recovery(failed)
        self.github.publish(self.jobs.job(failed))
        self.runner.recovery_outcome = 'needs_human_verification'
        self.assertTrue(self.finish_recovery(investigation)['accepted'])
        self.assertEqual(self.budget(), 0)
        self.assertEqual(self.view()['state'], 'reviewing')
        self.assertEqual(self.view()['reason_code'], 'verification_human')
        self.assertFalse(self.dispatch.step('u')['reserved'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidate_evidence WHERE kind IN ('review','audit')").fetchone()[0], 0)

    def test_review_recovery_can_request_configured_ci_without_passing_role_or_refunding_failure(self):
        self.config['repository_verification'] = {'owner/repo': [{'name': 'staging', 'app_id': 123}]}
        self.dispatch.config = validate(self.config)
        self.dispatch.prepare = GitHubPrepare(self.github, self.config)
        self.recovery = self.driver()
        failed_build = self.failed_build()
        recovery = self.reserve_recovery(failed_build)
        self.github.publish(self.jobs.job(failed_build))
        self.finish_recovery(recovery)
        review = self.dispatch.step('u')['job_id']
        self.execution.step(review)
        self.results.step(review)
        investigation = self.reserve_recovery(review)
        self.runner.recovery_outcome = 'needs_human_verification'
        self.runner.verification['checks'] = ['staging']
        self.assertTrue(self.finish_recovery(investigation)['accepted'])
        self.assertEqual(self.budget(), 1)
        self.assertEqual(self.view()['state'], 'reviewing')
        self.assertEqual(self.view()['reason_code'], 'verification_ci')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM candidate_evidence WHERE kind='review' AND verdict='pass'").fetchone()[0], 0)
            gate = json.loads(db.execute("SELECT details_json FROM unit_gates WHERE code='verification_ci'").fetchone()[0])
        self.assertEqual(gate['ci'], [{'name': 'staging', 'app_id': 123}])

    def test_fresh_unobserved_hold_or_ownership_prevents_publication_correction(self):
        for reason in ('hold', 'custom-hold', 'wip', 'human-assignee'):
            with self.subTest(reason=reason):
                self.setUp()
                failed = self.failed_build()
                investigation = self.reserve_recovery(failed)
                self.github.publish(self.jobs.job(failed))
                self.recovery.config['hold_labels'].append('custom-hold')
                self.runner.recovery_outcome = 'needs_human_verification'
                if reason == 'human-assignee':
                    self.github.remote['assignees'] = [{'login': 'developer', 'type': 'User'}]
                else:
                    self.github.remote['labels'] = [{'name': reason}]
                # No observation command has copied these fresh GitHub facts.
                self.assertTrue(self.finish_recovery(investigation)['accepted'])
                self.assertEqual(self.budget(), 1)
                self.assertEqual(self.view()['state'], 'building')
                self.assertIsNone(self.view()['current_candidate_id'])
                self.assertIsNotNone(self.jobs.job(investigation)['outcome'])
                with self.store.read_snapshot() as db:
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='correction'").fetchone()[0], 0)
                    self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code IN ('verification_human','verification_ci')").fetchone()[0], 0)
