"""Real SQL lifecycle tests for inactive steward decision authority."""
from copy import deepcopy
import json
import tempfile
import unittest
from unittest.mock import patch
from forge.store.codes import ARTIFACT_KINDS

from forge.steward_contract import decision, decision_schema, digest
from forge.store.db import Store
from forge.store.executions import Launch, insert_launch
from forge.store.models import Command, TransitionError, VersionConflict
from forge.store.steward import Steward, dossier_in, selection_in
from forge.store.steward_dossier import build_dossier_in
from forge.store.transitions import _id, _json, _unit, project_unit


class StewardCoreTests(unittest.TestCase):
    def setUp(self):
        # The integrator owns the Python role registry; SQL043 supplies the same kinds.
        registry=patch('forge.store.artifacts.ARTIFACT_KINDS',{**ARTIFACT_KINDS,'steward_dossier':'durable','steward_decision':'durable'})
        registry.start();self.addCleanup(registry.stop)
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.store = Store.create(temp.name)
        self.now, self.serial = 100., 0
        self.life = Steward(self.store, clock=lambda:self.now)
        admitted = self.life.admit_issue(request_id='admit',actor_id='operator',unit_id='u',issue_id='i',
            repository_id=1,repository_name='owner/repo',issue_number=1,title='Keep intent',body='Original body',
            requirements='original acceptance',observed_at=90)
        self.member = admitted['member_id']
        self.gate = self.open_gate('forge_problem',{'source':'scheduling','reason':'stalled','owner':'dispatch'})

    def view(self):
        with self.store.read_snapshot() as db:return project_unit(db,'u')

    def command(self):
        self.serial += 1
        return Command('c'+str(self.serial),'controller','u',self.view()['version'])

    def open_gate(self, code, details):
        cmd=self.command()
        return self.life._apply(cmd,'test_gate',{'code':code,'details':details},lambda db,u:
            {'gate_id':self.life._open_gate(db,cmd,code,details)})['gate_id']

    def dossier(self, mode='stall', trigger=None):
        with self.store.read_snapshot() as db:
            return build_dossier_in(db,_unit(db,'u'),mode=mode,trigger=trigger or {'gate_id':self.gate},now=self.now,
                observations={'base_oid':'a'*40,'observed_at':self.now,'issues':[{'repository_id':1,'issue_number':2,'state':'open','revision':'r2'}]},
                config={'provider_pool':[{'id':'codex-1'}],'environment_tiers':{'large':{}},'timeout_seconds':{'build':2700}})

    def reserve(self,dossier, retry_of=None):
        cmd=self.command(); manifest={'base_oid':'a'*40,'steward_dossier_digest':dossier['digest'],'steward_trigger_key':dossier['trigger_key']}
        def change(db,u):
            attempt,job=_id(cmd.request_id,'attempt'),_id(cmd.request_id,'job')
            db.execute('INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) VALUES(?,?,?,?,?,?,?,?)',
                (attempt,'u',self.member,u['membership_generation'],u['fencing_generation'],_json(manifest),u['budget_generation'],self.now))
            db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,created_at) VALUES(?,?,?,?,'steward','reserved',?)",(job,'u',attempt,self.member,self.now))
            db.execute('INSERT INTO job_leases(slot,job_id,execution_fence,acquired_at) VALUES(?,?,?,?)',('steward-1',job,job,self.now))
            insert_launch(db,job,'steward-1',job,Launch('forge/test','controller[bot]','codex',self.now+300,{},{}).payload(),self.now)
            return self.life.reserve_in(db,cmd,u,job_id=job,dossier=dossier,retry_of=retry_of)
        result=self.life._apply(cmd,'test_reserve_steward',{'dossier_digest':dossier['digest'],'retry_of':retry_of},change)
        return {'id':result['job_id'],'execution_fence':result['job_id'],'input_manifest':manifest,**result}

    def output(self,dossier,*,brief=True,action='retry',parameters=None,judgement='obvious'):
        choice={'id':'choice-1','action':action,'parameters':parameters or {'timeout_seconds':3000},'reason':'Observed bounded alternative','judgement':judgement}
        return {'version':1,'dossier_id':dossier['id'],'dossier_digest':dossier['digest'],'choices':[choice],
            'recommended_choice_id':'choice-1','confidence':.9,
            'brief':{'choice_id':'choice-1','instructions':'Preserve the original acceptance and rerun its checks.','evidence_refs':[]} if brief else None}

    def capture(self,job,output,**extra):
        self.life.collect(self.command(),job['id'],execution_fence=job['execution_fence'],capture={'result':output,'logs':'diagnostic',**extra})
        self.life.verify_cleanup(self.command(),job['id'],execution_fence=job['execution_fence'],evidence={'process_stopped':True,'resources_released':True})

    def accepted(self,*,brief=True,action='retry',parameters=None,judgement='obvious'):
        dossier=self.dossier(); job=self.reserve(dossier); output=self.output(dossier,brief=brief,action=action,parameters=parameters,judgement=judgement)
        self.capture(job,output)
        result=self.life.accept(self.command(),job_id=job['id'],execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],decision=output)
        return result['decision_id'],output,job

    def select(self,identifier,output,selection='auto',on_select=None):
        return self.life.select(self.command(),decision_id=identifier,choice_id=output['choices'][0]['id'],
            choice_digest=digest(output['choices'][0]),selection=selection,on_select=on_select)

    def test_capture_cleanup_bookkeeping_does_not_stale_semantic_pins_and_replay_is_exact(self):
        dossier=self.dossier();job=self.reserve(dossier);output=self.output(dossier)
        self.capture(job,output); cmd=self.command()
        kw=dict(job_id=job['id'],execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],decision=output)
        accepted=self.life.accept(cmd,**kw)
        self.assertEqual(self.life.accept(cmd,**kw),accepted)
        self.assertEqual(self.view()['charged_failures'],0)
        with self.store.read_snapshot() as db:
            self.assertEqual(dossier_in(db,job['id']),dossier)
            self.assertEqual(db.execute('SELECT count(*) FROM steward_decisions').fetchone()[0],1)

    def test_selected_is_not_applied_and_inline_brief_avoids_second_model_run(self):
        identifier,output,job=self.accepted();selected=self.select(identifier,output)
        self.assertFalse(selected['needs_brief'])
        with self.store.read_snapshot() as db:self.assertIsNone(selection_in(db,identifier)['application'])
        calls=[]
        def apply(db,cmd,unit,choice,brief):
            calls.append(brief['choice_id']); self.life._resolve(db,cmd,self.gate,{'choice':choice['id']});return {'scheduled':True}
        cmd=self.command();result=self.life.apply(cmd,decision_id=identifier,apply_move=apply)
        self.assertEqual(self.life.apply(cmd,decision_id=identifier,apply_move=apply),result)
        self.assertEqual(calls,['choice-1']);self.assertEqual(result['status'],'applied')
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],1)

    def test_adapter_failure_rolls_back_selection_and_application(self):
        identifier,output,_=self.accepted()
        def fail(*args):raise RuntimeError('adapter refused')
        with self.assertRaises(RuntimeError):self.select(identifier,output,on_select=fail)
        with self.store.read_snapshot() as db:self.assertIsNone(selection_in(db,identifier)['selection'])
        self.select(identifier,output)
        with self.assertRaises(RuntimeError):self.life.apply(self.command(),decision_id=identifier,apply_move=fail)
        with self.store.read_snapshot() as db:self.assertIsNone(selection_in(db,identifier)['application'])

    def test_brief_mode_keeps_exact_choice_and_is_another_bounded_cycle(self):
        identifier,output,_=self.accepted(brief=False);self.assertTrue(self.select(identifier,output)['needs_brief'])
        with self.assertRaisesRegex(TransitionError,'awaits its brief'):
            self.life.apply(self.command(),decision_id=identifier,apply_move=lambda *args:{})
        trigger={'decision_id':identifier,'choice_id':'choice-1','choice_digest':digest(output['choices'][0])}
        dossier=self.dossier('brief',trigger);job=self.reserve(dossier);enriched=self.output(dossier)
        changed=deepcopy(enriched);changed['choices'][0]['parameters']['timeout_seconds']=60
        with self.assertRaisesRegex(TransitionError,'cannot change'):decision(changed,dossier,accepted=output)
        self.capture(job,enriched)
        self.life.accept(self.command(),job_id=job['id'],execution_fence=job['id'],input_manifest=job['input_manifest'],decision=enriched)
        self.life.apply(self.command(),decision_id=identifier,apply_move=lambda *args:{'scheduled':True})
        with self.store.read_snapshot() as db:
            selected=selection_in(db,identifier);self.assertIsNotNone(selected['brief_decision_id'])
            self.assertEqual(db.execute('SELECT count(DISTINCT cycle) FROM steward_turns').fetchone()[0],2)

    def test_changed_requirements_and_independent_hold_stale_accepted_choice(self):
        identifier,output,_=self.accepted();self.open_gate('operator_pause',{'reason':'operator'})
        with self.assertRaises(VersionConflict):self.select(identifier,output)
        with self.store.read_snapshot() as db:self.assertIsNone(selection_in(db,identifier)['selection'])

    def test_independent_gate_and_budget_cannot_be_cleared_by_move_adapter(self):
        independent=self.open_gate('operator_pause',{'reason':'operator'})
        identifier,output,_=self.accepted();self.select(identifier,output)
        def bad(db,cmd,u,choice,brief):self.life._resolve(db,cmd,independent,{'bad':True});return {}
        with self.assertRaisesRegex(TransitionError,'independent holds'):self.life.apply(self.command(),decision_id=identifier,apply_move=bad)
        def reset(db,cmd,u,choice,brief):db.execute('UPDATE units SET budget_generation=budget_generation+1 WHERE id=?',(u['id'],));return {}
        with self.assertRaisesRegex(TransitionError,'budget'):self.life.apply(self.command(),decision_id=identifier,apply_move=reset)
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(independent,)).fetchone()[0])
            self.assertIsNone(selection_in(db,identifier)['application'])

    def test_model_claim_of_forge_failure_cannot_remove_real_work_charge(self):
        cmd=self.command();self.life._apply(cmd,'test_resolve',{},lambda db,u:self.life._resolve(db,cmd,self.gate,{'recheck':True}) or {})
        with self.store.read_snapshot() as db:
            revision=db.execute('SELECT requirements_revision_id FROM unit_issues WHERE id=?',(self.member,)).fetchone()[0]
        manifest={'requirements_revision_id':revision,'base_oid':'a'*40,'policy_hash':'policy'}
        created=self.life.reserve_job(self.command(),'build',slot='build-1',execution_fence='build-fence',member_id=self.member,
            input_manifest=manifest,launch=Launch('forge/test','controller[bot]','codex',self.now+300,{},{}))
        job={'id':created['job_id'],'execution_fence':'build-fence','input_manifest':manifest}
        self.capture(job,None)
        self.life.record_failure(self.command(),job['id'],execution_fence='build-fence',input_manifest=manifest,evidence={'reason':'failed tests'})
        dossier=self.dossier('recovery',{'failed_job_id':job['id']})
        output=self.output(dossier,action='escalate_forge',parameters={'signature':'a'*64})
        with self.assertRaisesRegex(TransitionError,'trusted retained'):decision(output,dossier)
        self.assertEqual(self.view()['charged_failures'],1)

    def test_options_must_be_grounded_and_judgement_requires_operator(self):
        dossier=self.dossier()
        for action,parameters in [('retry',{'provider_id':'unknown'}),('retry',{'timeout_seconds':3001}),
            ('rebuild',{'base_oid':'unobserved'}),('wait_on',{'repository_id':1,'issue_number':99})]:
            with self.subTest(action=action,parameters=parameters),self.assertRaises(TransitionError):
                decision(self.output(dossier,action=action,parameters=parameters),dossier)
        identifier,output,_=self.accepted(judgement='judgement')
        with self.assertRaisesRegex(TransitionError,'operator'):self.select(identifier,output)
        self.select(identifier,output,'operator')

    def test_manual_autonomy_opt_out_is_semantically_pinned(self):
        with self.store.write() as db:
            db.execute("INSERT INTO configuration(domain,subject,revision,active,settings_json,created_at) VALUES('steward','1',1,1,?,?)",(_json({'autonomy':'manual'}),self.now))
        identifier,output,_=self.accepted()
        with self.assertRaisesRegex(TransitionError,'operator'):self.select(identifier,output)
        self.select(identifier,output,'operator')

    def test_semantic_change_after_capture_rejects_model_result(self):
        dossier=self.dossier();job=self.reserve(dossier);output=self.output(dossier)
        self.capture(job,output)
        self.life.observe_issue(self.command(),'i',state='open',title='Changed',body='Changed requirements',requirements='new',remote_updated_at="2026-10-14T12:00:00Z",generation=1)
        with self.assertRaises((TransitionError,VersionConflict)):
            self.life.accept(self.command(),job_id=job['id'],execution_fence=job['id'],input_manifest=job['input_manifest'],decision=output)
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM steward_decisions').fetchone()[0],0)

    def test_acceptance_requires_exact_captured_decision(self):
        dossier=self.dossier();job=self.reserve(dossier);output=self.output(dossier)
        self.capture(job,output);changed=deepcopy(output);changed['confidence']=.8
        with self.assertRaisesRegex(TransitionError,'captured'):
            self.life.accept(self.command(),job_id=job['id'],execution_fence=job['id'],input_manifest=job['input_manifest'],decision=changed)

    def test_max_three_cycles_and_one_accepted_decision_per_trigger(self):
        self.accepted()
        with self.assertRaisesRegex(TransitionError,'authorized physical retry'):
            self.reserve(self.dossier())
        for index in range(2):
            cmd=self.command();self.life._apply(cmd,'test_resolve',{},lambda db,u:self.life._resolve(db,cmd,self.gate,{'recheck':True}) or {})
            self.gate=self.open_gate('forge_problem',{'source':'scheduling','reason':'stalled','owner':'dispatch','sequence':index})
            self.accepted()
        cmd=self.command();self.life._apply(cmd,'test_resolve',{},lambda db,u:self.life._resolve(db,cmd,self.gate,{'recheck':True}) or {})
        self.gate=self.open_gate('forge_problem',{'source':'scheduling','reason':'stalled','owner':'other'})
        with self.assertRaisesRegex(TransitionError,'cycle limit'):
            self.reserve(self.dossier())
        self.assertEqual(self.view()['charged_failures'],0)
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],3)

    def test_uncertain_external_effect_blocks_adapter_before_any_mutation(self):
        identifier,output,_=self.accepted();self.select(identifier,output)
        with self.store.write() as db:
            db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,payload_json,created_at,updated_at) VALUES('effect','effect','u','github_merge','uncertain','{}',100,100)")
        called=[]
        with self.assertRaisesRegex(TransitionError,'unsettled effects'):
            self.life.apply(self.command(),decision_id=identifier,apply_move=lambda *args:called.append(True) or {})
        self.assertEqual(called,[])

    def test_forged_provider_prose_cannot_reuse_physical_cycle(self):
        dossier=self.dossier();job=self.reserve(dossier);self.capture(job,None)
        self.life.defer_job(self.command(),job['id'],execution_fence=job['id'],input_manifest=job['input_manifest'],
            evidence={'kind':'provider_error'},code='provider_unavailable',details={'retry_at':130})
        self.now=131
        with self.assertRaisesRegex(TransitionError,'trusted provider'):
            self.reserve(self.dossier(),retry_of=job['id'])
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],1)

    def test_trusted_provider_retry_is_finite_and_reuses_cycle_without_charge(self):
        previous=None
        for attempt in range(3):
            dossier=self.dossier();job=self.reserve(dossier,retry_of=previous)
            # Controller lane owns launch admission; pin a host-observed container for terminal proof here.
            with self.store.write() as db:db.execute('UPDATE job_execution_state SET container_id=? WHERE job_id=?',('container',job['id']))
            self.capture(job,None,execution={'id':'container','image_id':'sha256:'+'a'*64,'exit_protocol':'1','running':False,'exit_code':1},
                execution_envelope={'version':1,'attempt':job['id'],'harness_status':'provider_error','provider_error':{'code':'rate_limit'},'exit_code':1})
            self.life.defer_job(self.command(),job['id'],execution_fence=job['id'],input_manifest=job['input_manifest'],
                evidence={'kind':'provider_error'},code='provider_unavailable',details={'retry_at':self.now+30})
            self.now+=31;previous=job['id']
            cmd=self.command()
            def release(db,u):
                for row in db.execute("SELECT id FROM unit_gates WHERE unit_id='u' AND code='provider_unavailable' AND resolved_at IS NULL").fetchall():
                    self.life._resolve(db,cmd,row[0],{'provider_ready':True})
                return {}
            self.life._apply(cmd,'test_provider_ready',{},release)
        with self.assertRaisesRegex(TransitionError,'physical retry limit'):
            self.reserve(self.dossier(),retry_of=previous)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(DISTINCT cycle) FROM steward_turns').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM steward_turns').fetchone()[0],3)
        self.assertEqual(self.view()['charged_failures'],0)

    def test_worker_schema_is_closed_finite_and_brief_pins_accepted_choices(self):
        dossier=self.dossier();schema=decision_schema(dossier)
        def check(value):
            if isinstance(value,dict):
                if value.get('type')=='object':
                    self.assertIs(value['additionalProperties'],False)
                    self.assertEqual(set(value['properties']),set(value['required']))
                for child in value.values():check(child)
            elif isinstance(value,list):
                for child in value:check(child)
        check(schema)
        self.assertEqual(schema['properties']['dossier_digest']['enum'],[dossier['digest']])
        identifier,output,_=self.accepted(brief=False);self.select(identifier,output)
        brief=self.dossier('brief',{'decision_id':identifier,'choice_id':'choice-1','choice_digest':digest(output['choices'][0])})
        frozen=decision_schema(brief,accepted=output);check(frozen)
        self.assertEqual(frozen['properties']['choices']['enum'],[output['choices']])
        self.assertEqual(frozen['properties']['brief']['properties']['choice_id']['enum'],['choice-1'])


if __name__=='__main__':unittest.main()
