"""Steward moves through real reservations, captured decisions and dispatch."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from forge.factory.config import validate
from forge.factory.dispatch_controller import DispatchController, PreparedDispatch
from forge.store.executions import ExecutionQueries, Launch
from forge.store.models import TransitionError, VersionConflict
from forge.store.steward import dossier_in, selection_in
from forge.store.steward_execution import StewardExecutions, StewardMoves, execution_gates_in
from forge.store.transitions import _unit
from tests import test_steward_core as core_fixture
from tests.test_store_sqlite_supervisor import GitHub, Runner


class StewardExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = core_fixture.StewardCoreTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.store = self.f.store
        self.exec = StewardExecutions(self.store, clock=lambda:self.f.now)
        self.moves = StewardMoves(self.store, clock=lambda:self.f.now)
        self.config = validate({'enabled':True,'image':'sha256:'+'1'*64})
        self.observations = {'base_oid':'a'*40,'observed_at':self.f.now,'issues':[
            {'repository_id':1,'issue_number':2,'state':'open','revision':'target'}]}

    def reserve(self, mode='stall', trigger=None, retry_of=None):
        result=self.exec.reserve(self.f.command(),mode=mode,trigger=trigger or {'gate_id':self.f.gate},
            observations=self.observations,config=self.config,slot='steward-1',execution_fence='fence-'+str(self.f.serial),
            launch=Launch('forge/test','controller[bot]','codex',self.f.now+300,self.config,{}),retry_of=retry_of)
        return ExecutionQueries(self.store).job(result['job_id'])

    def accept(self, job, *, brief=True, action='retry', parameters=None):
        with self.store.read_snapshot() as db: dossier=dossier_in(db,job['id'])
        output=self.f.output(dossier,brief=brief,action=action,parameters=parameters)
        self.f.capture(job,output)
        answer=self.f.life.accept(self.f.command(),job_id=job['id'],execution_fence=job['execution_fence'],
                                 input_manifest=job['input_manifest'],decision=output)
        return answer['decision_id'],output

    def apply(self, identifier):
        return self.f.life.apply(self.f.command(),decision_id=identifier,
            apply_move=lambda db,cmd,u,c,b:self.moves.apply_move(db,cmd,u,c,b,decision_id=identifier,
                                                               observations=self.observations,config=self.config))

    def test_applied_timeout_changes_next_real_dispatch_without_new_budget_epoch(self):
        job=self.reserve(); identifier,output=self.accept(job)
        self.exec.decision_wait(self.f.command(),identifier)
        self.f.select(identifier,output)
        self.apply(identifier)
        github=GitHub();runner=Runner(github)
        driver=DispatchController(self.store,runner,config=self.config,
            prepare=lambda snapshot,role:PreparedDispatch('a'*40,'build[bot]',deepcopy(self.config),None,{}),
            admission=lambda snapshot:True,clock=lambda:self.f.now)
        result=driver.step('u')
        self.assertTrue(result['reserved'],result)
        launched=ExecutionQueries(self.store).job(result['job_id'])
        self.assertEqual(launched['deadline'],self.f.now+3000)
        self.assertEqual(launched['history']['retry_plan']['decision_id'],identifier)
        self.assertEqual(self.f.view()['charged_failures'],0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT budget_generation FROM units').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM steward_applications').fetchone()[0],1)

    def test_brief_reservation_owns_original_stall_and_choice_wait_only(self):
        identifier,output=self.accept(self.reserve(),brief=False)
        self.exec.decision_wait(self.f.command(),identifier); self.f.select(identifier,output)
        from forge.steward_contract import digest
        trigger={'decision_id':identifier,'choice_id':'choice-1','choice_digest':digest(output['choices'][0])}
        brief=self.reserve('brief',trigger)
        self.assertTrue(brief['launch_allowed'])
        self.accept(brief)
        self.apply(identifier)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(DISTINCT cycle) FROM steward_turns').fetchone()[0],2)
            self.assertIsNotNone(selection_in(db,identifier)['brief_decision_id'])

    def test_unchanged_hold_blocks_physical_reservation_and_new_hold_blocks_application(self):
        self.f.life.pause(self.f.command())
        with self.assertRaisesRegex(TransitionError,'independent'):
            self.reserve()
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],0)

    def test_changed_membership_is_diagnostic_only_never_implementation_permission(self):
        # The same legacy recovery exception permits reading the failed scope,
        # but a move must continue to respect the independently owned gate.
        self.f.open_gate('membership_decision',{'reason':'requirements_changed'})
        with self.store.read_snapshot() as db:
            dossier=self.f.dossier()
            dossier['mode']='recovery';dossier['trigger']={'failed_job_id':'original'}
            self.assertEqual(execution_gates_in(db,_unit(db,'u'),dossier,diagnostic=True)[0]['id'],self.f.gate)
            self.assertTrue(any(g['code']=='membership_decision' for g in execution_gates_in(db,_unit(db,'u'),dossier,diagnostic=False)))

    def test_stale_plan_cannot_change_dispatch_after_fencing_generation_changes(self):
        identifier,output=self.accept(self.reserve());self.f.select(identifier,output);self.apply(identifier)
        with self.store.write() as db:db.execute('UPDATE units SET fencing_generation=fencing_generation+1 WHERE id=\'u\'')
        from forge.store.steward_execution import retry_plan_in
        with self.store.read_snapshot() as db:self.assertIsNone(retry_plan_in(db,_unit(db,'u'),'build'))

    def test_wait_on_uses_existing_dependency_authority_and_preserves_budget(self):
        self.f.life.admit_issue(request_id='other',actor_id='operator',unit_id='v',issue_id='other',
            repository_id=1,repository_name='owner/repo',issue_number=2,title='Prerequisite',body='',requirements='target',observed_at=90)
        identifier,output=self.accept(self.reserve(),brief=False,action='wait_on',parameters={'repository_id':1,'issue_number':2})
        self.f.select(identifier,output);self.apply(identifier)
        with self.store.read_snapshot() as db:
            from forge.store.repository_requirements import orders_in
            member=db.execute('SELECT * FROM unit_issues WHERE unit_id=\'u\'').fetchone()
            self.assertEqual(orders_in(db,member['issue_id'],member['requirements_revision_id']),['owner/repo#2'])
            self.assertEqual(db.execute('SELECT count(*) FROM unit_gates WHERE unit_id=\'u\' AND code=\'dependency\' AND resolved_at IS NULL').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_unsettled_publication_prevents_application_without_consuming_choice(self):
        identifier,output=self.accept(self.reserve());self.f.select(identifier,output)
        with self.store.write() as db:
            db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,payload_json,created_at,updated_at) VALUES('effect','effect','u','github_merge','uncertain','{}',100,100)")
        with self.assertRaisesRegex(TransitionError,'unsettled'):
            self.apply(identifier)
        with self.store.read_snapshot() as db:self.assertIsNone(selection_in(db,identifier)['application'])

    def failed_build(self):
        with self.store.read_snapshot() as db:
            gate=db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(self.f.gate,)).fetchone()
        if gate and gate[0] is None:
            cmd=self.f.command()
            self.f.life._apply(cmd,'fixture_resolve_stall',{},lambda db,u:self.f.life._resolve(db,cmd,self.f.gate,{'observed':True}) or {})
        with self.store.read_snapshot() as db:
            revision=db.execute("SELECT requirements_revision_id FROM unit_issues WHERE id=?",(self.f.member,)).fetchone()[0]
        manifest={'requirements_revision_id':revision,'base_oid':'a'*40,'policy_hash':'policy'}
        result=self.exec.reserve_job(self.f.command(),'build',slot='build-1',execution_fence='build-'+str(self.f.serial),
            member_id=self.f.member,input_manifest=manifest,
            launch=Launch('forge/test','build[bot]','codex',self.f.now+300,self.config,{}))
        job=ExecutionQueries(self.store).job(result['job_id']);self.f.capture(job,None)
        self.exec.record_failure(self.f.command(),job['id'],execution_fence=job['execution_fence'],input_manifest=manifest,
                                 evidence={'reason':'acceptance_test_failed'})
        return job

    def test_recovery_choice_releases_only_original_wait_and_keeps_failure_charge(self):
        failed=self.failed_build()
        identifier,output=self.accept(self.reserve('recovery',{'failed_job_id':failed['id']}))
        self.exec.decision_wait(self.f.command(),identifier);self.f.select(identifier,output);self.apply(identifier)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT budget_generation FROM units').fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT investigation FROM attempt_failures').fetchone()[0],'complete')
            self.assertEqual(db.execute('SELECT state FROM unit_issues WHERE id=?',(self.f.member,)).fetchone()[0],'pending')
        self.assertEqual(self.f.view()['charged_failures'],1)
        self.assertIsNone(self.f.view()['reason_code'])

    def test_rebuild_invalidates_old_source_without_refunding_failure(self):
        failed=self.failed_build()
        with self.store.read_snapshot() as db:before=db.execute('SELECT fencing_generation FROM units').fetchone()[0]
        identifier,output=self.accept(self.reserve('recovery',{'failed_job_id':failed['id']}),
                                      action='rebuild',parameters={'base_oid':'a'*40})
        self.f.select(identifier,output);self.apply(identifier)
        with self.store.read_snapshot() as db:
            unit=_unit(db,'u')
            self.assertGreater(unit['fencing_generation'],before)
            from forge.store.steward_execution import retry_plan_in
            self.assertEqual(retry_plan_in(db,unit,'build')['action'],'rebuild')
        self.assertEqual(self.f.view()['charged_failures'],1)

    def test_third_failure_stays_exhausted_after_diagnostic_selection(self):
        for index in range(3):
            failed=self.failed_build()
            identifier,output=self.accept(self.reserve('recovery',{'failed_job_id':failed['id']}),parameters={'timeout_seconds':1000+500*index})
            self.f.select(identifier,output)
            if index < 2:
                self.apply(identifier)
            else:
                with self.assertRaisesRegex(TransitionError,'exhausted'):
                    self.apply(identifier)
        self.assertEqual(self.f.view()['charged_failures'],3)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT budget_generation FROM units').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM steward_applications').fetchone()[0],2)


    def test_provider_adjustment_selects_named_ready_entry_without_changing_configured_pool(self):
        entries=[dict(id='first',provider='codex',model='model-one',provider_environment=['OPENAI_API_KEY'],provider_auth_file_env=None),
                 dict(id='second',provider='codex',model='model-two',provider_environment=['OPENAI_API_KEY'],provider_auth_file_env=None)]
        self.config=validate({**self.config,'provider_pool':entries,'provider_ready':['first','second']})
        identifier,output=self.accept(self.reserve(),parameters={'provider_id':'second'})
        self.f.select(identifier,output);self.apply(identifier)
        github=GitHub();runner=Runner(github)
        driver=DispatchController(self.store,runner,config=self.config,
            prepare=lambda snapshot,role:PreparedDispatch('a'*40,'build[bot]',deepcopy(self.config),None,{}),
            admission=lambda snapshot:True,clock=lambda:self.f.now)
        result=driver.step('u');self.assertTrue(result['reserved'],result)
        job=ExecutionQueries(self.store).job(result['job_id'])
        self.assertEqual(job['provider_id'],'second')
        self.assertEqual(job['runner_config']['provider_pool'],entries)



    def test_environment_adjustment_keeps_operator_choice_and_required_services(self):
        self.config=validate({**self.config,'environment_tiers':{
            'standard':{'image':self.config['image'],'memory':'4g','cpus':2},
            'full':{'image':'sha256:'+'2'*64,'memory':'8g','cpus':2}}})
        identifier,output=self.accept(self.reserve(),parameters={'environment_id':'standard'})
        self.f.select(identifier,output);self.apply(identifier)
        with self.store.write() as db:
            db.execute('INSERT INTO issue_environment_observations(issue_id,choice_json,override_json) VALUES(?,?,?)',
                       ('i',json.dumps({'tier':'full','automatic':False,'services':['postgres']}),
                        json.dumps({'mode':'full','services':['postgres']})))
        github=GitHub();runner=Runner(github)
        from forge.factory.environments import worker_config
        config=worker_config(self.config,{'environment_choice':{'tier':'full','services':['postgres']}})
        driver=DispatchController(self.store,runner,config=self.config,
            prepare=lambda snapshot,role:PreparedDispatch('a'*40,'build[bot]',deepcopy(config),None,{}),
            admission=lambda snapshot:True,clock=lambda:self.f.now)
        result=driver.step('u')
        self.assertEqual(result['reason'],'environment_conflict')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE role='build'").fetchone()[0],0)
            choice=json.loads(db.execute('SELECT choice_json FROM issue_environment_observations').fetchone()[0])
            self.assertEqual(choice['services'],['postgres'])



class StewardControllerTests(unittest.TestCase):
    setUp=StewardExecutionTests.setUp
    reserve=StewardExecutionTests.reserve
    accept=StewardExecutionTests.accept
    apply=StewardExecutionTests.apply
    def driver(self):
        from forge.factory.steward_controller import StewardController
        github=GitHub()
        github.issue=lambda item:(dict(github.remote), 'original acceptance')
        github.call=lambda *args:{'commit':{'sha':'a'*40}}
        return StewardController(self.store,Runner(github),github,config=self.config,
                                 admission=lambda scope:True,clock=lambda:self.f.now)

    def completed(self, job, *, brief=True, kind='ok', parameters=None):
        with self.store.read_snapshot() as db: dossier=dossier_in(db,job['id'])
        output=self.f.output(dossier,brief=brief,parameters=parameters) if kind=='ok' else None
        self.exec.progress(self.f.command(),job['id'],execution_fence=job['execution_fence'],phase='running',container_id='container')
        execution={'id':'container','image_id':self.config['image'],'exit_protocol':'1','running':False,
                   'exit_code':0 if kind=='ok' else 1,'oom_killed':False}
        envelope={'version':1,'attempt':job['id'],'harness_status':kind,'exit_code':execution['exit_code']}
        if kind=='provider_error':envelope['provider_error']={'code':'rate_limit'}
        if kind=='harness_error':
            from forge.harnesses.execution import fault
            envelope['harness_error']=fault('ImportError',phase='before_agent')
        self.exec.collect(self.f.command(),job['id'],execution_fence=job['execution_fence'],
                          capture={'result':output,'logs':'','execution':execution,'execution_envelope':envelope})
        self.exec.verify_cleanup(self.f.command(),job['id'],execution_fence=job['execution_fence'],
                                 evidence={'process_stopped':True,'resources_released':True})
        return output

    def test_controller_schedules_exact_stall_and_auto_applies_without_second_worker(self):
        driver=self.driver();reservation=driver.schedule('u',mode='stall',trigger={'gate_id':self.f.gate})
        job=ExecutionQueries(self.store).job(reservation['job_id']);self.completed(job)
        result=driver.finish(job['id'])
        self.assertEqual(result['continuation']['status'],'applied')
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],1)
        self.assertEqual(driver.finish(job['id'])['outcome'],'success')

    def test_manual_repository_waits_for_operator_selection_without_repeated_writes(self):
        with self.store.write() as db:
            db.execute("INSERT INTO configuration(domain,subject,revision,active,settings_json,created_at) VALUES('steward','1',1,1,?,?)",(json.dumps({'autonomy':'manual'}),self.f.now))
        driver=self.driver();job=self.reserve();output=self.completed(job)
        answer=driver.finish(job['id']);identifier=answer['decision_id']
        self.assertEqual(answer['continuation']['pending'],'operator_choice')
        before=self.f.view()['version']
        self.assertEqual(driver.advance(identifier)['pending'],'operator_choice')
        self.assertEqual(self.f.view()['version'],before)
        self.f.select(identifier,output,selection='operator')
        self.assertEqual(driver.advance(identifier)['status'],'applied')

    def test_controller_missing_inline_brief_reserves_second_pinned_mode(self):
        driver=self.driver();job=self.reserve();self.completed(job,brief=False)
        result=driver.finish(job['id']);brief=ExecutionQueries(self.store).job(result['continuation']['job_id'])
        self.assertEqual(brief['steward']['dossier']['mode'],'brief')
        self.assertIsNotNone(brief['steward']['accepted_decision'])
        self.completed(brief);result=driver.finish(brief['id'])
        self.assertEqual(result['continuation']['status'],'applied')

    def test_trusted_provider_retry_waits_for_cooldown_and_reuses_one_cycle(self):
        from forge.factory.wait_controller import WaitController
        driver=self.driver();job=self.reserve();self.completed(job,kind='provider_error')
        result=driver.finish(job['id']);self.assertFalse(result['charged'])
        self.assertEqual(driver.schedule('u',mode='stall',trigger={'gate_id':self.f.gate})['pending'],'provider_unavailable')
        self.f.now+=31
        WaitController(self.store,clock=lambda:self.f.now).step(result['gate_ids'][0])
        reserved=driver.schedule('u',mode='stall',trigger={'gate_id':self.f.gate})
        self.assertTrue(reserved['reserved'],reserved)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(DISTINCT cycle) FROM steward_turns').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT max(physical_attempt) FROM steward_turns').fetchone()[0],2)

    def test_unrepaired_forge_fault_blocks_replacement_steward(self):
        driver=self.driver();job=self.reserve();self.completed(job,kind='harness_error')
        result=driver.finish(job['id']);self.assertFalse(result['charged'])
        with self.assertRaisesRegex(TransitionError,'independent'):
            driver.schedule('u',mode='stall',trigger={'gate_id':self.f.gate})
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],1)

    def test_stale_choice_does_not_starve_new_current_stall(self):
        driver=self.driver();identifier,output=self.accept(self.reserve())
        self.exec.decision_wait(self.f.command(),identifier)
        # Existing scheduling owner resolves its stall; the old card becomes stale.
        cmd=self.f.command();self.f.life._apply(cmd,'fixture_reconcile',{},lambda db,u:self.f.life._resolve(db,cmd,self.f.gate,{'observed':True}) or {})
        self.f.gate=self.f.open_gate('forge_problem',{'source':'scheduling','reason':'stalled','owner':'dispatch','new':True})
        result=driver._unit_step('u')
        self.assertTrue(result['reserved'],result)
        with self.store.read_snapshot() as db:
            self.assertIsNone(selection_in(db,identifier)['selection'])
            self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],2)
            self.assertFalse(db.execute("SELECT 1 FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL").fetchone())
        with self.assertRaises(VersionConflict):self.f.select(identifier,output)

    def test_advanced_base_invalidates_exact_choice_then_reinvestigates_same_stall(self):
        driver=self.driver();identifier,output=self.accept(self.reserve())
        driver.github.call=lambda *args:{'commit':{'sha':'b'*40}}
        answer=driver.advance(identifier)
        self.assertEqual(answer['pending'],'observations_changed')
        self.assertEqual(driver.advance(identifier)['pending'],'authority_changed')
        driver.github.call=lambda *args:{'commit':{'sha':'a'*40}}
        self.assertEqual(driver.advance(identifier)['pending'],'authority_changed')
        driver.github.call=lambda *args:{'commit':{'sha':'b'*40}}
        with self.store.read_snapshot() as db:
            old=selection_in(db,identifier)
            self.assertIsNotNone(old['selection']);self.assertIsNone(old['application'])
            receipt=db.execute('SELECT * FROM steward_observation_changes').fetchone()
            self.assertEqual(json.loads(receipt['previous_json']),{'base_oid':'a'*40})
            self.assertEqual(json.loads(receipt['observed_json']),{'base_oid':'b'*40})
            self.assertIsNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(self.f.gate,)).fetchone()[0])
        reserved=driver._unit_step('u');self.assertTrue(reserved['reserved'],reserved)
        job=ExecutionQueries(self.store).job(reserved['job_id'])
        self.assertEqual(job['input_manifest']['base_oid'],'b'*40)
        self.completed(job)
        applied=driver.finish(job['id'])
        self.assertEqual(applied['continuation']['status'],'applied')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM steward_decisions').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM steward_observation_changes').fetchone()[0],1)
            self.assertIsNone(selection_in(db,identifier)['application'])
        self.assertEqual(self.f.view()['charged_failures'],0)

    def test_new_read_time_and_unrelated_issue_change_do_not_invalidate_retry(self):
        driver=self.driver();identifier,_=self.accept(self.reserve())
        self.f.now+=1
        driver._observe=lambda *args,**kwargs:{'base_oid':'a'*40,'observed_at':self.f.now,'issues':[
            {'repository_id':1,'issue_number':2,'state':'closed','revision':'unrelated-change'}]}
        self.assertEqual(driver.advance(identifier)['status'],'applied')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM steward_observation_changes').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],1)

    def test_closed_selected_prerequisite_invalidates_wait_choice(self):
        driver=self.driver();identifier,_=self.accept(self.reserve(),action='wait_on',
            parameters={'repository_id':1,'issue_number':2})
        driver._observe=lambda *args,**kwargs:{'base_oid':'a'*40,'observed_at':self.f.now,'issues':[
            {'repository_id':1,'issue_number':2,'state':'closed','revision':'closed-revision'}]}
        self.assertEqual(driver.advance(identifier)['pending'],'observations_changed')
        with self.store.read_snapshot() as db:
            receipt=db.execute('SELECT observed_json FROM steward_observation_changes').fetchone()
            self.assertEqual(json.loads(receipt[0])['target']['state'],'closed')
            self.assertEqual(db.execute('SELECT count(*) FROM issue_dependencies').fetchone()[0],0)
        reserved=driver._unit_step('u')
        self.assertTrue(reserved['reserved'],reserved)
        job=ExecutionQueries(self.store).job(reserved['job_id'])
        self.assertEqual(job['steward']['dossier']['facts']['observations']['issues'][0]['state'],'closed')

    def test_changed_autonomy_refreshes_original_stall_and_keeps_old_decision(self):
        driver=self.driver();identifier,output=self.accept(self.reserve())
        self.exec.decision_wait(self.f.command(),identifier)
        with self.store.write() as db:
            db.execute("INSERT INTO configuration(domain,subject,revision,active,settings_json,created_at) VALUES('steward','1',1,1,?,?)",(json.dumps({'autonomy':'manual'}),self.f.now))
        answer=driver._unit_step('u')
        self.assertTrue(answer['reserved'],answer)
        job=ExecutionQueries(self.store).job(answer['job_id']);self.completed(job)
        accepted=driver.finish(job['id'])
        self.assertEqual(accepted['continuation']['pending'],'operator_choice')
        self.assertNotEqual(accepted['decision_id'],identifier)
        for _ in range(2):
            self.assertEqual(driver._unit_step('u')['pending'],'operator_choice')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM steward_decisions').fetchone()[0],2)
            self.assertEqual([tuple(r) for r in db.execute('SELECT cycle,physical_attempt FROM steward_turns ORDER BY cycle')],[(1,1),(2,1)])
            self.assertIsNone(selection_in(db,identifier)['selection'])
            self.assertIsNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(self.f.gate,)).fetchone()[0])
        with self.assertRaises(VersionConflict):self.f.select(identifier,output)
        self.assertEqual(self.f.view()['charged_failures'],0)

    def test_captured_pause_discard_resume_refreshes_same_trigger_within_cycle_limit(self):
        driver=self.driver();job=self.reserve()
        for cycle in range(1,4):
            self.completed(job)
            self.f.life.pause(self.f.command())
            self.assertEqual(driver.finish(job['id'])['outcome'],'stale_investigation')
            self.f.life.resume(self.f.command())
            answer=driver._unit_step('u')
            if cycle<3:
                self.assertTrue(answer['reserved'],answer)
                job=ExecutionQueries(self.store).job(answer['job_id'])
            else:
                self.assertEqual(answer['pending'],'cycle_limit')
        with self.store.read_snapshot() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT cycle,physical_attempt FROM steward_turns ORDER BY cycle')],[(1,1),(2,1),(3,1)])
            self.assertEqual(db.execute('SELECT count(*) FROM steward_decisions').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE outcome='stale_investigation'").fetchone()[0],3)
            self.assertIsNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(self.f.gate,)).fetchone()[0])
            wait=json.loads(db.execute("SELECT details_json FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL").fetchone()[0])
            self.assertEqual(wait['reason'],'cycle_limit')
        self.assertEqual(self.f.view()['charged_failures'],0)

    def test_semantically_stale_capture_is_consumed_without_application_or_charge(self):
        driver=self.driver();job=self.reserve();self.completed(job)
        self.f.life.pause(self.f.command())
        result=driver.finish(job['id'])
        self.assertEqual(result['outcome'],'stale_investigation')
        self.assertEqual(self.f.view()['charged_failures'],0)
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM steward_decisions').fetchone()[0],0)

    def test_unreusable_deferral_asks_operator_once_instead_of_rejecting_each_cycle(self):
        # Production 2026-10-15: a checkout failure wrote a worker-shaped
        # blocked report, the steward contract rejected it, and every cycle then
        # launched a retry that reserve_steward refused.
        driver=self.driver();job=self.reserve()
        self.exec.progress(self.f.command(),job['id'],execution_fence=job['execution_fence'],phase='running',container_id='container')
        execution={'id':'container','image_id':self.config['image'],'exit_protocol':'1','running':False,'exit_code':125,'oom_killed':False}
        blocked={'version':1,'attempt':job['id'],'role':'steward','outcome':'blocked_environment','blocker':'dependency_access',
                 'summary':'Forge could not prepare the disposable checkout before work started.','headline':'Stopped','plain':'Stopped'}
        self.exec.collect(self.f.command(),job['id'],execution_fence=job['execution_fence'],capture={'result':blocked,'logs':'',
            'execution':execution,'execution_envelope':{'version':1,'attempt':job['id'],'harness_status':'ok','exit_code':125}})
        self.exec.verify_cleanup(self.f.command(),job['id'],execution_fence=job['execution_fence'],
                                 evidence={'process_stopped':True,'resources_released':True})
        driver.finish(job['id'])
        with self.store.write() as db:
            # The Forge problem itself is released elsewhere; the stall remains.
            db.execute("UPDATE unit_gates SET resolved_at=?,resolution='superseded' WHERE code='forge_problem' AND resolved_at IS NULL",(self.f.now,))
        for _ in range(3):
            answer=driver.schedule('u',mode='stall',trigger={'gate_id':self.f.gate})
            self.assertEqual(answer['pending'],'retry_unauthorized')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE role='steward'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='decision_rejected'").fetchone()[0],0)
            waits=[json.loads(row[0]) for row in db.execute("SELECT details_json FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL")]
        self.assertEqual([(w['source'],w['reason']) for w in waits],[('steward_execution','retry_unauthorized')])

    def test_three_physical_provider_failures_leave_owned_wait_without_fourth_job(self):
        from forge.factory.wait_controller import WaitController
        driver=self.driver();job=self.reserve()
        for physical in range(3):
            self.completed(job,kind='provider_error');result=driver.finish(job['id'])
            self.f.now+=31
            WaitController(self.store,clock=lambda:self.f.now).step(result['gate_ids'][0])
            next_job=driver.schedule('u',mode='stall',trigger={'gate_id':self.f.gate})
            if physical<2:
                job=ExecutionQueries(self.store).job(next_job['job_id'])
            else:
                self.assertEqual(next_job['pending'],'physical_retry_limit')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT count(DISTINCT cycle) FROM steward_turns').fetchone()[0],1)
            wait=json.loads(db.execute("SELECT details_json FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL").fetchone()[0])
            self.assertEqual(wait['source'],'steward_execution')
            self.assertEqual(wait['reason'],'physical_retry_limit')
        self.assertEqual(self.f.view()['charged_failures'],0)


class StewardTaskScopeTests(unittest.TestCase):
    def test_adjustment_and_brief_apply_only_to_failed_task_and_freeze_at_reservation(self):
        from tests import test_task_pipeline as task_fixture
        from forge.store.steward import Steward
        from forge.steward_contract import digest
        fixture=task_fixture.TaskPipelineTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        owner=fixture.plan(lambda members:fixture.value([
            fixture.definition('failed',[members[0]['member_id']]),
            fixture.definition('independent',[members[1]['member_id']])]))
        f=fixture.f;affected,independent=[row['member_id'] for row in owner['members']]
        choices={affected:{'tier':'standard','automatic':True,'services':[]}}
        f.dispatch.prepare=lambda snapshot,role:PreparedDispatch(f.base,role+'[bot]',f.config,member_choices=choices)
        failed=f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertTrue(f.capture(failed['id'],outcome='unable')['charged'])
        execs=StewardExecutions(f.store,clock=lambda:f.now);core=Steward(f.store,clock=lambda:f.now)
        observations={'base_oid':f.base,'observed_at':f.now,'issues':[]}
        reserved=execs.reserve(f.command(),mode='recovery',trigger={'failed_job_id':failed['id']},
            observations=observations,config=f.config,slot='steward-1',execution_fence='steward',
            launch=Launch('forge/diagnostic','controller[bot]','codex',f.now+300,f.config,{}))
        job=f.jobs.job(reserved['job_id'])
        with f.store.read_snapshot() as db:dossier=dossier_in(db,job['id'])
        output=core_fixture.StewardCoreTests.output(None,dossier,parameters={'timeout_seconds':3000})
        execs.collect(f.command(),job['id'],execution_fence='steward',capture={'result':output,'logs':''})
        execs.verify_cleanup(f.command(),job['id'],execution_fence='steward',evidence={'process_stopped':True,'resources_released':True})
        identifier=core.accept(f.command(),job_id=job['id'],execution_fence='steward',input_manifest=job['input_manifest'],decision=output)['decision_id']
        core.select(f.command(),decision_id=identifier,choice_id='choice-1',choice_digest=digest(output['choices'][0]),selection='auto')
        moves=StewardMoves(f.store,clock=lambda:f.now)
        core.apply(f.command(),decision_id=identifier,apply_move=lambda db,cmd,u,c,b:moves.apply_move(db,cmd,u,c,b,decision_id=identifier,observations=observations,config=f.config))
        choices.clear();choices[independent]={'tier':'standard','automatic':True,'services':[]}
        other=f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertEqual(other['deadline'],f.now+f.config['timeout_seconds']['build'])
        self.assertIsNone(other['history']['retry_plan'])
        self.assertIsNone(f.jobs.job(failed['id'])['history']['retry_plan'])
        head,bundle=fixture.bundle('independent-success')
        self.assertTrue(f.capture(other['id'],output_commit=head,bundle=bundle)['accepted'])
        choices.clear();choices[affected]={'tier':'standard','automatic':True,'services':[]}
        dispatched=f.dispatch.step('batch');self.assertTrue(dispatched['reserved'],dispatched)
        retry=f.jobs.job(dispatched['job_id'])
        self.assertEqual(retry['task_id'],failed['task_id'])
        self.assertEqual(retry['deadline'],f.now+3000)
        self.assertEqual(retry['history']['retry_plan']['decision_id'],identifier)
        with f.store.write() as db:db.execute('UPDATE unit_context SET retry_plan_json=NULL WHERE unit_id=\'batch\'')
        self.assertEqual(f.jobs.job(retry['id'])['history']['retry_plan']['decision_id'],identifier)
