from pathlib import Path
import tempfile
import unittest

from forge.factory.first_merge import proof
from forge.store.models import Candidate, Command
from forge.store.transitions import Lifecycle, manifest_hash
from tests.test_sqlite_identity import selected_store


class FirstMergeProofTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.store = selected_store(self.root)
        self.life = Lifecycle(self.store, clock=lambda:100)
        self.requirements = {'acceptance':['works']}
        self.life.admit_issue(request_id='admit',actor_id='operator',unit_id='unit',issue_id='1:1',
            repository_id=1,repository_name='owner/repo',issue_number=1,title='Issue',body='Body',
            requirements=self.requirements,observed_at=90)
        self.pilot = dict(repository={'id':1,'name':'owner/repo'},number=1,started_at=99,clock=lambda:110)

    def command(self, name):
        with self.store.read_snapshot() as db:
            version = db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0]
        return Command(name,'operator','unit',version)

    def test_external_completion_is_not_a_forge_merge(self):
        self.life.observe_issue(self.command('closed'), '1:1', state='closed', state_reason='completed',
            title='Issue',body='Body',requirements=self.requirements,remote_updated_at='2026-10-09T20:00:00Z',generation=1)
        result = proof(self.root, **self.pilot)
        self.assertEqual(result['operator_status']['label'], 'Completed')
        self.assertEqual(result['status'], 'waiting')
        self.assertIsNone(result['evidence'])

    def test_adopted_candidate_cannot_substitute_for_worker_evidence(self):
        candidate = Candidate(1,'forge/issue-1','head','base','policy',manifest_hash(self.requirements),'input')
        evidence = {key:getattr(candidate,key) for key in ('pull_number','head_oid','base_oid','policy_hash','input_manifest_hash')}
        self.life.adopt_candidate(self.command('adopt'),candidate,evidence={**evidence,'approved':True,'verified':True})
        self.assertIsNotNone(proof(self.root, **self.pilot)['candidate'])
        self.assertEqual(proof(self.root, **self.pilot)['status'], 'waiting')

    def test_repository_identity_and_timer_are_bound_to_the_pilot(self):
        with self.assertRaises(ValueError):
            proof(self.root, **{**self.pilot,'repository':{'id':1,'name':'different/repo'}})
        with self.assertRaises(ValueError):
            proof(self.root, **{**self.pilot,'started_at':111})
        self.assertEqual(proof(self.root, **{**self.pilot,'number':2})['status'], 'waiting')
