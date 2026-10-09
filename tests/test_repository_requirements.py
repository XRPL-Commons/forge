import json
from pathlib import Path
import tempfile
import unittest

from forge.factory.prerequisites import references, blocker
from forge.store.db import Store
from forge.store.models import Command, IdempotencyConflict, TransitionError, VersionConflict
from forge.store.repository_requirements import order_in, orders_in
from forge.store.runtime_requirements import RuntimeRequirements


class RepositoryRequirementsTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store=Store.create(Path(self.directory.name)/'store')
        self.commands=RuntimeRequirements(self.store,clock=lambda:100)
        self.revisions={}
        self.members={}
        for number in (1,2):
            result=self.commands.admit_issue(request_id='admit'+str(number),actor_id='operator',unit_id='u'+str(number),issue_id='i'+str(number),
                repository_id=1,repository_name='owner/repo',issue_number=number,title='Work',body='',requirements='requirements'+str(number),observed_at=99)
            self.revisions[number]=result['revision_id']
            self.members[number]=result['member_id']

    def command(self,number):
        with self.store.read_snapshot() as db:
            version=db.execute('SELECT version FROM units WHERE id=?',('u'+str(number),)).fetchone()[0]
        return Command('command'+str(number)+'-'+str(version),'operator','u'+str(number),version)

    def order(self,number,targets,request):
        with self.store.write() as db:
            return order_in(db,issue_id='i'+str(number),requirements_revision_id=self.revisions[number],
                prerequisites=targets,actor_id='operator',request_id=request)

    def test_order_pins_revision_and_replays_exactly(self):
        result=self.order(1,['Other/Repo#4'],'order1')
        self.assertEqual(result,self.order(1,['other/repo#4'],'order1'))
        with self.store.read_snapshot() as db:
            self.assertEqual(orders_in(db,'i1',self.revisions[1]),['other/repo#4'])
            gate=db.execute("SELECT details_json FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL").fetchone()
            self.assertEqual(json.loads(gate[0])['reason'],'prerequisites_need_observation')
            self.assertEqual(db.execute('SELECT count(*) FROM prerequisite_orders').fetchone()[0],1)
        with self.assertRaises(IdempotencyConflict):self.order(1,['other/repo#5'],'order1')
        with self.assertRaises(VersionConflict),self.store.write() as db:
            order_in(db,issue_id='i1',requirements_revision_id='old',prerequisites=[],actor_id='operator',request_id='stale')

    def test_cycle_rejected_in_same_transaction(self):
        self.order(1,['owner/repo#2'],'order1')
        with self.assertRaises(TransitionError):self.order(2,['owner/repo#1'],'order2')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM prerequisite_orders').fetchone()[0],1)

    def test_cross_repository_identity_cannot_be_rebound(self):
        self.order(1,['other/repo#4'],'order1')
        fact=dict(reference='other/repo#4',repository_id=2,github_id=44,number=4,kind='issue',state='closed',state_reason='completed',merged=False,updated_at='2026-10-09T12:00:00Z')
        self.assertTrue(self.commands.dependencies(self.command(1),requirements_revision_id=self.revisions[1],generation=1,facts=[fact])['clear'])
        self.assertFalse(self.commands.dependencies(self.command(1),requirements_revision_id=self.revisions[1],generation=2,facts=[{**fact,'repository_id':3}])['clear'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT repository_id FROM prerequisite_identities').fetchone()[0],2)

    def test_closed_without_completion_does_not_release_order(self):
        self.order(1,['other/repo#4'],'order1')
        fact=dict(reference='other/repo#4',repository_id=2,github_id=44,number=4,kind='pull_request',state='closed',state_reason=None,merged=False,updated_at='2026-10-09T12:00:00Z')
        self.assertFalse(self.commands.dependencies(self.command(1),requirements_revision_id=self.revisions[1],generation=1,facts=[fact])['clear'])
        self.assertTrue(self.commands.dependencies(self.command(1),requirements_revision_id=self.revisions[1],generation=2,facts=[{**fact,'merged':True}])['clear'])
        for reason in ('not_planned','duplicate',None):
            self.assertIsNotNone(blocker({**fact,'kind':'issue','state_reason':reason}))

    def test_projection_names_cross_repository_blockers_without_guessing_local_numbers(self):
        from forge.factory.store_views import PipelineViews
        self.order(1,['other/repo#4'],'order1')
        self.commands.dependencies(self.command(1),requirements_revision_id=self.revisions[1],generation=1,
            facts=[{'reference':'other/repo#4','error':'prerequisite_inaccessible'}])
        row=PipelineViews(self.store,clock=lambda:100).issues(key='i1')['issue']
        self.assertEqual(row['prerequisites'],[dict(reference='other/repo#4',repository='other/repo',number=4,
            kind=None,reason='prerequisite_inaccessible',complete=False,url='https://github.com/other/repo/issues/4')])
        self.assertNotIn(4,row['dependencies'])
        fact=dict(reference='other/repo#4',repository_id=2,github_id=44,number=4,kind='pull_request',state='closed',
            state_reason=None,merged=False,updated_at='2026-10-09T12:00:00Z')
        self.commands.dependencies(self.command(1),requirements_revision_id=self.revisions[1],generation=2,facts=[fact])
        row=PipelineViews(self.store,clock=lambda:100).issues(key='i1')['issue']
        self.assertEqual(row['prerequisites'][0]['reason'],'prerequisite_closed_unmerged')
        self.assertEqual(row['prerequisites'][0]['url'],'https://github.com/other/repo/pull/4')

    def test_independent_environment_choices_keep_their_issue_identity(self):
        for number,tier,services in [(1,'standard',[]),(2,'full',['postgres'])]:
            self.commands.select_environment(self.command(number),requirements_revision_id=self.revisions[number],
                choice=dict(tier=tier,services=services,automatic=True),labels=['env:'+tier],config_hash='policy')
        with self.store.read_snapshot() as db:
            self.assertEqual([(r['issue_id'],json.loads(r['choice_json'])['tier']) for r in db.execute('SELECT * FROM issue_environment_observations ORDER BY issue_id')],[('i1','standard'),('i2','full')])
            self.assertEqual({r[0] for r in db.execute('SELECT member_id FROM unit_environment')},set(self.members.values()))

    def test_explicit_clauses_parse_uniform_local_and_cross_repository_refs(self):
        body='Depends on #2, Other/Repo#3 and https://github.com/Else/Repo/pull/4.\n#5 must be merged first.\nBlocks #8\nSee owner/repo#9\n```\nDepends on #99\n```'
        self.assertEqual(references(body,'Owner/Repo'),['else/repo#4','other/repo#3','owner/repo#2','owner/repo#5'])


if __name__=='__main__':unittest.main()
