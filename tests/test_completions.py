import fcntl
import json
from pathlib import Path
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from forge.completions import refresh
from forge.control import atomic_json
from forge.observability import connect, normalized, save_item


class CompletionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.repo = 'o/r'
        self.previous = {'item_details': {'1': {'number': 1, 'kind': 'issue'}, '2': {'number': 2, 'kind': 'pr'}}}
        self.responses = {
            1: {'number': 1, 'title': 'Issue', 'html_url': 'https://github.com/o/r/issues/1',
                'state': 'closed', 'state_reason': 'completed', 'closed_at': '2026-09-08T00:56:53Z',
                'updated_at': '2026-09-08T00:56:53Z', 'labels': []},
            2: {'number': 2, 'title': 'PR', 'html_url': 'https://github.com/o/r/pull/2', 'state': 'closed',
                'closed_at': '2026-09-08T00:56:52Z', 'merged_at': '2026-09-08T00:56:52Z', 'merged': True,
                'updated_at': '2026-09-08T00:56:55Z', 'labels': [], 'head': {'sha': 'a'*40}, 'base': {'sha': 'b'*40}}}
        self.calls = []
        self.fail = False

    def fake_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if self.fail:
            raise subprocess.TimeoutExpired('gh', 2)
        return SimpleNamespace(returncode=0, stdout=json.dumps(self.responses[int(args[-1].rsplit('/',1)[-1])]))

    def run_refresh(self, previous=None, open_items=(), now=100):
        return refresh(self.state, self.repo, list(open_items), self.previous if previous is None else previous,
                       run=self.fake_run, now=now)

    def read(self, number):
        db=connect(self.state.absolute())
        if db is None:return None
        try:
            row=db.execute('SELECT data,evidence_updated_at FROM items WHERE repo=? AND number=?',(self.repo,number)).fetchone()
            return (json.loads(row['data']),row['evidence_updated_at']) if row else None
        finally:db.close()

    def test_missing_work_verified_closed_with_actual_dates(self):
        result=self.run_refresh()
        self.assertEqual(result['updated'],[1,2])
        self.assertEqual(self.read(1)[0]['closed_at'],'2026-09-08T00:56:53Z')
        self.assertEqual(self.read(2)[0]['merged_at'],'2026-09-08T00:56:52Z')
        self.assertEqual(self.read(2)[0]['stage'],'completed')
        self.assertTrue(all(call[1]['timeout']<=2 for call in self.calls))

    def test_closed_unmerged_and_not_planned_are_not_completed(self):
        self.responses[1]['state_reason']='not_planned'
        self.responses[2].update(merged_at=None,merged=False)
        self.run_refresh()
        self.assertEqual(self.read(1)[0]['stage'],'closed')
        self.assertEqual(self.read(2)[0]['stage'],'closed')

    def test_open_or_invalid_identity_is_never_recorded_closed(self):
        for change in [{'number':99},{'html_url':'https://github.com/other/r/issues/1'},
                       {'pull_request':{}},{'state':'open'},{'closed_at':None},{'closed_at':'invalid'}]:
            with self.subTest(change=change):
                saved=self.responses[1].copy();self.responses[1].update(change)
                self.run_refresh(previous={'item_details':{'1':{'number':1,'kind':'issue'}}},now=1000+len(self.calls)*100)
                self.assertIsNone(self.read(1));self.responses[1]=saved

    def test_invalid_pr_kind_or_merge_state_cannot_be_completed(self):
        for change in [{'head':None},{'merged':False},{'merged_at':'invalid'},
                       {'html_url':'https://github.com/o/r/issues/2'}]:
            with self.subTest(change=change):
                saved=self.responses[2].copy();self.responses[2].update(change)
                self.run_refresh(previous={'item_details':{'2':{'number':2,'kind':'pr'}}},now=1000+len(self.calls)*100)
                self.assertIsNone(self.read(2));self.responses[2]=saved

    def test_api_failure_retries_without_inventing_completion(self):
        self.fail=True
        result=self.run_refresh()
        self.assertEqual(len(result['pending']),2)
        self.assertIsNone(self.read(1))
        self.fail=False
        result=self.run_refresh(previous={},now=200)
        self.assertEqual(result['updated'],[1,2])

    def test_collector_lock_is_nonblocking_and_retains_pending(self):
        with (self.state/'observability.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            result=self.run_refresh()
            self.assertEqual(result['status'],'collector_busy')
            self.assertEqual(self.calls,[])
        self.assertEqual(self.run_refresh(previous={})['updated'],[1,2])

    def test_global_request_budget_bounds_all_repositories(self):
        atomic_json(self.state/'completion-budget.json',{'window':100,'requests':4,'seconds':0})
        self.assertEqual(self.run_refresh()['status'],'budget_deferred')
        self.assertEqual(self.calls,[])
        atomic_json(self.state/'completion-budget.json',{'window':100,'requests':0,'seconds':4})
        self.assertEqual(self.run_refresh()['status'],'budget_deferred')
        self.assertEqual(self.calls,[])

    def test_preserves_evidence_and_does_not_advance_repository_coverage(self):
        db=connect(self.state,write=True)
        raw={**self.responses[2],'state':'open','merged_at':None,'pull_request':{},'labels':[{'name':'ready-for-audit'}]}
        data=normalized(self.repo,raw)
        data.update(evidence=[{'kind':'audit','body':'Recorded evidence'}],evidence_status='ok',
                    linked_issues=[{'repo':self.repo,'number':1}],checks=['old-check'],summary_status='ok')
        with db:
            save_item(db,self.repo,data,'2026-09-08T00:00:00Z','2026-09-08T00:00:00Z')
            db.execute('INSERT INTO repositories VALUES(?,?,?,?,?,?)',(self.repo,'old','coverage',None,'ok','cursor'))
        db.close()
        self.run_refresh(previous={})
        updated,stamp=self.read(2)
        self.assertEqual(updated['evidence'],data['evidence'])
        self.assertEqual(updated['linked_issues'],data['linked_issues'])
        self.assertEqual(stamp,'2026-09-08T00:00:00Z')
        db=connect(self.state.absolute())
        self.assertEqual(tuple(db.execute('SELECT observed_at,coverage_since,cursor FROM repositories').fetchone()),('old','coverage','cursor'))
        db.close()

    def test_items_still_in_open_inventory_need_no_targeted_read(self):
        self.assertEqual(self.run_refresh(open_items=[{'number':1},{'number':2}])['status'],'idle')
        self.assertEqual(self.calls,[])


if __name__=='__main__':unittest.main()
