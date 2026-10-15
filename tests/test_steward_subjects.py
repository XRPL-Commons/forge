import json
import unittest
from copy import deepcopy
from unittest.mock import patch

from forge.factory.config import validate
from forge.store.models import Command, TransitionError, VersionConflict, IdempotencyConflict
from forge.store.transitions import Lifecycle, manifest_hash
from forge.steward_contract import decision, decision_schema
from forge.steward_subjects import triage_dossiers, triage_schema, triage_output
from tests.steward_subject_fixture import output, choice, row_choice
from tests import test_repository_planner as planning_fixture
from tests import test_store_discussions as conversation_fixture


class ProposalSubjects(unittest.TestCase):
    def setUp(self):
        self.f=planning_fixture.RepositoryPlannerTests();self.f.setUp();self.addCleanup(self.f.doCleanups)
        f=self.f
        Lifecycle(f.store,clock=lambda:f.now).admit_issue(request_id='second',actor_id='operator',unit_id='second',issue_id='1:2',
            repository_id=1,repository_name='example/project',issue_number=2,title='Other work',body='More details',requirements='other',observed_at=90)
        f.plans.refresh_inputs(f.request['proposal_id'])
        context=f.plans.context(f.request['proposal_id'],head='a'*40)
        job={**f.job,'base_branch':'main','steward_mode':'triage','runner_config':validate({})}
        dossiers=triage_dossiers(context,job);context.update(steward_dossiers=dossiers,steward_schema=triage_schema(dossiers,context))
        job['context_hash']=manifest_hash(context)
        self.context,self.job,self.dossiers=context,job,dossiers

    def capture(self,value):
        f=self.f;f.plans.reserve(f.request['proposal_id'],self.job,self.context)
        f.plans.progress(self.job['id'],capture={'result':value,'logs':'bounded'},fenced=True,cleanup=True)
        return f.plans.publish_decisions(f.request['proposal_id'],execution_id=self.job['id'],request_id='publish',actor_id='supervisor:triage')

    def result(self):
        return dict(version=1,decisions=[output(d,[row_choice('keep',d['authority']['anchor_issue_id']),
            row_choice('question',d['authority']['anchor_issue_id'],{'question':'Please clarify scope.'},identity='ask')]) for d in self.dossiers],
            metadata=dict(reply='Keep both issues; ask only where needed.',notes=[],handoff=None))

    def test_ranked_alternative_selection_and_partial_acceptance_keep_history(self):
        self.capture(self.result());f=self.f;proposal=f.plans.proposal(f.request['proposal_id']);a,b=proposal['rows']
        self.assertEqual(a['selected_choice_id'],'first')
        args=dict(expected_version=proposal['version'],choice_id='ask',digest=a['steward_decision']['digest'],request_id='choose',actor_id='github:123')
        receipt=f.plans.choose_row(proposal['id'],a['id'],**args)
        self.assertEqual(f.plans.choose_row(proposal['id'],a['id'],**args),receipt)
        with self.assertRaises(IdempotencyConflict):f.plans.choose_row(proposal['id'],a['id'],**{**args,'choice_id':'first'})
        current=f.plans.proposal(proposal['id']);self.assertEqual(current['rows'][0]['action'],'question')
        self.assertEqual(current['rows'][0]['status'],'proposed')
        result=f.plans.accept(proposal['id'],expected_version=current['version'],accept_rows=[a['id']],reject_rows=[],
            request_id='accept',actor_id='github:123',config=validate({}))
        final=f.plans.proposal(proposal['id'])
        self.assertEqual([r['status'] for r in final['rows']],['accepted','proposed'])
        self.assertEqual(result['effects'],[])
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM steward_subject_decisions').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM triage_row_history WHERE row_id=?',(a['id'],)).fetchone()[0],2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)
        # Accepted rows cannot be replaced, even with a retained valid choice.
        with self.assertRaises(TransitionError):f.plans.choose_row(proposal['id'],a['id'],**{**args,'request_id':'late','expected_version':final['version']})

    def test_alternatives_cannot_change_members_or_alias_captured_subject(self):
        result=self.result();result['decisions'][0]['choices'][1]['parameters']['members']=['1:2']
        with self.assertRaises(TransitionError):triage_output(result,self.dossiers)
        result=self.result();result['decisions'][0]['dossier_digest']='0'*64
        with self.assertRaises(TransitionError):triage_output(result,self.dossiers)
        result=self.result();result['decisions'].pop()
        with self.assertRaises(TransitionError):triage_output(result,self.dossiers)

    def test_chosen_brief_cannot_silently_follow_manual_row_edit(self):
        self.capture(self.result());f=self.f;proposal=f.plans.proposal(f.request['proposal_id']);row=proposal['rows'][0]
        f.plans.edit_row(proposal['id'],row['id'],expected_version=proposal['version'],request_id='edit',actor_id='github:123',
            value=dict(action='keep',members=['1:1'],reason='Manually reviewed scope',rule_quote=None,parameters={}))
        self.assertIsNone(f.plans.proposal(proposal['id'])['rows'][0]['selected_choice_id'])

    def test_typed_wait_is_grounded_and_accepted_through_existing_dependency_authority(self):
        value=self.result();first=value['decisions'][0]
        first['choices']=[row_choice('wait_on','1:1',{'repository_id':1,'issue_number':2})]
        self.capture(value);f=self.f;proposal=f.plans.proposal(f.request['proposal_id']);row=proposal['rows'][0]
        self.assertEqual(row['action'],'order')
        from forge.store.repository_requirements import order_in,orders_in
        f.plans.accept(proposal['id'],expected_version=proposal['version'],accept_rows=[row['id']],reject_rows=[],
            request_id='wait',actor_id='github:123',config=validate({}),order_in=order_in)
        with f.store.read_snapshot() as db:
            self.assertEqual(orders_in(db,'1:1',self.context['issues'][0]['requirements_revision_id']),['example/project#2'])
        invalid=deepcopy(value);invalid['decisions'][0]['choices'][0]['parameters']['details']['issue_number']=1
        with self.assertRaises(TransitionError):triage_output(invalid,self.dossiers)

    def test_captured_notes_and_restart_publish_use_the_same_decision_envelope(self):
        f=self.f;value=self.result()
        source={'kind':'issue_revision','identity':self.context['issues'][0]['requirements_revision_id'],
            'description':'Captured requirement','url':'https://github.com/example/project/issues/1'}
        value['metadata']['notes']=[{'note_id':None,'kind':'lesson','content':'Keep the exact context.', 'sources':[source]}]
        f.plans.reserve(f.request['proposal_id'],self.job,self.context)
        f.plans.progress(self.job['id'],capture={'result':value},fenced=True,cleanup=True)
        result=f.controller.cycle(launch=False)
        self.assertEqual(result['items'][0]['phase'],'complete')
        self.assertEqual(f.plans.repository(1)['notes'][0]['sources'],[source])
        self.assertEqual(f.plans.repository(1)['notes'][0]['actor_id'],'supervisor:triage')
        self.assertEqual(f.controller.cycle(launch=False)['items'],[])

    def environment_proposal(self):
        f=self.f;config=validate({})
        config['environment_tiers']={'standard':{'image':'sha256:'+'1'*64}}
        # Rebuild captured dossiers with the actual configured environment capability.
        context={k:v for k,v in self.context.items() if k not in {'steward_dossiers','steward_schema'}}
        self.job['runner_config']=config
        self.dossiers=triage_dossiers(context,self.job)
        context.update(steward_dossiers=self.dossiers,steward_schema=triage_schema(self.dossiers,context))
        self.context=context;self.job['context_hash']=manifest_hash(context)
        value=self.result();value['decisions'][0]['brief']={'choice_id':'first','instructions':'Run focused tests.',
            'evidence_refs':[],'environment':{'environment_id':'standard','services':[]}}
        self.capture(value)
        return config,f.plans.proposal(f.request['proposal_id'])

    def test_brief_environment_applies_only_on_exact_approved_choice(self):
        f=self.f;config,proposal=self.environment_proposal();a,b=proposal['rows']
        with f.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM unit_environment').fetchone()[0],0)
        args=dict(expected_version=proposal['version'],accept_rows=[a['id']],reject_rows=[],request_id='accept-env',
            actor_id='github:123',config=config)
        receipt=f.plans.accept(proposal['id'],**args)
        self.assertEqual(f.plans.accept(proposal['id'],**args),receipt)
        with f.store.read_snapshot() as db:
            selected=json.loads(db.execute('SELECT choice FROM unit_environment').fetchone()[0])
            self.assertEqual(selected['source'],'steward')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM unit_environment').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0],2)
            self.assertEqual(db.execute("SELECT version FROM units WHERE id='second'").fetchone()[0],1)
            effect=db.execute("SELECT expected_version FROM external_effects WHERE kind='environment_label'").fetchone()
            self.assertEqual(effect[0],2)
            saved=db.execute("SELECT * FROM decisions WHERE command='triage_environment'").fetchall()
            self.assertEqual(len(saved),1)
            self.assertEqual(saved[0]['actor_id'],'github:123')
            self.assertEqual(json.loads(saved[0]['payload_json'])['choice_id'],'first')
        self.assertEqual([row['status'] for row in f.plans.proposal(proposal['id'])['rows']],['accepted','proposed'])
        with self.assertRaises(VersionConflict):
            Lifecycle(f.store).pause(Command('stale-before-environment','github:123','unit',1))

    def test_matching_manual_environment_does_not_advance_unit_or_add_effect(self):
        f=self.f;config,proposal=self.environment_proposal();row=proposal['rows'][0]
        with f.store.write() as db:
            db.execute('INSERT INTO issue_environment_observations(issue_id,choice_json) VALUES(?,?)',
                ('1:1',json.dumps({'tier':'standard','services':[],'automatic':False})))
        f.plans.accept(proposal['id'],expected_version=proposal['version'],accept_rows=[row['id']],reject_rows=[],
            request_id='matching-env',actor_id='github:123',config=config)
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM unit_environment').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM external_effects').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM decisions WHERE command='triage_environment'").fetchone()[0],0)

    def test_physical_invocation_limit_keeps_all_attempts_without_budget_charge(self):
        f=self.f
        from forge.store.auxiliary import save_in
        with f.store.write() as db:
            for i in range(3):
                job={**self.job,'id':'old-'+str(i)}
                save_in(db,'triage',f.request['proposal_id'],job,f.now)
                save_in(db,'triage',f.request['proposal_id'],{**job,'fenced':True,'cleanup':True},f.now)
        with self.assertRaisesRegex(TransitionError,'three-invocation'):
            f.plans.reserve(f.request['proposal_id'],self.job,self.context)
        self.assertEqual(f.controller._start(f.plans.runs()[0])['error'],'steward_cycle_limit')
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_executions').fetchone()[0],3)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)


