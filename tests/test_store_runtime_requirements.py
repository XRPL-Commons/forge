from copy import deepcopy
from dataclasses import replace
import json
import tempfile
import unittest
import uuid

from forge.factory.config import policy, validate
from forge.factory.dispatch_controller import DispatchController, GitHubPrepare
from forge.factory.requirements_controller import RuntimeRequirementsController
from forge.factory.result_controller import ResultController
from forge.factory.github import GitHubUnavailable
from forge.store.db import Store
from forge.store.executions import Executions, ExecutionQueries
from forge.store.models import Candidate, Command, TransitionError
from forge.store.runtime_requirements import RuntimeRequirements
from forge.store.transitions import applicable_gates, manifest_hash
from tests.test_store_dispatch_controller import GitHub as BaseGitHub, Runner, BASE, HEAD


class GitHub(BaseGitHub):
    def __init__(self):
        super().__init__()
        self.remote['updated_at'] = '2026-10-08T01:00:00Z'
        self.dependencies = {}
        self.labels_sent = 0
        self.lose_label_response = False

    def issue(self, item):
        if item['number'] == 1:
            return deepcopy(self.remote), self.requirements
        value = self.dependencies[item['number']]
        if isinstance(value, Exception):
            raise value
        return deepcopy(value), 'dependency-requirements'

    def environment_label(self, item, remote, label):
        self.labels_sent += 1
        self.remote['labels'] = [label]
        if self.lose_label_response:
            self.lose_label_response = False
            raise GitHubUnavailable('response lost')


class RuntimeRequirementsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now = 100.0
        self.config = validate({'enabled': True, 'image': 'sha256:' + '1' * 64,
                                'memory_tiers': ['4g','8g','16g'],
                                'environment_tiers': {'full': {'image': 'sha256:'+'2'*64,'memory':'4g','cpus':2}}})
        self.github, self.runner = GitHub(), Runner()
        self.commands = RuntimeRequirements(self.store, clock=lambda: self.now)
        admitted = self.commands.admit_issue(request_id='admit',actor_id='operator',unit_id='u',issue_id='i',
            repository_id=1,repository_name='owner/repo',issue_number=1,title='Fix issue',body='',
            requirements=self.github.requirements,observed_at=90)
        self.member,self.revision = admitted['member_id'],admitted['revision_id']
        self.controller = RuntimeRequirementsController(self.store,self.github,config=self.config,clock=lambda:self.now)
        self.dispatch = DispatchController(self.store,self.runner,config=self.config,
            prepare=GitHubPrepare(self.github,self.config,requirements=self.controller),admission=lambda _:True,clock=lambda:self.now)
        self.jobs = ExecutionQueries(self.store)

    def snapshot(self):
        return self.dispatch.snapshot('u')

    def command(self):
        return Command(str(uuid.uuid4()),'test','u',self.snapshot()['unit']['version'])

    def counts(self):
        with self.store.read_snapshot() as db:
            return tuple(db.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in ('budget_entries','attempt_failures'))

    def capture(self,job_id,*,oom=False):
        job=self.jobs.job(job_id)
        self.commands.collect(self.command(),job_id,execution_fence=job['execution_fence'],
            capture={'result':None,'logs':'failure details','execution':{'oom_killed':oom},'missing':[]})
        self.commands.verify_cleanup(self.command(),job_id,execution_fence=job['execution_fence'],
            evidence={'process_stopped':True,'resources_released':True})
        return self.jobs.job(job_id)

    def dependency(self,state='open',number=2):
        self.github.remote['body']='Depends on #'+str(number)
        self.github.dependencies[number]=dict(number=number,state=state,updated_at='2026-10-08T01:00:00Z')

    def test_choice_is_durable_and_dispatch_refreshes_version(self):
        launched=self.dispatch.step('u')
        self.assertTrue(launched['reserved'])
        self.assertEqual(self.jobs.job(launched['job_id'])['runner_config']['environment_tier'],'standard')
        self.assertEqual(self.github.labels_sent,1)
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT * FROM unit_environment').fetchone()
            self.assertEqual(row['requirements_revision_id'],self.revision)
            self.assertEqual(db.execute('SELECT status FROM external_effects').fetchone()[0],'confirmed')

    def test_open_then_closed_then_reopened_dependency_fences_work(self):
        self.dependency()
        self.assertFalse(self.controller.observe('u')['dependencies_clear'])
        self.assertEqual(self.dispatch.step('u')['reason'],'dependency')
        self.github.dependencies[2]['state']='closed'
        self.assertTrue(self.controller.observe('u')['dependencies_clear'])
        job_id=self.dispatch.step('u')['job_id']
        self.github.dependencies[2]['state']='open'
        self.controller.observe('u')
        self.assertFalse(self.jobs.job(job_id)['launch_allowed'])
        self.assertEqual(self.counts(),(0,0))

    def test_dependency_outage_cannot_clear_wait(self):
        self.dependency()
        self.controller.observe('u')
        self.github.dependencies[2]=GitHubUnavailable('offline')
        with self.assertRaises(GitHubUnavailable):
            self.controller.observe('u')
        self.assertEqual(self.snapshot()['reason'],'dependency')

    def test_pr_and_self_reference_never_prove_prerequisite(self):
        self.dependency('closed')
        self.github.dependencies[2]['pull_request']={ 'url':'test' }
        with self.assertRaises(GitHubUnavailable):
            self.controller.observe('u')
        self.github.remote['body']='Depends on #1'
        self.assertFalse(self.controller.observe('u')['dependencies_clear'])

    def test_obsolete_and_out_of_order_dependency_proof(self):
        self.commands.dependencies(self.command(),requirements_revision_id=self.revision,generation=2,facts=[])
        stale=self.commands.dependencies(self.command(),requirements_revision_id=self.revision,generation=1,
            facts=[dict(number=2,state='open',updated_at='2026-10-08T01:00:00Z')])
        self.assertTrue(stale['stale'])
        with self.assertRaises(TransitionError):
            self.commands.dependencies(self.command(),requirements_revision_id='old',generation=3,facts=[])

    def test_lost_label_response_recovers_without_repeating_mutation(self):
        self.github.lose_label_response=True
        with self.assertRaises(GitHubUnavailable):
            self.controller.observe('u')
        self.controller.observe('u')
        self.assertEqual(self.github.labels_sent,1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT status FROM external_effects').fetchone()[0],'confirmed')

    def test_operator_label_change_supersedes_old_intent(self):
        self.github.lose_label_response=True
        with self.assertRaises(GitHubUnavailable):
            self.controller.observe('u')
        self.github.remote['labels']=['env:full']
        choice=self.controller.observe('u')['choice']
        self.assertEqual(choice['tier'],'full')
        self.assertFalse(choice['automatic'])
        self.assertEqual(self.github.labels_sent,1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT status FROM external_effects').fetchone()[0],'superseded')

    def test_confirmed_oom_retries_next_tier_without_charge(self):
        first=self.dispatch.step('u')['job_id']
        self.capture(first,oom=True)
        results=ResultController(self.store,self.github,policy=lambda _:policy(self.config),requirements=self.controller,clock=lambda:self.now)
        result=results.step(first)
        self.assertEqual(result['retry'],'memory')
        second=self.dispatch.step('u')['job_id']
        self.assertEqual(self.jobs.job(second)['runner_config']['memory'],'8192m')
        self.assertEqual(self.jobs.job(first)['branch'],self.jobs.job(second)['branch'])
        self.assertEqual(self.counts(),(0,0))

    def test_cap_holds_and_generic_resources_do_not_escalate(self):
        first=self.dispatch.step('u')['job_id']
        job=self.capture(first)
        self.assertIsNone(self.controller.classify(job,{},dict(outcome='blocked_environment',blocker='resources',summary='disk is full')))
        self.controller.config['memory_tiers']=['4g']
        self.assertIsNone(self.controller.classify(job,{'execution':{'oom_killed':True}},None))
        self.assertEqual(self.counts(),(0,0))

    def test_service_retry_is_single_automatic_and_preserves_branch(self):
        first=self.dispatch.step('u')['job_id']
        job=self.capture(first)
        report=dict(outcome='blocked_environment',blocker='services',summary='database missing',required_services=['postgres'])
        self.assertEqual(self.controller.classify(job,{},report)['retry'],'services')
        second=self.dispatch.step('u')['job_id']
        next_job=self.jobs.job(second)
        self.assertEqual(next_job['runner_config']['services'],['postgres'])
        self.assertEqual(next_job['branch'],job['branch'])
        next_job=self.capture(second)
        report['required_services']=['redis']
        self.assertIsNone(self.controller.classify(next_job,{},report))
        self.assertEqual(self.counts(),(0,0))

    def test_manual_environment_never_auto_escalates_services(self):
        self.commands.set_override(self.command(),requirements_revision_id=self.revision,override={'mode':'standard','services':[]})
        job=self.capture(self.dispatch.step('u')['job_id'])
        self.assertIsNone(self.controller.classify(job,{},dict(outcome='blocked_environment',required_services=['postgres'])))

    def test_memory_drains_capacity_without_backfill(self):
        first=self.dispatch.step('u')['job_id']
        job=self.capture(first,oom=True)
        self.controller.classify(job,{'execution':{'oom_killed':True}},None)
        self.commands.admit_issue(request_id='second',actor_id='operator',unit_id='v',issue_id='other',
            repository_id=1,repository_name='owner/repo',issue_number=2,title='Other',body='',requirements='x',observed_at=90)
        self.runner.allowed=False
        result=self.dispatch.cycle()
        self.assertEqual(len(result['items']),1)
        self.assertEqual(result['items'][0]['unit_id'],'u')
        self.assertEqual(result['items'][0]['reason'],'capacity_unavailable')

    def test_success_learns_role_allocation_atomically(self):
        job=self.capture(self.dispatch.step('u')['job_id'])
        self.controller.classify(job,{'execution':{'oom_killed':True}},None)
        job=self.capture(self.dispatch.step('u')['job_id'])
        candidate=Candidate(9,job['branch'],HEAD,BASE,job['input_manifest']['policy_hash'],manifest_hash(job['requirements']),manifest_hash(job['input_manifest']))
        self.commands.finish_job(self.command(),job['id'],execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],
            evidence={'head_oid':HEAD,'base_oid':BASE,'pull_number':9},candidate=candidate)
        with self.store.read_snapshot() as db:
            learned=db.execute('SELECT * FROM memory_profiles').fetchone()
            self.assertEqual((learned['role'],learned['memory_bytes'],learned['source_job_id']),('build',8*1024**3,job['id']))
            self.assertIsNone(db.execute('SELECT memory_retry_json FROM unit_environment_observations').fetchone()[0])

    def test_persisted_dependency_proof_is_revision_bound_and_holds_remain(self):
        self.dependency('closed')
        self.commands.admit_issue(request_id='prerequisite',actor_id='operator',unit_id='v',issue_id='other',
            repository_id=1,repository_name='owner/repo',issue_number=2,title='Other',body='',requirements='x',observed_at=90)
        with self.store.write() as db:
            db.execute("INSERT INTO issue_dependencies VALUES('edge','i',?,'other','requirements','Depends on #2',90)",(self.revision,))
        self.assertEqual(self.snapshot()['reason'],'dependency')
        self.commands.pause(self.command())
        self.controller.observe('u')
        self.assertEqual(self.snapshot()['reason'],'operator_pause')
        with self.store.read_snapshot() as db:
            self.assertFalse(any(g['code']=='dependency' for g in applicable_gates(db,self.snapshot()['unit'],self.member)))
        self.assertEqual(self.counts(),(0,0))

    def test_unavailable_environment_has_durable_gate_and_recovers(self):
        self.github.remote['body']='Test postgres migrations'
        self.controller.config['environment_tiers']={}
        with self.assertRaises(ValueError):
            self.controller.observe('u')
        self.assertEqual(self.snapshot()['reason'],'environment')
        self.controller.config['environment_tiers']=self.config['environment_tiers']
        self.controller.observe('u')
        self.assertIsNone(self.snapshot()['reason'])

    def test_clear_declared_wait_does_not_override_explicit_body_dependency(self):
        self.dependency()
        self.commands.open_gate(self.command(),'dependency',member_id=self.member,
            details={'issues':[2],'requirements':self.github.requirements,'job_id':'worker'})
        self.commands.clear_declared_dependencies(self.command(),requirements_revision_id=self.revision,evidence={'reason':'operator correction'})
        self.controller.observe('u')
        self.assertEqual(self.snapshot()['reason'],'dependency')

    def test_complete_observation_rejects_pull_request_even_empty_marker(self):
        self.dependency('closed')
        self.github.dependencies[2]['pull_request']={}
        with self.assertRaises(GitHubUnavailable):
            self.controller.observe('u')
