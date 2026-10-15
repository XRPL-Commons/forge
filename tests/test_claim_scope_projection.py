"""SQL and API projections agree on captured claim requirement pins."""
import json
import unittest

from forge.factory.issue_selection import IssueSelection
from forge.factory.status_projection import operator_status
from forge.store.queries import Queries
from forge.store.executions import Executions
from tests import test_delivery_execution as fixtures


class ClaimScopeProjectionTests(unittest.TestCase):
    def test_claim_blocks_only_its_live_member_revision_in_sql_and_api(self):
        fixture=fixtures.DeliveryExecutionTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        fixture.compose();parent,plan,children=fixture.plan()
        child,manifest=fixture.child(plan,'0')
        Executions(fixture.store,clock=lambda:100).collect(fixture.command(),child['job_id'],
            execution_fence='fence-0',capture={'result':None,'logs':''})
        fixture.clean(child,'fence-0')
        member=fixture.members['issue-0']
        with fixture.store.read_snapshot() as db:
            revision=db.execute('SELECT requirements_revision_id FROM unit_issues WHERE id=?',(member,)).fetchone()[0]
        command=fixture.command()
        with fixture.store.write() as db:
            job=dict(db.execute('SELECT * FROM jobs WHERE id=?',(child['job_id'],)).fetchone())
            fixture.life._result(db,command,job,{},'deferred')
            fixture.life._open_gate(db,command,'dependency',{'source':'build_claim','job_id':child['job_id'],
                'requirements_by_member':{member:revision},'references':[]},job_id=child['job_id'])
        def check(expected):
            with fixture.store.read_snapshot() as db:
                raw={row['id']:row['indexed_reason_code'] for row in db.execute('SELECT id,indexed_reason_code FROM issue_selection_facts')}
                self.assertEqual({key:raw[key] for key in expected},expected)
                selected={row['id']:row['public_status'] for row in IssueSelection(db).execute('SELECT id,public_status FROM public_issue_index')}
            for issue,reason in expected.items():
                row=Queries(fixture.store).issue(issue)
                self.assertEqual(row['lifecycle']['reason_code'],reason)
                self.assertEqual(selected[issue],operator_status(row)['code'])
        check({'issue-0':'dependency','issue-1':None})
        with fixture.store.write() as db:
            db.execute('INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) '
                "SELECT 'new-revision',issue_id,title,body,'new-content','new-requirements',requirements_json,observed_at+1 FROM issue_revisions WHERE id=?",(revision,))
            db.execute("UPDATE issues SET current_revision_id='new-revision' WHERE id='issue-0'")
            db.execute("UPDATE unit_issues SET requirements_revision_id='new-revision' WHERE id=?",(member,))
        check({'issue-0':None,'issue-1':None})