class ConversationSubjects(unittest.TestCase):
    def setUp(self):
        self.f=conversation_fixture.DiscussionTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_exact_selected_alternative_replays_and_cannot_change_identity(self):
        f=self.f;f.row['actions']=['retry','pause'];job=f.start()
        value=output(job['context']['steward_dossier'],[choice('retry'),choice('pause',identity='pause')],reply='Pause or retry after diagnosis.')
        f.runner.inspect.return_value={'status':'exited','running':False}
        f.runner.capture.return_value={'result':value,'logs':'','execution':{'exit_code':0}}
        f.service.drive(job,f.github)
        with conversation_fixture.requesting(return_value='receipt') as request:
            f.service.apply('1:2',job['id'],choice_id='pause')
            self.assertEqual(request.call_args.args[1],'pause')
            f.service.apply('1:2',job['id'],choice_id='pause')
            with self.assertRaises(IdempotencyConflict):f.service.apply('1:2',job['id'],choice_id='first')
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM steward_subject_decisions').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)
        self.assertEqual(f.service.view('1:2')['messages'][-1]['selected_choice_id'],'pause')

    def test_three_invocations_require_a_new_authenticated_message(self):
        f=self.f;job=f.start()
        for index in range(3):
            f.service.cancel('1:2');f.service.drive(job,f.github)
            f.send('Why did this fail?')
            f.service.tick(f.config,f.github,f.settings)
            if index<2:job=f.service.read()['1:2']['job']
        self.assertEqual(f.service.commands.invocation_count('1:2'),3)
        self.assertEqual(f.service.view('1:2')['status'],'unavailable')
        self.assertIn('three-invocation',f.service.view('1:2')['messages'][-1]['text'])
        f.send('Please investigate the updated test requirement.')
        f.service.tick(f.config,f.github,f.settings)
        self.assertEqual(f.service.commands.invocation_count('1:2'),1)
        self.assertEqual(f.service.view('1:2')['status'],'running')
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM discussion_executions').fetchone()[0],4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0],0)

    def test_reply_uses_same_contract_and_preserves_exact_authenticated_subject(self):
        f=self.f;job=f.start();d=job['context']['steward_dossier']
        self.assertEqual(d['subject']['actor_id'],'github:123')
        value=output(d,[choice('answer')],reply='The current gate explains the wait.')
        self.assertEqual(decision(value,d),value)
        schema=decision_schema(d)
        self.assertEqual(schema['properties']['brief']['anyOf'][1]['properties']['environment'],{'type':'null'})
        changed=deepcopy(value);changed['dossier_id']='another-turn'
        with self.assertRaises(TransitionError):decision(changed,d)
