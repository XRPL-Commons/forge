"""Open-gate scope hydration is independent of unrelated retained job history."""
import json
import unittest
from unittest.mock import patch

from forge.store import transitions
from forge.store.executions import Executions, Launch
from tests import test_delivery_execution as fixtures


def broad_gates(db,unit_id):
    """Exact previous canonical reader, used solely for result parity."""
    rows=db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL',(unit_id,)).fetchall()
    scopes={}
    for row in db.execute('SELECT p.job_id,p.member_id FROM job_members p JOIN jobs j ON j.id=p.job_id WHERE j.unit_id=?',(unit_id,)):
        scopes.setdefault(row['job_id'],[]).append(row['member_id'])
    return sorted((dict(row)|{'job_member_ids':scopes.get(row['job_id'],[])} for row in rows),
                  key=lambda gate:(transitions.GATE_PRIORITY[gate['code']],gate['created_at'],gate['id']))


def add_history(store,template_job,count=2000):
    """Retained completed executions with real attempt FKs and trigger-pinned scope."""
    with store.write() as db:
        job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(template_job,)).fetchone())
        attempt=dict(db.execute('SELECT * FROM attempts WHERE id=?',(job['attempt_id'],)).fetchone())
        for number in range(count):
            previous_attempt={**attempt,'id':'historical-attempt-'+str(number),'created_at':1+number/10000}
            previous_job={**job,'id':'historical-job-'+str(number),'attempt_id':previous_attempt['id'],
                          'phase':'cleaned','outcome':'pass','fenced':1,'cleanup':1,'created_at':previous_attempt['created_at']}
            for table,value in [('attempts',previous_attempt),('jobs',previous_job)]:
                db.execute('INSERT INTO '+table+'('+','.join(value)+') VALUES('+','.join('?' for _ in value)+')',tuple(value.values()))


