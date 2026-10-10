"""An issue between two stages reads as work in progress, never as a hold (#241)."""
import tempfile
import unittest

from forge.store.executions import Launch
from forge.store.models import Candidate
from forge.store.transitions import manifest_hash
from tests import test_operator_status_matrix as matrix


class TransitionalStatusTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fixture = matrix.OperatorStatusMatrixTests()
        self.fixture.prepare(directory.name, 'queued')
        self.life = self.fixture.life

    def board(self):
        return self.fixture.views.factory(issue='1:1')['issues'][0]

    def assert_settling(self, reason, role):
        issue = self.board()
        self.assertEqual(issue['reason_code'], reason)
        self.assertEqual(issue['operator_status']['code'], 'working')
        self.assertEqual(issue['operator_status']['label'], 'Working')
        self.assertNotEqual(issue['state'], 'hold')
        self.assertIsNone(issue['hold_kind'])
        self.assertIsNone(issue['attention_kind'])
        self.assertEqual(issue['transition']['code'], reason)
        self.assertEqual(issue['transition']['role'], role)
        board = next(row for row in self.fixture.views.factory(section='board')['issues'] if row['key'] == issue['key'])
        self.assertEqual(board['transition'], issue['transition'])
        self.assertEqual(board['state'], issue['state'])
        self.assertEqual(board['operator_status'], issue['operator_status'])
        # The SQL list filter classifies the same issue the same way.
        self.assertEqual(self.fixture.views.issues(state='working')['total'], 1)
        self.assertEqual(self.fixture.views.issues(state='waiting')['total'], 0)
        return issue

    def review(self):
        requirements = {'acceptance': ['works']}
        candidate = Candidate(10, 'branch', 'head', 'base', 'policy', manifest_hash(requirements), 'inputs')
        adopted = self.life.adopt_candidate(self.fixture.command(), candidate, evidence={'approved': True, 'verified': True,
            'pull_number': 10, 'head_oid': 'head', 'base_oid': 'base', 'policy_hash': 'policy', 'input_manifest_hash': 'inputs'})
        manifest = dict(candidate_id=adopted['candidate_id'], head_oid='head', base_oid='base', policy_hash='policy',
                        requirements_hash=manifest_hash(requirements), input_manifest_hash='inputs')
        return self.life.reserve_job(self.fixture.command(), 'review', slot='slot', execution_fence='fence',
            input_manifest=manifest, candidate_id=adopted['candidate_id'],
            launch=Launch('branch', 'builder[bot]', 'codex', 200, {}, {}))['job_id']

    def test_reserved_job_starts_in_its_bay(self):
        job = self.review()
        issue = self.board()
        self.assertEqual(issue['active'], job)
        self.assertEqual(issue['operator_status']['code'], 'working')
        self.assertEqual(issue['state'], 'built')
        self.assertIsNone(issue['transition'])

    def test_result_capture_after_review_stays_in_review(self):
        job = self.review()
        self.life.collect(self.fixture.command(), job, execution_fence='fence', capture={'result': None, 'logs': ''})
        issue = self.assert_settling('cleanup_pending', 'review')
        self.assertEqual(issue['transition']['job_id'], job)
        self.assertEqual(issue['state'], 'built')
        self.life.verify_cleanup(self.fixture.command(), job, execution_fence='fence',
                                 evidence={'process_stopped': True, 'resources_released': True})
        issue = self.assert_settling('result_pending', 'review')
        self.assertEqual(issue['operator_status']['reason'], 'Checking the completed worker result')
        self.assertEqual(issue['state'], 'built')

    def test_failed_attempt_routing_is_not_a_hold(self):
        with self.fixture.store.read_snapshot() as db:
            revision = db.execute("SELECT requirements_revision_id,id FROM unit_issues WHERE unit_id='u'").fetchone()
        manifest = dict(requirements_revision_id=revision[0], base_oid='base', policy_hash='policy')
        job = self.life.reserve_job(self.fixture.command(), 'build', slot='slot', execution_fence='fence',
            input_manifest=manifest, member_id=revision[1])['job_id']
        self.life.record_failure(self.fixture.command(), job, execution_fence='fence', input_manifest=manifest,
                                 evidence={'reason': 'tests failed'})
        issue = self.board()
        self.assertEqual(issue['reason_code'], 'recovery_pending')
        self.assertEqual(issue['operator_status']['code'], 'working')
        self.assertEqual(issue['transition']['role'], 'build')
        self.assertIsNone(issue['hold_kind'])

    def test_real_holds_stay_holds(self):
        self.life.open_gate(self.fixture.command(), 'environment', details={'reason': 'database'})
        issue = self.board()
        self.assertEqual(issue['state'], 'hold')
        self.assertEqual(issue['hold_kind'], 'environment')
        self.assertIsNone(issue['transition'])


if __name__ == '__main__':
    unittest.main()
