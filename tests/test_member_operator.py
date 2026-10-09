"""Issue decisions preserve independent physical executions in an approved batch."""
from copy import deepcopy
import unittest

from forge.factory.config import validate
from forge.factory.store_operator import OperatorActions
from forge.factory.store_views import action_revision
from forge.store.models import TransitionError
from tests import test_delivery_execution as delivery


class Remote:
    def __init__(self, count):
        self.issues={number:dict(number=number,state='open',labels=[],assignees=[]) for number in range(1,count+1)}
        self.calls=[]
    def issue(self,item):
        return deepcopy(self.issues[item['number']]),{'acceptance':['works']}
    def call(self,role,item,method,path,payload=None):
        self.calls.append((method,path,payload))
        issue=self.issues[item['number']]
        if method=='POST' and path.endswith('/labels'):
            issue['labels']=sorted(set(issue['labels'])|set(payload['labels']))
        elif method=='DELETE' and '/labels/' in path:
            from urllib.parse import unquote
            issue['labels'].remove(unquote(path.split('/labels/')[1]))
        else:raise AssertionError((method,path,payload))
        return deepcopy(issue)


class MemberOperatorTests(unittest.TestCase):
    setUp=delivery.DeliveryExecutionTests.setUp
    command=delivery.DeliveryExecutionTests.command
    compose=delivery.DeliveryExecutionTests.compose
    manifest=delivery.DeliveryExecutionTests.manifest
    clean=delivery.DeliveryExecutionTests.clean
    plan=delivery.DeliveryExecutionTests.plan
    child=delivery.DeliveryExecutionTests.child
    accept=delivery.DeliveryExecutionTests.accept

    def setup_actions(self,count=2,dependencies=()):
        self.compose(count,dependencies)
        self.remote=Remote(count)
        self.actions=OperatorActions(self.store,self.remote,actor_id='github:42',config=validate({}),clock=lambda:100)
        return self.plan()
    def request(self,issue,action):
        with self.store.read_snapshot() as db: revision=action_revision(db,issue)
        return self.actions.request(issue,action,revision,'Operator decision')
    def jobs(self):
        with self.store.read_snapshot() as db:
            return {r['id']:dict(r) for r in db.execute("SELECT * FROM jobs WHERE unit_id='batch'")}

    def test_pause_one_child_preserves_independent_completion_and_budget(self):
        _,plan,_=self.setup_actions()
        first,one=self.child(plan,'0'); second,two=self.child(plan,'1')
        request=self.request('issue-0','pause')
        jobs=self.jobs()
        self.assertEqual(jobs[first['job_id']]['fenced'],1)
        self.assertEqual(jobs[second['job_id']]['fenced'],0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],2)
        self.accept(second,two,'1')
        self.assertTrue(self.actions.step(request)['applied'])
        with self.store.read_snapshot() as db:
            gates=[dict(r) for r in db.execute("SELECT * FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL")]
            self.assertEqual([g['member_id'] for g in gates],[self.members['issue-0']])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def test_pause_predecessor_fences_dependent_but_keeps_independent_child(self):
        _,plan,_=self.setup_actions(3,[{'issue_id':'issue-1','prerequisite_issue_id':'issue-0'}])
        first,one=self.child(plan,'0');self.accept(first,one,'0')
        dependent,_=self.child(plan,'1');independent,three=self.child(plan,'2')
        request=self.request('issue-0','pause')
        jobs=self.jobs()
        self.assertEqual(jobs[dependent['job_id']]['fenced'],1)
        self.assertEqual(jobs[independent['job_id']]['fenced'],0)
        self.accept(independent,three,'2')
        self.assertTrue(self.actions.step(request)['applied'])

    def test_resume_waits_only_for_affected_cleanup_and_keeps_other_pause(self):
        _,plan,_=self.setup_actions()
        first,one=self.child(plan,'0'); second,two=self.child(plan,'1')
        pause=self.request('issue-0','pause'); self.actions.step(pause)
        self.actions.stop_job(second['job_id'],'Pause other child')
        self.clean(first,'fence-0')
        resume=self.request('issue-0','resume')
        self.assertTrue(self.actions.step(resume)['applied'])
        with self.store.read_snapshot() as db:
            self.assertEqual([r[0] for r in db.execute("SELECT member_id FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL")],[self.members['issue-1']])
            self.assertEqual(db.execute('SELECT cleanup FROM jobs WHERE id=?',(second['job_id'],)).fetchone()[0],0)

    def test_concurrent_member_intents_complete_independently(self):
        self.setup_actions()
        first=self.request('issue-0','pause'); second=self.request('issue-1','backlog')
        self.assertTrue(self.actions.step(first)['applied'])
        self.assertTrue(self.actions.step(second)['applied'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='operator_sync' AND resolved_at IS NULL").fetchone()[0],0)

    def test_child_reset_is_scoped_and_retains_cleanup_lease(self):
        _,plan,_=self.setup_actions()
        first,_=self.child(plan,'0'); second,_=self.child(plan,'1')
        self.actions.stop_job(first['job_id'],'Reset child',action='reset')
        jobs=self.jobs()
        self.assertEqual(jobs[first['job_id']]['fenced'],1)
        self.assertEqual(jobs[second['job_id']]['fenced'],0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def test_ordinary_close_cannot_close_a_grouped_delivery(self):
        self.setup_actions()
        with self.assertRaisesRegex(TransitionError,'explicit member removal'):
            self.request('issue-0','close')
        self.assertEqual(self.remote.calls,[])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_issues WHERE unit_id='batch' AND left_at IS NULL").fetchone()[0],2)
