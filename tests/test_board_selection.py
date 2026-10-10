"""Board candidate bounds preserve complete canonical response authority."""
import tempfile
import unittest
from unittest.mock import Mock,patch

from forge.factory.config import validate
from forge.factory.store_inventory import StoreInventory
from forge.factory.store_views import PipelineViews,CURRENT_MEMBERS_SQL,CURRENT_SOURCE_SQL
from forge.store.db import Store
from tests import test_completed_selection as completed
from tests import test_delivery_execution as delivery
from tests import test_operator_status_matrix as matrix


class BroadBoardConnection:
    """Restore the previous broad query and unconditional discovery selection."""
    def __init__(self,db):self.db=db
    def execute(self,sql,*args):
        if CURRENT_MEMBERS_SQL[5:] in sql:
            sql=sql.replace(','+CURRENT_MEMBERS_SQL[5:],' ').replace(CURRENT_SOURCE_SQL,'public_issue_index x')
        if sql=='SELECT 1 FROM issue_discovery_observations LIMIT 1':
            return self.db.execute('SELECT 1')
        return self.db.execute(sql,*args)
    def __getattr__(self,k):return getattr(self.db,k)


def parity(test,store,*,keys=(),clock=1000):
    views=PipelineViews(store,runtime=validate({}),clock=lambda:clock)
    options=[{},dict(section='board'),dict(section='runners'),dict(section='completed')]
    options.extend(dict(issue=key) for key in keys)
    actual=[views.factory(**args) for args in options]
    original=store._connect_path
    with patch.object(store,'_connect_path',lambda *a,**kw:BroadBoardConnection(original(*a,**kw))):
        for args,result in zip(options,actual):
            with test.subTest(options=args):test.assertEqual(views.factory(**args),result)
    return actual[1]


class BoardMatrixTests(unittest.TestCase):
    def test_all_operator_states_and_disabled_repository_keep_full_responses(self):
        for case in matrix.MATRIX:
            with self.subTest(state=case['name']),tempfile.TemporaryDirectory() as root:
                fixture=matrix.OperatorStatusMatrixTests();fixture.prepare(root,case['name'])
                parity(self,fixture.store,keys=['1:1'],clock=100)
        with tempfile.TemporaryDirectory() as root:
            fixture=matrix.OperatorStatusMatrixTests();fixture.prepare(root,'queued')
            with fixture.store.write() as db:db.execute('UPDATE repositories SET enabled=0')
            board=parity(self,fixture.store,keys=['1:1'],clock=100)
            self.assertEqual(board['issues'][0]['reason_code'],'repository_setup')

    def test_group_scope_and_mandatory_recovery_remain_canonical(self):
        fixture=delivery.DeliveryExecutionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.compose();owner,plan,_=fixture.plan();first,one=fixture.child(plan,'0');second,two=fixture.child(plan,'1')
        board=parity(self,fixture.store,keys=['issue-0','issue-1'],clock=100)
        self.assertEqual(len(board['issues']),2)
        fixture.life.record_failure(fixture.command(),first['job_id'],execution_fence='fence-0',input_manifest=one,evidence={'reason':'code_failure'})
        board=parity(self,fixture.store,keys=['issue-0','issue-1'],clock=100)
        self.assertEqual([r['key'] for r in board['issues']],['issue-1'])
        fixture.life.open_gate(fixture.command(),'label_hold',member_id=fixture.members['issue-1'],details={'label':'hold'})
        parity(self,fixture.store,keys=['issue-0','issue-1'],clock=100)


class FormerMembershipBoardTests(unittest.TestCase):
    setUp=completed.CompletedSelectionTests.setUp

    def test_closed_reopened_dropped_released_and_dissolved_members_keep_scope(self):
        parity(self,self.store,keys=['1:'+str(n) for n in range(1,14)])

    def test_actual_plan_reads_candidate_members_then_issue_primary_keys(self):
        # Unrelated untracked inventory and terminal membership history must not
        # become the outer input to the active board's lifecycle classifier.
        inventory=StoreInventory(self.store,Mock(),Mock(),config=validate({}),clock=lambda:1000)
        for number in range(20,220):
            inventory.record({'id':1,'name':'example/repo'},dict(number=number,state='closed',state_reason='completed',title='Unrelated',body='',updated_at='2026-10-10T01:00:00Z',labels=[],assignees=[]),'req')
        captured=[];original=self.store._connect_path
        class Capture:
            def __init__(self,db):self.db=db
            def execute(self,sql,*args):
                if CURRENT_MEMBERS_SQL[5:] in sql:captured.append((sql,args))
                return self.db.execute(sql,*args)
            def __getattr__(self,k):return getattr(self.db,k)
        with patch.object(self.store,'_connect_path',lambda *a,**kw:Capture(original(*a,**kw))):
            PipelineViews(self.store,clock=lambda:1000).factory(section='board')
        self.assertEqual(len(captured),1)
        with self.store.read_snapshot() as db:
            for sql,args in captured:
                plan=[r['detail'] for r in db.execute('EXPLAIN QUERY PLAN '+sql,*args)]
                self.assertTrue(any('MATERIALIZE current_members' in r for r in plan),plan)
                self.assertTrue(any('SEARCH i USING INDEX sqlite_autoindex_issues_1 (id=?)' in r for r in plan),plan)
                self.assertFalse(any(r.startswith('SCAN i ') for r in plan),plan)
        parity(self,self.store)


class DiscoveryBoardTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store.create(temporary.name)
        self.inventory=StoreInventory(self.store,Mock(),Mock(),config=validate({}),clock=lambda:100)

    def record(self,number,*,state='open',body='',stamp='2026-10-10T01:00:00Z'):
        self.inventory.record({'id':1,'name':'example/repo'},dict(number=number,state=state,state_reason='completed' if state=='closed' else None,title='Untracked',body=body,updated_at=stamp,labels=[],assignees=[]),'req')

    def test_empty_discovery_input_never_prepares_expanded_status_selector(self):
        self.record(1);queries=[];original=self.store._connect_path
        def connected(*a,**kw):
            db=original(*a,**kw);db.set_trace_callback(queries.append);return db
        with patch.object(self.store,'_connect_path',side_effect=connected):
            board=PipelineViews(self.store,clock=lambda:100).factory(section='board')
        self.assertEqual(board['issues'],[])
        self.assertIn('SELECT 1 FROM issue_discovery_observations LIMIT 1',queries)
        self.assertFalse(any('FROM issue_discovery_observations d CROSS JOIN public_issue_index' in q for q in queries))
        parity(self,self.store,keys=['1:1'],clock=100)

    def test_current_failure_closed_issue_and_obsolete_failure_are_classified(self):
        for n,reason in enumerate(('requirements_stale','remote_timestamp_invalid','admission_conflict'),1):
            self.record(n);self.inventory.failure('1:'+str(n),reason)
        self.record(4,state='closed');self.inventory.failure('1:4','requirements_stale')
        self.record(5);self.inventory.failure('1:5','requirements_stale')
        self.record(5,body='Changed revision',stamp='2026-10-10T01:01:00Z')
        board=parity(self,self.store,keys=['1:'+str(n) for n in range(1,6)],clock=100)
        self.assertEqual({r['key'] for r in board['issues']},{'1:1','1:2','1:3'})
        # A later request reads the new failure state, with no authority cache.
        for n in range(1,6):self.inventory.clear_failure('1:'+str(n))
        self.assertEqual(parity(self,self.store,clock=100)['issues'],[])
