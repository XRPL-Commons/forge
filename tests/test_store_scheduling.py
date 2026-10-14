import json
import tempfile
import unittest

from forge.store.db import Store
from forge.store.models import Command
from forge.store.queries import Queries
from forge.store.scheduling import Scheduling, current_in
from forge.store.executions import Executions, Launch
from forge.factory.issue_selection import IssueSelection
from forge.factory.status_projection import operator_status


class SchedulingTests(unittest.TestCase):
    def setUp(self):
        directory=tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store=Store.create(directory.name)
        self.now=1000.
        self.life=Executions(self.store,clock=lambda:self.now)
        self.schedules=Scheduling(self.store,clock=lambda:self.now)
        self.member=self.life.admit_issue(request_id='admit',actor_id='operator',unit_id='u',issue_id='i',
            repository_id=1,repository_name='owner/repo',issue_number=1,title='Issue',body='body',requirements={},
            observed_at=self.now)['member_id']
        self.serial=0

    def command(self):
        self.serial+=1
        return Command(str(self.serial),'operator','u',Queries(self.store).unit('u')['version'])

    def test_admission_has_real_schedule_and_sql_status_matches_projection(self):
        issue=Queries(self.store).issue('i')
        action=issue['lifecycle']['next_action']
        self.assertEqual((action['owner'],action['reason'],action['due_at']),('dispatch','admitted',self.now))
        self.assertEqual(operator_status(issue)['code'],'queued')
        with self.store.read_snapshot() as db:
            status=IssueSelection(db).execute("SELECT public_status FROM public_issue_index WHERE id='i'").fetchone()[0]
        self.assertEqual(status,'queued')
        self.schedules.clear('u',owner='dispatch')
        issue=Queries(self.store).issue('i')
        self.assertEqual(operator_status(issue)['code'],'waiting')
        with self.store.read_snapshot() as db:
            status=IssueSelection(db).execute("SELECT public_status FROM public_issue_index WHERE id='i'").fetchone()[0]
        self.assertEqual(status,'waiting')

    def test_restart_preserves_due_reason_and_changed_assignment_invalidates_it(self):
        self.schedules.record('u',owner='dispatch',reason='role_capacity',due_at=self.now+500)
        restarted=Scheduling(Store.open(self.store.root),clock=lambda:self.now)
        self.now+=400
        self.assertEqual(restarted.cycle()['items'],[])
        self.life.pause(self.command())
        self.assertIsNone(Queries(self.store).issue('i')['lifecycle']['next_action'])
        self.now+=1000
        self.assertEqual(restarted.cycle()['items'],[])

    def test_unowned_stall_opens_scoped_forge_gate_and_explicit_recheck_preserves_pause(self):
        self.schedules.clear('u',owner='dispatch')
        self.assertEqual(self.schedules.cycle()['items'],[])
        self.now+=299
        self.assertEqual(self.schedules.cycle()['items'],[])
        self.now+=1
        stalled=self.schedules.cycle()['items'][0]
        gate_id=stalled['gate_id']
        issue=Queries(self.store).issue('i')
        self.assertEqual(issue['lifecycle']['reason_code'],'forge_problem')
        self.assertEqual(operator_status(issue)['owner'],'forge')
        self.life.pause(self.command())
        before=Queries(self.store).unit('u')['version']
        self.assertEqual(self.schedules.recheck(self.command(),gate_id)['pending'],'operator_recheck')
        self.assertEqual(Queries(self.store).unit('u')['version'],before)
        self.schedules.recheck(self.command(),gate_id,explicit=True)
        issue=Queries(self.store).issue('i')
        self.assertEqual(issue['lifecycle']['reason_code'],'operator_pause')
        self.assertEqual(issue['lifecycle']['next_action']['reason'],'operator_recheck')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM unit_gates WHERE resolved_at IS NULL').fetchone()[0],1)

    def test_maintenance_does_not_create_an_orphan_or_charge(self):
        self.schedules.clear('u',owner='dispatch')
        self.schedules.cycle(maintenance=True)
        self.now+=1000
        self.assertEqual(self.schedules.cycle(maintenance=True)['items'],[])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM scheduling_orphans').fetchone()[0],0)

    def test_staging_import_is_never_mutated_by_derivative_schedule_records(self):
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        self.schedules.record('u',owner='dispatch',reason='failure')
        self.schedules.clear('u',owner='dispatch')
        self.schedules.cycle()
        self.assertEqual(Queries(self.store).issue('i')['lifecycle']['next_action']['reason'],'admitted')

    def test_revoked_result_receipt_invalidates_schedule_without_unit_version_change(self):
        revision=Queries(self.store).issue('i')['current_revision_id']
        manifest={'requirements_revision_id':revision,'base_oid':'base','policy_hash':'policy'}
        job=self.life.reserve_job(self.command(),'build',slot='build-1',execution_fence='fence',input_manifest=manifest,
            member_id=self.member,launch=Launch('branch','build[bot]','codex',2000,{},{}))['job_id']
        self.life.collect(self.command(),job,execution_fence='fence',capture={'result':None,'logs':''})
        self.schedules.record('u',owner='results',reason='policy_reconciliation',job_id=job)
        self.assertIsNotNone(self.schedules.pending('u','results',job_id=job))
        before=Queries(self.store).unit('u')['version']
        with self.store.write() as db:
            db.execute('UPDATE job_execution_state SET result_revoked=1 WHERE job_id=?',(job,))
        self.assertEqual(Queries(self.store).unit('u')['version'],before)
        self.assertIsNone(self.schedules.pending('u','results',job_id=job))
        self.assertIsNone(Queries(self.store).issue('i')['lifecycle']['next_action'])
