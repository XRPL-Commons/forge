"""Result claims, fresh remote evidence and delayed assignment authority."""
from copy import deepcopy
import json
import tempfile
import unittest

from forge.factory.contracts import candidate, digest
from forge.factory.effect_controller import EffectController
from forge.factory.github import GitHubUnavailable
from forge.factory.result_controller import ResultController
from forge.store.db import Store
from forge.store.executions import Executions, ExecutionQueries, Launch
from forge.store.models import Command, TransitionError, VersionConflict
from forge.store.queries import Queries

HEAD, BASE = 'a' * 40, 'b' * 40


class GitHub:
    def __init__(self):
        self.issue_value = dict(number=1, state='open', body='Body', labels=[])
        self.requirements = 'requirements'
        self.publication_value = None
        self.pr_value = None
        self.after_publication = None
        self.issue_error = False

    def issue(self, item):
        if self.issue_error:
            raise GitHubUnavailable('unavailable')
        return deepcopy(self.issue_value), self.requirements

    def publication(self, job, requirements, policy):
        if self.after_publication:
            self.after_publication()
        if self.publication_value is None:
            return None
        value = candidate(self.publication_value, job, requirements, policy)
        return dict(value, branch=self.publication_value['head']['ref'])

    def pr(self, item, number):
        return deepcopy(self.pr_value)

    def verdict(self, job, report):
        return 42


class Verdicts:
    def __init__(self):
        self.after = None
        self.calls = []
        self.receipt = {'check_id': 42}

    def ensure(self, job, report):
        self.calls.append((job['id'], report['outcome']))
        if self.after:
            self.after()
        return self.receipt


class ResultControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.life = Executions(self.store, clock=lambda: 100)
        self.queries = ExecutionQueries(self.store)
        self.github, self.verdicts = GitHub(), Verdicts()
        self.policy = {'human_required': False}
        self.serial = 0
        admitted = self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
            repository_id=1, repository_name='owner/repo', issue_number=1, title='Issue', body='Body',
            requirements=self.github.requirements, observed_at=90)
        self.member = admitted['member_id']
        self.manifest = dict(requirements_revision_id=admitted['revision_id'], base_oid=BASE,
                             policy_hash=digest(self.policy))
        self.controller = ResultController(self.store, self.github, policy=lambda job: self.policy,
            verdicts=self.verdicts, clock=lambda: 100)

    def view(self):
        return Queries(self.store).unit('u')

    def command(self):
        self.serial += 1
        return Command(str(self.serial), 'operator', 'u', self.view()['version'])

    def reserve(self, role='build', context=None):
        manifest = self.manifest
        identity = self.view().get('current_candidate_id')
        if role != 'build':
            with self.store.read_snapshot() as db:
                row = dict(db.execute('SELECT * FROM candidates WHERE id=?', (identity,)).fetchone())
            manifest = {key: row[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
            manifest['candidate_id'] = identity
        return self.life.reserve_job(self.command(), role, slot='slot', execution_fence='fence-' + str(self.serial),
            input_manifest=manifest, member_id=self.member if role == 'build' else None,
            candidate_id=identity if role != 'build' else None,
            launch=Launch('forge/issue-1', 'builder[bot]', 'codex', 200, {}, self.policy,
                          context=context or {}))['job_id']

    def report(self, job, outcome='pass', **fields):
        row = self.queries.job(job)
        report = dict(version=1, attempt=job, role=row['role'], outcome=outcome, summary='Test result')
        if row['role'] in {'review', 'audit'}:
            report['candidate'] = row['candidate']['key']
        if row['role'] == 'audit':
            report.update(human_required='not_required', human_reason='No manual verification')
        return {**report, **fields}

    def capture(self, job, report=None, *, cleanup=True, **extra):
        self.life.collect(self.command(), job, execution_fence=self.queries.job(job)['execution_fence'], capture={'result': report, 'logs': '', **extra})
        if cleanup:
            self.life.verify_cleanup(self.command(), job, execution_fence=self.queries.job(job)['execution_fence'],
                evidence={'process_stopped': True, 'resources_released': True})

    def publish(self, job):
        self.github.publication_value = dict(number=7, state='open', draft=False, merged=False,
            body='<!-- forge-submission ' + json.dumps(dict(version=1, issue=1, attempt=job, head=HEAD)) + ' -->',
            user={'login': 'builder[bot]', 'type': 'Bot'},
            head={'sha': HEAD, 'ref': 'forge/issue-1', 'repo': {'id': 1}},
            base={'sha': BASE, 'ref': 'main', 'repo': {'id': 1}})
        self.github.pr_value = deepcopy(self.github.publication_value)

    def build(self):
        job = self.reserve()
        self.publish(job)
        self.capture(job, self.report(job))
        self.assertTrue(self.controller.step(job)['accepted'])
        return job

    def charges(self):
        with self.store.read_snapshot() as db:
            return db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='charge'").fetchone()[0]

    def test_worker_pass_without_publication_cannot_advance_and_charges_once(self):
        job = self.reserve()
        self.capture(job, self.report(job))
        self.assertTrue(self.controller.step(job)['charged'])
        self.assertEqual(self.controller.step(job)['outcome'], 'failure')
        self.assertEqual(self.charges(), 1)
        self.assertEqual(self.view()['state'], 'building')

    def test_verified_publication_succeeds_even_when_worker_report_is_missing(self):
        job = self.reserve()
        self.publish(job)
        self.capture(job)
        self.assertTrue(self.controller.step(job)['accepted'])
        self.assertEqual(self.view()['state'], 'reviewing')
        self.assertEqual(self.charges(), 0)

    def test_capture_requires_verified_cleanup_before_any_github_evidence(self):
        job = self.reserve()
        self.capture(job, self.report(job), cleanup=False)
        self.github.issue_error = True
        self.assertEqual(self.controller.step(job)['pending'], 'cleanup')

    def test_requirements_policy_and_remote_closure_wait_for_observation_without_charging(self):
        for change in ('requirements', 'policy', 'closed'):
            with self.subTest(change=change):
                self.setUp()
                job = self.reserve()
                self.publish(job)
                self.capture(job, self.report(job))
                if change == 'requirements':
                    self.github.requirements = 'new requirements'
                elif change == 'policy':
                    self.policy = {'human_required': True}
                else:
                    self.github.issue_value['state'] = 'closed'
                self.assertIn('pending', self.controller.step(job))
                self.assertEqual(self.charges(), 0)
                self.assertIsNone(self.queries.job(job)['outcome'])

    def test_captured_build_with_stale_policy_is_consumed_without_charge_then_reassignable(self):
        job = self.reserve()
        self.publish(job)
        self.capture(job, self.report(job))
        self.policy = {'human_required': True}
        self.assertEqual(self.controller.step(job)['pending'], 'policy_reconciliation')
        self.assertTrue(self.queries.job(job)['result_revoked'])
        self.assertEqual(self.view()['reason_code'], 'result_pending')
        self.assertFalse(self.controller.step(job)['accepted'])
        self.assertEqual(self.queries.job(job)['outcome'], 'stale_failure')
        self.assertEqual(self.charges(), 0)
        self.assertIsNone(self.view()['reason_code'])
        self.manifest['policy_hash'] = digest(self.policy)
        next_job = self.reserve()
        self.assertNotEqual(next_job, job)
        self.assertEqual(self.queries.job(next_job)['input_manifest']['policy_hash'], digest(self.policy))

    def test_policy_revocation_preserves_independent_environment_gate(self):
        job = self.reserve()
        self.capture(job, self.report(job))
        self.life.open_gate(self.command(), 'environment', details={'reason': 'operator_recheck'})
        self.policy = {'human_required': True}
        self.controller.step(job)
        self.controller.step(job)
        self.assertEqual(self.view()['reason_code'], 'environment')
        self.assertEqual(self.charges(), 0)

    def test_captured_policy_revocation_requires_exact_fence_and_changed_policy(self):
        job = self.reserve()
        self.capture(job)
        row = self.queries.job(job)
        for fence, policy_hash in [('another-fence', 'different'), (row['execution_fence'], self.manifest['policy_hash'])]:
            with self.subTest(fence=fence, policy_hash=policy_hash):
                with self.assertRaises(TransitionError):
                    self.life.revoke_captured(self.command(), job, execution_fence=fence,
                        input_manifest=self.manifest, policy_hash=policy_hash)
                self.assertFalse(self.queries.job(job)['result_revoked'])
        self.assertEqual(self.charges(), 0)

    def test_pause_before_validation_consumes_stale_result_without_github_or_charge(self):
        job = self.reserve()
        self.capture(job, self.report(job))
        self.life.pause(self.command())
        self.github.issue_error = True
        self.assertFalse(self.controller.step(job)['accepted'])
        self.assertEqual(self.queries.job(job)['outcome'], 'stale_failure')
        self.assertEqual(self.charges(), 0)

    def test_pause_during_publication_read_cannot_advance_or_charge(self):
        job = self.reserve()
        self.publish(job)
        self.capture(job, self.report(job))
        self.github.after_publication = lambda: self.life.pause(self.command())
        with self.assertRaises(VersionConflict):
            self.controller.step(job)
        self.assertEqual(self.charges(), 0)
        self.assertEqual(self.view()['state'], 'building')
        self.github.after_publication = None
        self.assertFalse(self.controller.step(job)['accepted'])

    def test_oom_and_environment_waits_do_not_consume_failure_budget(self):
        for oom in (True, False):
            with self.subTest(oom=oom):
                self.setUp()
                job = self.reserve()
                report = self.report(job, 'blocked_environment', blocker='services', required_services=['postgres'])
                self.capture(job, report, execution={'oom_killed': oom})
                answer = self.controller.step(job)
                self.assertTrue(answer['accepted'])
                self.assertFalse(answer['charged'])
                self.assertEqual(self.view()['reason_code'], 'environment')
                self.assertEqual(self.charges(), 0)

    def test_only_failed_error_channel_establishes_provider_outage(self):
        for channel, exit_code, charged in [('error', 1, False), ('message', 1, True), ('error', 0, True)]:
            with self.subTest(channel=channel, exit_code=exit_code):
                self.setUp()
                job = self.reserve()
                self.capture(job, logs=json.dumps({'type': channel, 'error': {'message': 'usage limit'}}),
                             execution={'exit_code': exit_code})
                self.assertEqual(self.controller.step(job)['charged'], charged)
                self.assertEqual(self.charges(), int(charged))

    def test_pass_and_reject_flow_through_real_durable_effect_adapter(self):
        for outcome, phase in [('pass', 'auditing'), ('reject', 'building')]:
            with self.subTest(outcome=outcome):
                self.setUp()
                self.build()
                job = self.reserve('review')
                self.capture(job, self.report(job, outcome))
                self.controller.verdicts = EffectController(self.store, self.github, clock=lambda: 100)
                self.assertTrue(self.controller.step(job)['accepted'])
                self.assertEqual(self.view()['state'], phase)
                self.assertEqual(self.charges(), int(outcome == 'reject'))
                with self.store.read_snapshot() as db:
                    effect = db.execute("SELECT status,payload_json FROM external_effects WHERE kind='github_verdict'").fetchone()
                self.assertEqual(effect['status'], 'confirmed')
                self.assertEqual(json.loads(effect['payload_json'])['report']['outcome'], outcome)

    def test_review_pass_requires_published_verdict_and_fresh_candidate_read(self):
        self.build()
        job = self.reserve('review')
        self.capture(job, self.report(job))
        self.verdicts.receipt = None
        self.assertEqual(self.controller.step(job)['pending'], 'verdict_effect')
        self.assertEqual(self.view()['state'], 'reviewing')
        self.verdicts.receipt = {'check_id': 42}
        self.verdicts.after = lambda: self.github.pr_value['head'].update(sha='c' * 40)
        self.assertEqual(self.controller.step(job)['pending'], 'candidate_reconciliation')
        self.assertEqual(self.charges(), 0)
        self.assertIsNone(self.queries.job(job)['outcome'])

    def test_verdict_intent_version_change_does_not_make_valid_result_stale(self):
        self.build()
        job = self.reserve('review')
        self.capture(job, self.report(job))
        self.verdicts.after = lambda: self.life.observe_issue(self.command(), 'i', state='open',
            title='Issue', body='Body', requirements=self.github.requirements,
            remote_updated_at='2026-10-07T12:00:00Z', generation=1)
        self.assertTrue(self.controller.step(job)['accepted'])
        self.assertEqual(self.view()['state'], 'auditing')

    def test_operator_pause_during_verdict_confirmation_cannot_advance(self):
        self.build()
        job = self.reserve('review')
        self.capture(job, self.report(job))
        self.verdicts.after = lambda: self.life.pause(self.command())
        self.assertFalse(self.controller.step(job)['accepted'])
        self.assertEqual(self.view()['state'], 'reviewing')
        self.assertEqual(self.charges(), 0)

    def test_forged_candidate_claim_cannot_publish_a_verdict(self):
        self.build()
        job = self.reserve('review')
        self.capture(job, self.report(job, candidate='another-candidate'))
        self.assertTrue(self.controller.step(job)['charged'])
        self.assertEqual(self.verdicts.calls, [])
        self.assertEqual(self.view()['state'], 'reviewing')

    def test_changed_base_blocks_even_a_valid_report_before_verdict_publication(self):
        self.build()
        job = self.reserve('review')
        self.capture(job, self.report(job))
        self.github.pr_value['base']['sha'] = 'c' * 40
        self.assertEqual(self.controller.step(job)['pending'], 'candidate_reconciliation')
        self.assertEqual(self.verdicts.calls, [])
        self.assertEqual(self.charges(), 0)

    def test_review_verification_defers_without_recording_a_technical_pass(self):
        self.build()
        job = self.reserve('review')
        self.capture(job, self.report(job, 'needs_human_verification',
            verification={'what': 'Check staging', 'why': 'Requires deployment', 'checks': []}))
        self.assertFalse(self.controller.step(job)['charged'])
        self.assertEqual(self.view()['state'], 'reviewing')
        self.assertEqual(self.view()['reason_code'], 'verification_human')
        self.assertEqual(self.verdicts.calls, [])

    def test_review_ci_verification_uses_only_configured_check_identity(self):
        self.build()
        job = self.reserve('review', context={'verification_checks': [{'name': 'staging', 'app_id': 123}]})
        self.capture(job, self.report(job, 'needs_human_verification',
            verification={'what': 'Check staging', 'why': 'Requires deployment', 'checks': ['staging']}))
        self.assertFalse(self.controller.step(job)['charged'])
        self.assertEqual(self.view()['state'], 'reviewing')
        self.assertEqual(self.view()['reason_code'], 'verification_ci')
        with self.store.read_snapshot() as db:
            details = json.loads(db.execute("SELECT details_json FROM unit_gates WHERE code='verification_ci'").fetchone()[0])
        self.assertEqual(details['ci'], [{'name': 'staging', 'app_id': 123}])

    def test_review_rejection_charges_and_invalidates_current_candidate_once(self):
        self.build()
        job = self.reserve('review')
        self.capture(job, self.report(job, 'reject'))
        self.assertTrue(self.controller.step(job)['charged'])
        self.assertEqual(self.charges(), 1)
        self.assertEqual(self.view()['state'], 'building')
        self.assertIsNone(self.view()['current_candidate_id'])
        self.controller.step(job)
        self.assertEqual(self.charges(), 1)

    def test_repository_human_policy_cannot_be_waived_by_audit_report(self):
        self.policy['human_required'] = True
        self.manifest['policy_hash'] = digest(self.policy)
        self.build()
        review = self.reserve('review')
        self.capture(review, self.report(review))
        self.controller.step(review)
        audit = self.reserve('audit')
        self.capture(audit, self.report(audit))
        self.assertTrue(self.controller.step(audit)['accepted'])
        self.assertEqual(self.view()['reason_code'], 'approval_required')

    def test_fresh_human_ownership_prevents_result_acceptance_before_observation(self):
        job = self.reserve()
        self.publish(job)
        self.capture(job, self.report(job))
        self.github.issue_value['assignees'] = [{'login': 'person', 'type': 'User'}]
        self.assertEqual(self.controller.step(job)['pending'], 'issue_reconciliation')
        self.assertIsNone(self.queries.job(job)['outcome'])
        self.assertEqual(self.charges(), 0)

    def test_audit_pass_opens_human_gate_in_same_transition(self):
        self.build()
        review = self.reserve('review')
        self.capture(review, self.report(review))
        self.controller.step(review)
        audit = self.reserve('audit')
        self.capture(audit, self.report(audit, human_required='required', human_reason='Confirm staging migration'))
        self.assertTrue(self.controller.step(audit)['accepted'])
        self.assertEqual(self.view()['state'], 'merging')
        self.assertEqual(self.view()['reason_code'], 'approval_required')

    def test_build_human_verification_binds_gate_to_published_candidate(self):
        job = self.reserve()
        self.publish(job)
        self.capture(job, self.report(job, 'needs_human_verification',
            verification={'what': 'Test staging', 'why': 'Needs deployment', 'checks': []}))
        self.assertTrue(self.controller.step(job)['accepted'])
        self.assertEqual(self.view()['reason_code'], 'verification_human')
        with self.store.read_snapshot() as db:
            gate = db.execute("SELECT candidate_id FROM unit_gates WHERE code='verification_human'").fetchone()
            self.assertEqual(gate[0], self.view()['current_candidate_id'])

    def test_decision_and_grounded_dependency_waits_preserve_budget(self):
        for outcome in ('needs_decision', 'waiting_on_issues'):
            with self.subTest(outcome=outcome):
                self.setUp()
                job = self.reserve()
                self.github.issue_value['body'] = 'Depends on #2'
                fields = {'decision': dict(question='Which?', options=['A', 'B'], recommendation='A', evidence='Needs choice')}
                if outcome == 'waiting_on_issues':
                    fields = {'issues': [2], 'evidence': 'Depends on #2'}
                self.capture(job, self.report(job, outcome, **fields))
                self.assertFalse(self.controller.step(job)['charged'])
                self.assertEqual(self.charges(), 0)

    def test_fabricated_dependency_quote_is_charged_failure(self):
        job = self.reserve()
        self.capture(job, self.report(job, 'waiting_on_issues', issues=[2], evidence='Depends on #2'))
        self.assertTrue(self.controller.step(job)['charged'])

    def test_github_outage_keeps_capture_pending_for_retry(self):
        job = self.reserve()
        self.capture(job)
        self.github.issue_error = True
        page = self.controller.cycle()
        self.assertEqual(page['items'][0]['error'], 'GitHubUnavailable')
        self.assertIsNone(self.queries.job(job)['outcome'])
        self.assertEqual(self.charges(), 0)
