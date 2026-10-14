import json
import sqlite3
import unittest
import uuid
from dataclasses import replace

from forge.factory.wait_controller import WaitController
from forge.store.models import Command, TransitionError
from forge.store.worker_claims import WorkerClaims, revisions_in
from tests import test_store_runtime_requirements as helpers
from tests import test_store_operator as operator_helpers
from tests import test_store_result_controller as result_helpers


class ClaimTests(unittest.TestCase):
    snapshot = helpers.RuntimeRequirementsTests.snapshot
    command = helpers.RuntimeRequirementsTests.command
    capture = helpers.RuntimeRequirementsTests.capture
    counts = helpers.RuntimeRequirementsTests.counts

    def setUp(self):
        helpers.RuntimeRequirementsTests.setUp(self)
        self.claims = WorkerClaims(self.store, clock=lambda:self.now)

    def setting(self,domain,value):
        with self.store.write() as db:
            revision=db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM configuration WHERE domain=?',(domain,)).fetchone()[0]
            db.execute('UPDATE configuration SET active=0 WHERE domain=?',(domain,))
            db.execute("INSERT INTO configuration(domain,subject,revision,active,settings_json,created_at) VALUES(?,'instance',?,1,?,?)",(domain,revision,json.dumps(value),self.now))

    def claim(self,reason='github_permission',details=None):
        launched=self.dispatch.step('u')
        self.assertTrue(launched.get('reserved'),launched)
        job=self.capture(launched['job_id'])
        args=dict(execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],
            evidence={'reason':'worker_claim'},details=details or {'reason':reason,'required_services':[]})
        command=self.command()
        return job,command,args,self.claims.defer_claim(command,job['id'],**args)

    def test_timer_releases_only_claim_and_repeated_attempt_charges_once_across_replay(self):
        job,command,args,first=self.claim()
        gate=first['gate_id']
        self.now=399
        self.assertEqual(WaitController(self.store,clock=lambda:self.now).step(gate)['pending'],'worker_claim_retest')
        self.now=400
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(gate)['resolved'])
        self.assertEqual(self.counts(),(0,0))
        job,command,args,repeated=self.claim()
        self.assertTrue(repeated['charged'])
        self.assertTrue(repeated['recovery_required'])
        self.assertEqual(self.claims.defer_claim(command,job['id'],**args),repeated)
        with self.assertRaises((TransitionError, sqlite3.IntegrityError)):
            self.claims.defer_claim(replace(command,request_id=str(uuid.uuid4()),expected_version=repeated['version']),job['id'],**args)
        self.assertEqual(self.counts(),(1,1))
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='recovery_pending' AND resolved_at IS NULL").fetchone()[0],1)

    def test_deploy_change_retests_claim_without_releasing_pause(self):
        self.setting('host.json',{'source':'a'*40,'image':'sha256:'+'1'*64,'updated_at':90})
        _,_,_,first=self.claim()
        self.commands.pause(self.command())
        self.setting('host.json',{'source':'b'*40,'image':'sha256:'+'2'*64,'updated_at':100})
        self.now=101
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['resolved'])
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL").fetchone())
        self.assertEqual(self.counts(),(0,0))
        self.commands.resume(self.command())
        self.assertFalse(self.claim()[3]['charged'])

    def test_observation_timestamps_and_unrelated_policy_do_not_retest(self):
        self.setting('host.json',{'source':'a'*40,'image':'sha256:'+'1'*64,'updated_at':90})
        self.setting('factory.json',{'repositories':[{'id':1,'name':'owner/repo'}]})
        _,_,_,first=self.claim()
        self.setting('host.json',{'source':'a'*40,'image':'sha256:'+'1'*64,'updated_at':100})
        self.setting('factory.json',{'repositories':[{'id':1,'name':'owner/repo'},{'id':2,'name':'elsewhere/repo'}]})
        self.assertEqual(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['pending'],'worker_claim_retest')

    def test_relevant_repository_settings_release_github_permission(self):
        self.setting('factory.json',{'repositories':[{'id':1,'name':'owner/repo','merge_policy':'human'}]})
        _,_,_,first=self.claim()
        self.setting('factory.json',{'repositories':[{'id':1,'name':'owner/repo','merge_policy':'auto'}]})
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['resolved'])

    def test_environment_selection_change_retests_but_unchanged_observation_does_not(self):
        _,_,_,first=self.claim('services')
        self.controller.observe('u')
        self.assertEqual(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['pending'],'worker_claim_retest')
        self.setting('runtime.json',{'environment_tiers':{'full':{'image':'sha256:'+'3'*64}}})
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['resolved'])

    def test_only_explicit_provider_login_generation_change_retests(self):
        self.setting('provider-logins.json',{'generations':{'FORGE_CODEX_AUTH_FILE':'first'}})
        job,_,_,first=self.claim()
        with self.store.read_snapshot() as db:
            runner=json.loads(db.execute('SELECT runner_config_json FROM job_launches WHERE job_id=?',(job['id'],)).fetchone()[0])
        # Fixture provider reference is recorded by the immutable launch.
        references=runner.get('provider_environment',[])+([runner['provider_auth_file_env']] if runner.get('provider_auth_file_env') else [])
        self.assertTrue(references)
        self.setting('provider-logins.json',{'generations':{references[0]:'second'}})
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['resolved'])

    def test_configured_backoff_and_distinct_claim_cap_survive_controller_restart(self):
        self.setting('runtime.json',{'reliability':{'claim_retest_seconds':[30,60]}})
        _,_,_,first=self.claim('services',{'reason':'services','required_services':['postgres']})
        self.now=129
        self.assertEqual(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['retry_at'],130)
        self.now=130
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['resolved'])
        _,_,_,second=self.claim('services',{'reason':'services','required_services':['redis']})
        self.now=189
        self.assertEqual(WaitController(self.store,clock=lambda:self.now).step(second['gate_id'])['retry_at'],190)
        self.now=190
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(second['gate_id'])['resolved'])
        self.claims=WorkerClaims(self.store,clock=lambda:self.now)
        self.assertTrue(self.claim('services',{'reason':'services','required_services':['mongodb']})[3]['charged'])
        self.assertEqual(self.counts(),(1,1))

    def test_worker_memory_prose_is_not_trusted_oom_evidence(self):
        launched=self.dispatch.step('u')
        job=self.capture(launched['job_id'])
        report={'outcome':'blocked_environment','blocker':'resources','summary':'Out of memory, OOM killed'}
        self.assertIsNone(self.controller.classify(job,{'execution':{'oom_killed':False}},report))
        self.assertEqual(self.counts(),(0,0))

    def test_nonclaim_environment_and_oom_wait_remain_owned(self):
        for details in ({'source':'runtime_selection','reason':'toolchain'},{'reason':'resources','oom_killed':True}):
            gate=self.commands.open_gate(self.command(),'environment',details=details)['gate_id']
            with self.assertRaises(TransitionError):WaitController(self.store,clock=lambda:1000).step(gate)
            with self.store.write() as db:db.execute("UPDATE unit_gates SET resolved_at=100,resolution='superseded' WHERE id=?",(gate,))

    def test_legacy_worker_wait_is_normalized_and_retested(self):
        launched=self.dispatch.step('u');job=self.capture(launched['job_id'])
        first=self.commands.defer_job(self.command(),job['id'],execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],
            evidence={'reason':'worker_claim'},code='environment',details={'reason':'github_permission','required_services':[]})
        self.assertTrue(WaitController(self.store,clock=lambda:self.now).step(first['gate_id'])['resolved'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM worker_claim_obligations').fetchone()[0],1)


class OperatorClaimTests(unittest.TestCase):
    setUp=operator_helpers.OperatorTests.setUp
    command=operator_helpers.OperatorTests.command
    view=operator_helpers.OperatorTests.view
    reserve=operator_helpers.OperatorTests.reserve
    revision=operator_helpers.OperatorTests.revision
    request=operator_helpers.OperatorTests.request

    def test_actual_recheck_releases_only_claim_and_preserves_independent_hold(self):
        job_id=self.reserve()
        result_helpers.ResultControllerTests.capture(self,job_id,None)
        from forge.store.executions import ExecutionQueries
        job=ExecutionQueries(self.store).job(job_id)
        claim=WorkerClaims(self.store,clock=lambda:self.now).defer_claim(self.command(),job_id,
            execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],
            evidence={'reason':'worker_claim'},details={'reason':'github_permission','required_services':[]})
        self.life.open_gate(self.command(),'dependency',details={'source':'runtime_dependencies','blocked':['other/repo#1']})
        request=self.request('recheck')
        self.assertTrue(self.actions.step(request)['applied'])
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(claim['gate_id'],)).fetchone()[0])
            self.assertEqual([row[0] for row in db.execute('SELECT code FROM unit_gates WHERE resolved_at IS NULL')],['dependency'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)


class GroupClaimTests(unittest.TestCase):
    from tests.test_delivery_execution import DeliveryExecutionTests as _helpers
    setUp=_helpers.setUp
    command=_helpers.command
    compose=_helpers.compose
    manifest=_helpers.manifest
    clean=_helpers.clean

    def test_recheck_one_member_keeps_other_member_claim(self):
        from forge.factory.store_operator import OperatorActions
        from forge.factory.store_views import action_revision
        from forge.factory.config import validate
        from tests.test_member_operator import Remote
        self.compose()
        manifest=self.manifest()
        job=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='parent-fence',input_manifest=manifest)
        result=WorkerClaims(self.store,clock=lambda:100).defer_claim(self.command(),job['job_id'],
            execution_fence='parent-fence',input_manifest=manifest,evidence={'reason':'worker_claim'},
            details={'reason':'github_permission','required_services':[]})
        self.clean(job,'parent-fence')
        self.assertEqual(len(result['gate_ids']),2)
        actions=OperatorActions(self.store,Remote(2),actor_id='github:42',config=validate({}),clock=lambda:100)
        with self.store.read_snapshot() as db:revision=action_revision(db,'issue-0')
        request=actions.request('issue-0','recheck',revision,'Test only this member again')
        self.assertTrue(actions.step(request)['applied'])
        with self.store.read_snapshot() as db:
            remaining=[dict(row) for row in db.execute('SELECT * FROM unit_gates WHERE resolved_at IS NULL')]
            self.assertEqual({row['code'] for row in remaining},{'environment'})
            self.assertEqual([row['member_id'] for row in remaining if row['code']=='environment'],[self.members['issue-1']])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM worker_claim_obligations WHERE released_at IS NULL').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def test_simultaneous_claims_aggregate_exact_receipts_and_wait_for_all_obligations(self):
        from forge.store.executions import Launch
        from tests.test_delivery_execution import task_plan
        self.compose()
        manifest=self.manifest()
        owner=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='parent-fence',input_manifest=manifest)
        tasks=[{'key':str(i),'executor':'child','member_ids':[self.members['issue-0']],'scope':'Separate contribution '+str(i),'depends_on':[],'internalizes':[]} for i in range(2)]
        tasks.append({'key':'2','executor':'child','member_ids':[self.members['issue-1']],'scope':'Independent contribution','depends_on':[],'internalizes':[]})
        plan=self.delivery.save_plan(self.command(),owner['job_id'],execution_fence='parent-fence',input_manifest=manifest,plan=task_plan(tasks),evidence={'verified':True})
        self.clean(owner,'parent-fence')
        children=[]
        for i in range(2):
            pin=self.query.task_manifest(plan['plan_id'],plan['task_ids'][str(i)])
            child=self.delivery.reserve_task(self.command(),plan['plan_id'],plan['task_ids'][str(i)],slot='slot-'+str(i),execution_fence='fence-'+str(i),
                input_manifest=pin,worktree_key='work-'+str(i),launch=Launch('branch','builder[bot]','codex',1000,{},{}))
            children.append((child,pin))
        gates=[]
        for i,(child,pin) in enumerate(children):
            result=WorkerClaims(self.store,clock=lambda:100+i*10).defer_claim(self.command(),child['job_id'],
                execution_fence='fence-'+str(i),input_manifest=pin,evidence={'reason':'worker_claim'},details={'reason':'services','required_services':['postgres']})
            self.assertFalse(result['charged'])
            gates.append(result['gate_id'])
            self.clean(child,'fence-'+str(i))
        self.assertEqual(gates[0],gates[1])
        self.assertFalse(WaitController(self.store,clock=lambda:400).step(gates[0])['resolved'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM worker_claim_obligations WHERE released_at IS NULL').fetchone()[0],1)
        self.assertTrue(WaitController(self.store,clock=lambda:410).step(gates[0])['resolved'])

    def owner_claim(self,legacy=False):
        self.compose()
        manifest=self.manifest()
        job=self.delivery.reserve_builder(self.command(),stage='planning',slot='owner',execution_fence='parent-fence',input_manifest=manifest)
        args=dict(execution_fence='parent-fence',input_manifest=manifest,evidence={'reason':'worker_claim'},details={'reason':'github_permission','required_services':[]})
        if legacy: self.life.defer_job(self.command(),job['job_id'],code='environment',**args)
        else: WorkerClaims(self.store,clock=lambda:100).defer_claim(self.command(),job['job_id'],**args)
        self.clean(job,'parent-fence')
        self.life.pause(self.command())
        return job

    def test_dissolve_retains_claim_obligations_and_pause_for_each_successor(self):
        job=self.owner_claim()
        released=self.life.dissolve(self.command(),evidence={'approved':True})['released']
        for member in released:
            with self.store.read_snapshot() as db:
                gates=list(db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL',(member['unit_id'],)))
                self.assertEqual({gate['code'] for gate in gates},{'operator_pause','environment'})
                obligation=db.execute('SELECT * FROM worker_claim_obligations WHERE member_id=?',(member['member_id'],)).fetchone()
                self.assertEqual(obligation['job_id'],job['job_id'])
                self.assertIsNone(obligation['released_at'])
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def test_drop_normalizes_legacy_claim_and_keeps_both_remaining_obligations(self):
        job=self.owner_claim(legacy=True)
        released=self.delivery.drop_member(self.command(),self.members['issue-0'],membership_generation=1,
            dependent_decisions={},evidence={'approved':True})['released']
        with self.store.read_snapshot() as db:
            gates=list(db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL',(released['unit_id'],)))
            self.assertEqual({gate['code'] for gate in gates},{'operator_pause','environment'})
            obligation=db.execute('SELECT * FROM worker_claim_obligations WHERE member_id=?',(released['member_id'],)).fetchone()
            self.assertEqual(obligation['job_id'],job['job_id'])
            remaining=db.execute("SELECT * FROM unit_gates WHERE unit_id='batch' AND code='environment' AND resolved_at IS NULL").fetchall()
            self.assertEqual([row['member_id'] for row in remaining],[self.members['issue-1']])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)
