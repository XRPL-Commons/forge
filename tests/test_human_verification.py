import copy
import time
import unittest
import tempfile
from pathlib import Path
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.contracts import result, assignment_note
from forge.factory.control import attention_kind
from forge.factory.harness import schema
from forge.factory.issues import presentation
from forge.factory.verification import ci_result
from forge.factory.verification import configure
from forge.settings import DEFAULT, digest, write_json
from tests.test_factory_cycle import CycleTest
from tests.test_factory_adapters import WorkerContractTests

VERIFY = {'what': 'Build the release image, POST synthetic input to /api/export-pdf, and confirm %PDF-.',
          'why': 'Workers have no Docker socket.', 'checks': []}


class VerificationContractTests(unittest.TestCase):
    def test_operator_ci_configuration_requires_a_connected_repository_and_current_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / 'factory.json', {**DEFAULT, 'repositories': [{'id': 1, 'name': 'example/project', 'enabled': True}]})
            runtime = validate({})
            checks = [{'name': 'release-smoke', 'app_id': 123}]
            configured = configure(root, '1', checks, digest(runtime))
            self.assertEqual(configured['checks'], checks)
            with self.assertRaises(ValueError): configure(root, '1', [], digest(runtime))
            with self.assertRaises(ValueError): configure(root, '2', checks, configured['runtime_revision'])
            self.assertEqual(configure(root, '1', [], configured['runtime_revision'])['checks'], [])

    def test_all_worker_roles_can_report_exact_steps_but_no_other_outcome_can(self):
        for role in ('build', 'review', 'audit', 'recovery'):
            job = WorkerContractTests().note(role)
            report = {'version': 1, 'attempt': job['id'], 'role': role, 'outcome': 'needs_human_verification',
                      'summary': 'External acceptance check remains.', 'verification': VERIFY}
            if role in ('review', 'audit'): report['candidate'] = 'candidate-key'
            if role == 'audit': report.update(human_required='required', human_reason=VERIFY['what'])
            if role == 'recovery': report.update(pattern='verification', evidence=[], uncertainty='', alternative=None)
            self.assertEqual(result(copy.deepcopy(report), job)['verification'], VERIFY)
            self.assertIn('needs_human_verification', schema(job)['properties']['outcome']['enum'])
            for invalid in (None, {}, {**VERIFY, 'what': ''}, {**VERIFY, 'checks': ['smoke', 'smoke']},
                            {**VERIFY, 'why': 'x' * 4001}, {**VERIFY, 'command': 'arbitrary'}):
                with self.subTest(role=role, invalid=invalid), self.assertRaises(ValueError):
                    result({**report, 'verification': invalid}, job)
            with self.assertRaises(ValueError): result({**report, 'outcome': 'pass'}, job)
            self.assertIn('history.verified_checks', assignment_note(job)['instructions'])

    def test_ci_requires_exact_revision_publisher_name_and_success(self):
        selected = {'head': 'head', 'base': 'base'}
        profile = [{'name': 'release-smoke', 'app_id': 123}]
        row = {'id': 1, 'name': 'release-smoke', 'app_id': 123, 'status': 'completed', 'conclusion': 'success'}
        snapshot = {'head': 'head', 'base': 'base', 'checks': {'head': [row]}}
        self.assertTrue(ci_result(snapshot, selected, profile))
        for changed in ({'app_id': 124}, {'name': 'other'}, {'status': 'in_progress'}, {'conclusion': 'skipped'}, {'conclusion': 'failure'}):
            self.assertFalse(ci_result({**snapshot, 'checks': {'head': [{**row, **changed}]}}, selected, profile))
        self.assertFalse(ci_result({**snapshot, 'checks': {'head': [row, row]}}, selected, profile))
        self.assertFalse(ci_result({**snapshot, 'base': 'old'}, selected, profile))
        self.assertFalse(ci_result(None, selected, profile))
        for profile in ({'example/project': [{'name': 'smoke', 'app_id': True}]}, {'example/project': []}):
            with self.assertRaises(ValueError): validate({'repository_verification': profile})


