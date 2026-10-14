"""Non-pipeline worker faults retain ownership through cleanup and retry."""
import unittest
from tests import test_repository_planner as planner_fixtures
from tests import test_store_discussions as discussion_fixtures
from forge.store.configuration import Configuration
from forge.store.executions import provider_available
from forge.harnesses.execution import fault

IMAGE='sha256:'+'1'*64


def failed_capture(job, status='harness_error'):
    envelope=dict(version=1,attempt=job['id'],harness_status=status,exit_code=1)
    if status=='harness_error':envelope['harness_error']=fault('ImportError')
    if status=='provider_error':envelope['provider_error']={'code':'unknown'}
    return dict(result=None,logs='',execution_envelope=envelope,
        execution=dict(id=job['container_id'],image_id=IMAGE,exit_protocol='1',running=False,exit_code=1,oom_killed=status=='oom'))


def repaired(store, now):
    config=Configuration(store,clock=lambda:now);new='sha256:'+'2'*64
    config.replace('runtime.json',{'image':new},expected_revision=0)
    config.replace('host.json',{'image':new,'updated_at':now},expected_revision=0)


class PlannerExecutionWaitTests(unittest.TestCase):
    setUp=planner_fixtures.RepositoryPlannerTests.setUp

    def fault(self,status='harness_error'):
        self.job['runner_config']={'image':IMAGE,'provider':'codex','memory':'2g','memory_tiers':['2g','4g']}
        self.controller.config.update(self.job['runner_config'])
        self.plans.reserve(self.request['proposal_id'],self.job,self.context)
        job=self.plans.progress('triage',container_id='container')
        self.runner.inspect.return_value={'status':'exited','running':False}
        self.runner.capture.return_value=failed_capture(job,status)
        return self.controller.drive(job)

    def test_forge_failure_has_owned_wait_and_qualified_image_exit(self):
        self.assertEqual(self.fault()['reason'],'forge_problem')
        self.assertTrue(self.plans.job('triage')['cleanup'])
        self.assertEqual(self.controller.cycle(launch=False)['items'][0]['pending'],'forge_problem')
        repaired(self.store,self.now)
        self.assertEqual(self.controller.cycle(launch=False)['items'][0]['phase'],'queued')
        with self.store.read_snapshot() as db:
            run=db.execute('SELECT * FROM repository_triage_runs').fetchone()
            self.assertEqual(run['status'],'queued');self.assertIsNone(run['execution_id'])
            self.assertEqual(db.execute('SELECT count(*) FROM forge_problem_reports').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_provider_cooldown_survives_controller_restart_and_blocks_account(self):
        self.assertEqual(self.fault('provider_error')['reason'],'provider_error')
        with self.store.read_snapshot() as db:
            self.assertFalse(provider_available(db,'codex',None,{},self.now))
        self.now+=29
        self.assertEqual(self.controller.cycle(launch=False)['items'][0]['pending'],'provider_error')
        self.now+=1
        self.assertEqual(self.controller.cycle(launch=False)['items'][0]['phase'],'queued')
        with self.store.read_snapshot() as db:
            self.assertTrue(provider_available(db,'codex',None,{},self.now))

    def test_oom_selects_larger_memory_on_next_launch(self):
        self.assertEqual(self.fault('oom')['reason'],'oom')
        self.assertEqual(self.controller.cycle(launch=False)['items'][0]['phase'],'queued')
        config=self.controller.problems.memory('triage',self.request['proposal_id'],self.job['runner_config'])
        self.assertEqual(config['memory'],'4g')

    def test_memory_cap_wait_exits_when_operator_adds_larger_tier(self):
        self.fault('oom')
        self.assertEqual(self.controller.problems.recheck('triage',self.request['proposal_id'],{'memory_tiers':['2g']})['pending'],'oom')
        self.assertTrue(self.controller.problems.recheck('triage',self.request['proposal_id'],{'memory_tiers':['2g','8g']})['resolved'])
        config=self.controller.problems.memory('triage',self.request['proposal_id'],{'memory':'2g','memory_tiers':['2g','8g']})
        self.assertEqual(config['memory'],'8g')


class DiscussionExecutionWaitTests(unittest.TestCase):
    setUp=discussion_fixtures.DiscussionTests.setUp
    send=discussion_fixtures.DiscussionTests.send
    start=discussion_fixtures.DiscussionTests.start

    def fault(self,status='harness_error'):
        job=self.start()
        self.service.commands.progress(job['id'],container_id='container-id')
        job=self.service.commands.job(job['id'])
        self.runner.inspect.return_value={'status':'exited','running':False}
        self.runner.capture.return_value=failed_capture(job,status)
        self.service.drive(job,self.github)
        return job

    def test_private_harness_fault_does_not_become_reply_and_resumes_after_repair(self):
        job=self.fault()
        thread=self.service.read()['1:2']
        self.assertEqual(thread['status'],'queued');self.assertIn('Forge needs',thread['reason'])
        self.assertTrue(self.service.commands.job(job['id'])['cleanup'])
        self.assertEqual(self.service.problems.recheck('discussion','1:2')['pending'],'forge_problem')
        repaired(self.store,self.now)
        self.assertTrue(self.service.problems.recheck('discussion','1:2')['resolved'])
        self.assertFalse(any(message['role']=='assistant' for message in self.service.read()['1:2']['messages']))
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_provider_wait_has_durable_due_time_and_no_finding_timeout(self):
        self.fault('provider_error')
        self.now+=29
        self.assertEqual(self.service.problems.recheck('discussion','1:2')['pending'],'provider_error')
        self.now+=1
        self.assertTrue(self.service.problems.recheck('discussion','1:2')['resolved'])
        self.assertEqual(self.service.commands.thread('1:2')['requested_at'],self.now)

    def test_cancelled_question_does_not_resume_from_old_image_repair(self):
        self.fault();self.service.cancel('1:2');repaired(self.store,self.now)
        self.assertIsNone(self.service.problems.recheck('discussion','1:2'))
        self.assertEqual(self.service.read()['1:2']['status'],'cancelled')

    def test_cancellation_does_not_clear_account_cooldown_early(self):
        job=self.fault('provider_error');self.service.cancel('1:2')
        self.assertIsNone(self.service.problems.recheck('discussion','1:2'))
        with self.store.read_snapshot() as db:
            config=job['runner_config']
            self.assertFalse(provider_available(db,config['provider'],job.get('provider_id'),config,self.now))