class OpenGateScopeTests(unittest.TestCase):
    setUp=fixtures.DeliveryExecutionTests.setUp
    compose=fixtures.DeliveryExecutionTests.compose
    command=fixtures.DeliveryExecutionTests.command
    manifest=fixtures.DeliveryExecutionTests.manifest
    plan=fixtures.DeliveryExecutionTests.plan
    clean=fixtures.DeliveryExecutionTests.clean
    child=fixtures.DeliveryExecutionTests.child

    def assert_parity(self):
        with self.store.read_snapshot() as db:
            self.assertEqual(transitions._gates(db,'batch'),broad_gates(db,'batch'))
            unit=dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
            jobs=[row[0] for row in db.execute("SELECT job_id FROM unit_gates WHERE unit_id='batch' AND resolved_at IS NULL AND job_id IS NOT NULL")]
            for member in [None,*self.members.values()]:
                for job in [None,*jobs]:
                    actual=transitions.applicable_gates(db,unit,member,unit['current_candidate_id'],job_id=job)
                    with patch.object(transitions,'_gates',broad_gates):
                        self.assertEqual(actual,transitions.applicable_gates(db,unit,member,unit['current_candidate_id'],job_id=job))

    def test_owner_scope_and_job_scopes_keep_full_priority_and_member_filtering(self):
        self.compose()
        manifest=self.manifest()
        parent=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='owner',input_manifest=manifest,
            launch=Launch('forge/owner','builder[bot]','codex',1000,{},{}))
        Executions(self.store,clock=lambda:100).collect(self.command(),parent['job_id'],execution_fence='owner',capture={'logs':'fixture'})
        self.life.open_gate(self.command(),'label_hold',member_id=self.members['issue-0'],details={'label':'hold'})
        self.assert_parity()
        with self.store.read_snapshot() as db:
            gates=transitions._gates(db,'batch')
            process=[gate for gate in gates if gate['job_id'] is not None]
            self.assertTrue(process)
            self.assertTrue(all(set(gate['job_member_ids'])==set(self.members.values()) for gate in process))

    def tasks(self):
        self.compose(); parent,plan,_=self.plan()
        first,one=self.child(plan,'0'); second,two=self.child(plan,'1')
        Executions(self.store,clock=lambda:100).collect(self.command(),first['job_id'],execution_fence='fence-0',capture={'logs':'fixture'})
        self.life.record_failure(self.command(),second['job_id'],execution_fence='fence-1',input_manifest=two,evidence={'reason':'code_failure'})
        return parent,first,second

    def test_task_cleanup_result_and_recovery_scopes_ignore_large_completed_history(self):
        parent,first,second=self.tasks()
        self.assert_parity()
        with self.store.read_snapshot() as db:before=transitions._gates(db,'batch')
        add_history(self.store,parent['job_id'])
        self.assert_parity()
        with self.store.read_snapshot() as db:
            after=transitions._gates(db,'batch')
            self.assertEqual(after,before)
            self.assertTrue({'cleanup_pending','result_pending','recovery_pending'} <= {gate['code'] for gate in after})
            for gate in after:
                if gate['job_id']==first['job_id']:self.assertEqual(gate['job_member_ids'],[self.members['issue-0']])
                if gate['job_id']==second['job_id']:self.assertEqual(gate['job_member_ids'],[self.members['issue-1']])

    def test_no_process_gate_skips_job_history_and_new_gate_is_read_fresh(self):
        self.compose(); parent,_,_=self.plan(); add_history(self.store,parent['job_id'])
        self.life.open_gate(self.command(),'label_hold',member_id=self.members['issue-0'],details={'label':'hold'})
        calls=[]
        class Capture:
            def __init__(self,db):self.db=db
            def execute(self,sql,*args):calls.append(sql);return self.db.execute(sql,*args)
        with self.store.write() as db:
            initial=transitions._gates(Capture(db),'batch')
            self.assertEqual(initial,broad_gates(db,'batch'))
            self.assertEqual(len(calls),1)
            # A later mutation read must see a newly opened process gate in this
            # very transaction; the optimization retains no scope cache.
            command=self.command()
            gate=self.life._open_gate(db,command,'result_pending',{'job_id':parent['job_id']},job_id=parent['job_id'])
            following=transitions._gates(db,'batch')
            self.assertEqual(following,broad_gates(db,'batch'))
            self.assertEqual(set(next(row for row in following if row['id']==gate)['job_member_ids']),set(self.members.values()))

    def test_vm_work_and_plan_are_bounded_by_open_gate_job_ids(self):
        parent,_,_=self.tasks()
        calls=[]
        class Capture:
            def __init__(self,db):self.db=db
            def execute(self,sql,*args):calls.append((sql,args));return self.db.execute(sql,*args)
        def measure():
            steps=[0]
            with self.store.read_snapshot() as db:
                def progress():steps[0]+=1;return 0
                db.set_progress_handler(progress,10)
                try:value=transitions._gates(Capture(db),'batch')
                finally:db.set_progress_handler(None,0)
            return value,steps[0]
        before,cost=measure();add_history(self.store,parent['job_id']);after,following=measure()
        self.assertEqual(before,after)
        self.assertLessEqual(following,cost*2+10,(cost,following))
        sql,args=next((sql,args) for sql,args in calls if 'json_each' in sql)
        requested=json.loads(args[0][0])
        self.assertEqual(set(requested),{gate['job_id'] for gate in after if gate['job_id'] is not None})
        with self.store.read_snapshot() as db:
            plan=[row['detail'] for row in db.execute('EXPLAIN QUERY PLAN '+sql,*args)]
        self.assertTrue(any('sqlite_autoindex_jobs_1 (id=?)' in row for row in plan),plan)
        self.assertTrue(any('sqlite_autoindex_job_members_1 (job_id=?)' in row for row in plan),plan)
        self.assertFalse(any('jobs_unit_history' in row or 'SCAN j ' in row for row in plan),plan)