class VerificationCycleTests(CycleTest):
    def report(self, job, checks=()):
        report = {'version': 1, 'attempt': job['id'], 'role': job['role'], 'outcome': 'needs_human_verification',
                  'summary': 'External acceptance check remains.', 'verification': {**VERIFY, 'checks': list(checks)}}
        if job['role'] in ('review', 'audit'): report['candidate'] = job['candidate']['key']
        if job['role'] == 'audit': report.update(human_required='required', human_reason=VERIFY['what'])
        if job['role'] == 'recovery': report.update(pattern='verification', evidence=['PR review'], uncertainty='', alternative=None)
        return report

    def build_wait(self, checks=()):
        self.cycle(); job = self.active(); self.github.publish(job)
        self.engine.finish(job, self.report(job, checks))
        self.assertFalse(self.cycle()['errors'])
        return self.ledger.read()['issues'][self.key]

    def approve(self, at=None):
        self.github.approved = True
        self.github.approvals = Mock(return_value=[{'login': 'maintainer', 'state': 'APPROVED',
            'at': at if at is not None else time.time() + 2, 'counted': True, 'why': None}])

    def test_build_wait_is_visible_and_resumes_reviews_without_spending_attempt(self):
        item = self.build_wait()
        self.assertEqual(item['state'], 'approval_needed')
        self.assertEqual(item['failures'], [])
        self.assertEqual(attention_kind(item), 'verify')
        row = presentation({'number': 1, 'state': 'open', 'labels': []}, item,
                           {'name': 'example/project', 'enabled': True, 'access_ready': True}, self.config)
        self.assertEqual(row['verification_request']['what'], VERIFY['what'])
        self.assertIsNotNone(row['approval'])
        self.assertIn('retry', row['actions'])
        self.approve(at=item['verification_request']['created_at'] - 10)
        self.cycle(); self.assertEqual(self.state(), 'approval_needed')
        self.approve(); self.assertFalse(self.cycle()['errors'])
        self.assertEqual(self.active()['role'], 'review')
        self.assertTrue(self.active()['history']['verified_checks'])
        self.assertFalse(self.github.pull['merged'])
        self.finish('review'); self.cycle(); self.finish('audit'); self.cycle(); self.cycle()
        self.assertEqual(self.state(), 'merged')

    def test_review_and_audit_pause_without_publishing_a_passing_verdict(self):
        for role in ('review', 'audit'):
            with self.subTest(role=role):
                self.setUp(); self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
                if role == 'audit': self.finish('review'); self.cycle()
                job = self.active(); self.engine.finish(job, self.report(job)); self.cycle()
                item = self.ledger.read()['issues'][self.key]
                self.assertEqual(item['state'], 'approval_needed')
                self.assertNotIn(role, item['evidence'])
                self.assertEqual(item['failures'], [])
                self.approve(); self.cycle(); self.assertEqual(self.active()['role'], role)

    def test_recovery_corrects_verification_failure_without_consuming_budget(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        self.finish('review', 'unable'); self.cycle()
        job = self.active(); self.assertEqual(job['role'], 'recovery')
        self.engine.finish(job, self.report(job)); self.assertFalse(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['state'], 'approval_needed')
        self.assertFalse(item['failures'][0]['charged'])
        self.approve(); self.cycle(); self.assertEqual(self.active()['role'], 'review')

    def test_changed_candidate_discards_old_request_and_proof(self):
        self.build_wait(); self.approve()
        self.github.pull['head']['sha'] = 'd' * 40
        self.cycle()
        item = self.ledger.read()['issues'][self.key]
        self.assertNotIn('verification_request', item)
        self.assertNotIn('verified_checks', item)
        self.assertFalse(self.github.pull['merged'])

    def test_opted_in_ci_resumes_automatically_but_failure_cannot_be_overridden(self):
        self.config = validate({**self.config, 'repository_verification': {'example/project': [{'name': 'smoke', 'app_id': 123}]}})
        item = self.build_wait(['smoke'])
        selected = item['candidate']
        row = {'name': 'smoke', 'app_id': 123, 'status': 'completed', 'conclusion': 'failure'}
        self.github.ci_evidence = Mock(return_value={'head': selected['head'], 'base': selected['base'], 'checks': {selected['head']: [row]}})
        self.approve(); self.cycle(); self.assertEqual(self.state(), 'approval_needed')
        row['conclusion'] = 'success'
        self.cycle(); self.assertEqual(self.active()['role'], 'review')
        self.finish('review'); self.cycle(); self.finish('audit')
        row['conclusion'] = 'failure'
        self.cycle(); self.assertNotEqual(self.state(), 'merged')
        row['conclusion'] = 'success'; self.cycle(); self.assertEqual(self.state(), 'merged')

    def test_unconfigured_ci_request_stays_a_human_gate(self):
        item = self.build_wait(['untrusted-check'])
        self.assertEqual(item['verification_request']['ci'], [])
        self.cycle(); self.assertEqual(self.state(), 'approval_needed')

    def test_revoked_approval_blocks_merge_after_remaining_reviews(self):
        self.build_wait(); self.approve(); self.cycle()
        self.finish('review'); self.cycle(); self.finish('audit')
        self.github.approved = False
        self.github.approvals.return_value = []
        self.cycle(); self.assertEqual(self.state(), 'approval_needed')
        self.assertFalse(self.github.pull['merged'])

    def test_closed_or_unavailable_candidate_never_resumes(self):
        from forge.factory.github import GitHubUnavailable
        self.build_wait(); self.approve()
        read = self.github.pr
        self.github.pr = Mock(side_effect=GitHubUnavailable('Unavailable'))
        self.cycle(); self.assertEqual(self.state(), 'approval_needed')
        self.github.pr = read
        self.github.closed = True
        self.cycle(); self.assertEqual(self.state(), 'cancelled')

    def test_requests_without_a_published_candidate_cannot_create_an_approval(self):
        self.cycle(); job = self.active(); self.engine.finish(job, self.report(job)); self.cycle()
        self.assertNotEqual(self.state(), 'approval_needed')
        self.assertNotIn('verification_request', self.ledger.read()['issues'][self.key])
