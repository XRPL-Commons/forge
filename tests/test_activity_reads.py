"""Page batching preserves full canonical cards and bounds unrelated history."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.factory.activity_reads import ActivityReads
from forge.factory.config import validate
from forge.factory.store_views import PipelineViews
from forge.repository_environments import RepositoryEnvironments
from forge.settings import Settings, DEFAULT
from forge.store.models import Command
from forge.store.queries import Queries
from forge.store.transitions import Lifecycle
from tests import test_completed_selection as completed
from tests import test_delivery_execution as delivery
from tests import test_operator_status_matrix as matrix
from tests.test_sqlite_identity import selected_store


def parity(test, views, **options):
    result = views.factory(**options)
    # Existing detail readers retain the former individual SQL reads, providing
    # an independent full-response comparison without duplicating status logic.
    with patch('forge.factory.activity_reads.ActivityReads', return_value=None):
        test.assertEqual(views.factory(**options), result)
    return result


class ActivityParityTests(unittest.TestCase):
    def test_every_operator_state_keeps_complete_card_response(self):
        for case in matrix.MATRIX:
            with self.subTest(state=case['name']), tempfile.TemporaryDirectory() as root:
                fixture = matrix.OperatorStatusMatrixTests(); fixture.prepare(root, case['name'])
                view = PipelineViews(fixture.store, runtime=validate({}), clock=lambda:100)
                for section in ('board','completed','runners'):
                    parity(self, view, section=section)

    def test_grouped_active_tasks_and_scoped_gates_keep_full_response(self):
        fixture=delivery.DeliveryExecutionTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.compose(); _,plan,_=fixture.plan(); fixture.child(plan,'0'); fixture.child(plan,'1')
        fixture.life.open_gate(fixture.command(),'label_hold',member_id=fixture.members['issue-1'],details={'label':'hold'})
        view=PipelineViews(fixture.store,runtime=validate({}),clock=lambda:100)
        parity(self,view,section='board')
        for key in ('issue-0','issue-1'): parity(self,view,issue=key)


class FormerMembershipParityTests(unittest.TestCase):
    setUp=completed.CompletedSelectionTests.setUp
    def test_removed_reopened_closed_and_dissolved_members_keep_context(self):
        view=PipelineViews(self.store,runtime=validate({}),clock=lambda:1000)
        for section in ('board','completed','runners'): parity(self,view,section=section)
        for n in range(1,14): parity(self,view,issue='1:'+str(n))


class RealisticPageTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name); self.store=selected_store(self.root)
        self.settings=Settings(self.root,client=Mock(),clock=lambda:1000)
        self.store=self.settings.database
        self.repositories=[{'id':n,'name':f'example/repo-{n}','enabled':True} for n in range(1,30)]
        self.settings.save_config({**DEFAULT,'repositories':self.repositories})
        life=Lifecycle(self.store,clock=lambda:1000); self.members={}
        for n in range(1,71):
            repo=(n-1)%29+1
            pin=life.admit_issue(request_id='admit-'+str(n),actor_id='test',unit_id='u'+str(n),issue_id=str(n),
                repository_id=repo,repository_name=f'example/repo-{repo}',issue_number=n,title='Bounded fixture',
                body='',requirements='requirements',observed_at=1000)
            self.members[n]=pin
            if n<=50:
                life.observe_issue(Command('observe-'+str(n),'test','u'+str(n),1),str(n),state='closed',state_reason='not_planned',
                    title='Bounded fixture',body='',requirements='requirements',generation=1,remote_updated_at='2026-10-10T00:00:00Z')
                with self.store.read_snapshot() as db:version=db.execute('SELECT version FROM units WHERE id=?',('u'+str(n),)).fetchone()[0]
                life.close_unit(Command('close-'+str(n),'test','u'+str(n),version),close_code='operator_closed',evidence={'approved':True})
        with self.store.write() as db:
            for n,pin in self.members.items():
                for generation in range(1,31):
                    facts=[{'reference':f'example/repo-{(n-1)%29+1}#900','kind':'issue','state':'open' if generation==30 else 'closed','state_reason':'completed'}]
                    db.execute('INSERT INTO dependency_checks VALUES(?,?,?,?,?,?,?,?)',
                        (f'check-{n}-{generation}','u'+str(n),pin['member_id'],pin['revision_id'],generation,int(generation!=30),json.dumps(facts),1000))
            for repo in range(1,30):
                for generation,status in [(1,'completed'),(2,'completed'),(3,'running')]:
                    scan=f'scan-{repo}-{generation}'
                    db.execute('INSERT INTO environment_scans(id,repository_id,source_revision,status,created_at) VALUES(?,?,?,?,?)',
                               (scan,repo,'source',status,generation))
                    if status=='completed':
                        for revision in (1,2):
                            db.execute('INSERT INTO environment_proposals(id,scan_id,revision,status,proposal_json,created_at) VALUES(?,?,?,?,?,?)',
                                (f'{scan}-{revision}',scan,revision,'superseded' if revision==2 else 'proposed',json.dumps({'repo_id':repo,'generation':generation,'services':[]}),generation))
        self.views=PipelineViews(self.store,repositories=lambda runtime:self.settings.repository_views(runtime=runtime),
                                 runtime=validate({}),clock=lambda:1000)

    def test_29_repositories_and_50_history_cards_have_full_parity_and_indexed_checks(self):
        captured=[]; connect=self.store._connect_path
        class Capture:
            def __init__(self,db):self.db=db
            def execute(self,sql,*args):captured.append((sql,args));return self.db.execute(sql,*args)
            def __getattr__(self,name):return getattr(self.db,name)
        with patch.object(self.store,'_connect_path',lambda *a,**kw:Capture(connect(*a,**kw))):
            board=self.views.factory(section='board'); history=self.views.factory(section='completed')
        self.assertEqual((len(board['issues']),len(history['issues']),len(board['repositories'])),(20,50,29))
        self.assertTrue(all(row['blocked_dependencies']==[900] for row in history['issues']))
        queries=[(sql,args) for sql,args in captured if 'WITH requested AS MATERIALIZED' in sql]
        self.assertEqual(len(queries),2)
        self.assertFalse(any('SELECT facts_json FROM dependency_checks WHERE unit_id=' in sql for sql,_ in captured))
        self.assertEqual(sum('SELECT repository_id,proposal_json FROM (SELECT s.repository_id,p.proposal_json' in sql for sql,_ in captured),2)
        with self.store.read_snapshot() as db:
            for sql,args in queries:
                plan=[row['detail'] for row in db.execute('EXPLAIN QUERY PLAN '+sql,*args)]
                self.assertTrue(any('dependency_current' in row and 'member_id=? AND requirements_revision_id=?' in row for row in plan),plan)
                self.assertFalse(any(row.startswith('SCAN d') or row.startswith('SCAN c ') for row in plan),plan)
        parity(self,self.views,section='board'); parity(self,self.views,section='completed')
        self.assertEqual(len(self.views.issues(key='70')['issue']['prerequisites']),1)

    def test_page_dependency_lookup_does_not_walk_unrelated_retained_checks(self):
        captured=[]
        class Capture:
            def __init__(self,db):self.db=db
            def execute(self,sql,*args):
                if 'WITH requested AS MATERIALIZED' in sql:captured.append((sql,args))
                return self.db.execute(sql,*args)
        with self.store.read_snapshot() as db:
            ActivityReads(Capture(db),Queries._issue_contexts(db,['70']))
        sql,args=captured[0]
        def measured():
            steps=[0]
            with self.store.read_snapshot() as db:
                def progress():steps[0]+=1;return 0
                db.set_progress_handler(progress,10)
                try:result=[dict(row) for row in db.execute(sql,*args)]
                finally:db.set_progress_handler(None,0)
            return result,steps[0]
        before,cost=measured()
        pin=self.members[1]
        with self.store.write() as db:
            db.executemany('INSERT INTO dependency_checks VALUES(?,?,?,?,?,?,?,?)',
                [(f'unrelated-{n}','u1',pin['member_id'],pin['revision_id'],n,1,'[]',1000) for n in range(31,9031)])
        after,following=measured()
        self.assertEqual(after,before)
        self.assertLessEqual(following,cost*2+10,(cost,following))

    def test_current_snapshot_and_next_projection_see_independent_evidence(self):
        with self.store.read_session():
            with self.store.read_snapshot() as db:
                contexts=Queries._issue_contexts(db,['70']); first=ActivityReads(db,contexts)
            pin=self.members[70]
            def observe():
                with self.store.write() as writer:
                    writer.execute('INSERT INTO dependency_checks VALUES(?,?,?,?,?,?,?,?)',
                        ('new-check','u70',pin['member_id'],pin['revision_id'],31,1,'[]',1001))
            with ThreadPoolExecutor(max_workers=1) as executor:executor.submit(observe).result()
            with self.store.read_snapshot() as db:
                second=ActivityReads(db,contexts)
            self.assertEqual(first.checks['70']['facts_json'],second.checks['70']['facts_json'])
        with self.store.read_snapshot() as db:
            self.assertEqual(ActivityReads(db,Queries._issue_contexts(db,['70'])).checks['70']['facts_json'],'[]')

    def test_repository_proposals_keep_latest_eligible_revision_and_running_scan(self):
        service=RepositoryEnvironments(self.settings)
        runtime,proposals,scans=service.snapshot(runtime=validate({}))
        self.assertEqual(len(proposals),29)
        for repo in range(1,30):
            self.assertEqual(proposals[str(repo)]['generation'],2)
            self.assertEqual(scans[str(repo)]['status'],'running')
        with self.store.write() as db:db.execute("UPDATE environment_proposals SET status='superseded'")
        self.assertEqual(service.snapshot(runtime=runtime)[1],{})
