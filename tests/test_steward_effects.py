"""Selected issue actions survive lost replies without duplicate POSTs."""
import json
import unittest
import uuid
from unittest.mock import Mock

from forge.factory.github import GitHubUnavailable
from forge.factory.steward_effect_controller import StewardEffectController
from forge.factory.steward_service import StewardService
from forge.store.steward_effects import queue_in
from tests import test_steward_core as core


class StewardEffectsTests(unittest.TestCase):
    def setUp(self):
        self.f = core.StewardCoreTests(); self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.store = self.f.store
        self.service = StewardService(store=self.store, actor_id='github:1609016', clock=lambda:self.f.now)
        self.github = Mock()
        self.github.publisher.return_value = 'controller[bot]'
        self.issues = {1: {'number':1,'state':'open','labels':[],'assignees':[]}}
        self.comments = {}; self.created = []; self.writes = []
        self.github.issue.side_effect = lambda item: (dict(self.issues[item['number']]), 'original acceptance')
        self.github.pages.side_effect = lambda role,item,path: self.created if path.startswith('/issues?') else self.comments.get(path,[])
        self.github.call.side_effect = self.call
        self.driver = StewardEffectController(self.store,self.github,config={'enabled':True},clock=lambda:self.f.now)

    def call(self,role,item,method,path,payload):
        self.writes.append((method,path,payload))
        if method == 'PATCH': self.issues[item['number']].update(payload); return self.issues[item['number']]
        value = dict(payload,id=100+len(self.writes),user={'login':'controller[bot]','type':'Bot'})
        if path == '/issues':
            value['number'] = 10+len(self.created); self.created.append(value)
        else: self.comments.setdefault(path,[]).append(value)
        return value

    def queue(self,action='question',parameters=None):
        identifier,output,_ = self.f.accepted(action=action,parameters=parameters or {'text':'Which behavior is intended?'},judgement='judgement')
        view = self.service.view('i')['decision']
        self.service.choose(key='i',decision_id=identifier,choice_id='choice-1',digest=view['digest'],request_id=str(uuid.uuid4()))
        with self.store.read_snapshot() as db:
            from forge.store.steward import decision_in
            dossier=decision_in(db,identifier)[2]
        self.f.life.apply(self.f.command(),decision_id=identifier,apply_move=lambda db,cmd,u,c,b:
            queue_in(db,cmd,u,decision_id=identifier,choice=c,dossier=dossier))
        with self.store.read_snapshot() as db:
            effects=[r[0] for r in db.execute('SELECT effect_id FROM steward_issue_effects WHERE decision_id=? ORDER BY position',(identifier,))]
        return identifier,effects

    def test_question_remains_pending_until_readback_and_keeps_work_open(self):
        identifier,effects=self.queue()
        self.assertEqual(self.service.view('i')['decision']['application']['status'],'pending')
        self.assertEqual(self.driver.step(effects[0])['status'],'confirmed')
        self.assertEqual(self.service.view('i')['decision']['application']['status'],'applied')
        self.assertEqual(self.f.view()['state'],'queued')
        self.driver.step(effects[0]); self.assertEqual(len(self.writes),1)

    def test_lost_post_reply_is_reconciled_without_resending(self):
        _,effects=self.queue()
        def lost(*args): self.call(*args); raise GitHubUnavailable('lost reply')
        self.github.call.side_effect=lost
        self.assertEqual(self.driver.step(effects[0])['status'],'pending')
        self.f.now+=31
        self.assertEqual(self.driver.step(effects[0])['status'],'confirmed')
        self.assertEqual(len(self.writes),1)

    def test_confirmed_remote_step_keeps_receipt_and_backs_off_blocked_local_completion(self):
        _,effects=self.queue()
        def held(*args):
            value=self.call(*args)
            self.f.open_gate('operator_pause',{'reason':'pause during reply'})
            return value
        self.github.call.side_effect=held
        self.assertEqual(self.driver.step(effects[0])['status'],'pending')
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT status,receipt_json FROM external_effects WHERE id=?',(effects[0],)).fetchone()
            self.assertEqual(row[0],'confirmed');self.assertTrue(json.loads(row[1])['verified'])
        self.assertEqual(self.driver.step(effects[0])['reason'],'retry_delay')
        self.assertEqual(self.driver.cycle()['items'],[])
        self.assertEqual(len(self.writes),1)

    def test_unobserved_post_is_never_blindly_repeated(self):
        _,effects=self.queue()
        self.github.call.side_effect=GitHubUnavailable('no response')
        self.driver.step(effects[0]); self.f.now+=31
        self.assertEqual(self.driver.step(effects[0])['reason'],'post_outcome_unconfirmed')
        self.assertEqual(self.github.call.call_count,1)
        self.assertEqual(self.service.view('i')['decision']['application']['status'],'pending')

    def test_hold_after_selection_prevents_remote_write(self):
        _,effects=self.queue(); self.f.open_gate('operator_pause',{'reason':'stop'})
        self.assertEqual(self.driver.step(effects[0])['status'],'pending')
        self.github.call.assert_not_called()

    def test_paused_runtime_prevents_new_writes_but_allows_readback(self):
        _,effects=self.queue()
        self.driver.config={'enabled':False}
        self.assertEqual(self.driver.step(effects[0])['reason'],'runtime_or_repository_disabled')
        self.github.call.assert_not_called()

    def test_late_confirmation_resolves_only_its_own_uncertainty_gate(self):
        _,effects=self.queue()
        def lost(*args): self.call(*args); raise GitHubUnavailable('lost reply')
        self.github.call.side_effect=lost
        self.driver.step(effects[0])
        self.f.now+=901
        from forge.store.effects import Effects
        self.assertTrue(Effects(self.store,clock=lambda:self.f.now).bounded(effects[0]))
        self.f.now+=901
        self.driver.step(effects[0])
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute("SELECT 1 FROM unit_gates WHERE resolved_at IS NULL AND json_extract(details_json,'$.effect_id')=?",(effects[0],)).fetchone())
        self.assertEqual(self.service.view('i')['decision']['application']['status'],'applied')

    def test_drop_comments_before_closing_and_only_then_settles_locally(self):
        _,effects=self.queue('drop',{'intent_note':'This work is no longer needed.'})
        self.assertEqual(self.driver.step(effects[-1])['reason'],'earlier_step')
        self.driver.step(effects[0]); self.assertEqual(self.f.view()['state'],'queued')
        self.driver.step(effects[1]); self.assertEqual(self.f.view()['state'],'closed')
        self.assertEqual([write[0] for write in self.writes],['POST','PATCH'])

    def lost_closure(self,*,approved=True):
        if approved:
            with self.store.write() as db:
                db.execute("INSERT INTO unit_publication_targets VALUES('u','{}')")
        self.issues[1].update(title='Keep intent',body='Original body',updated_at='2026-10-14T10:00:00Z')
        identifier,effects=self.queue('drop',{'intent_note':'This work is no longer needed.'})
        self.driver.step(effects[0])
        def lost(*args):
            self.call(*args)
            raise GitHubUnavailable('lost closure reply')
        self.github.call.side_effect=lost
        self.assertEqual(self.driver.step(effects[1])['reason'],'github_unavailable')
        return identifier,effects

    def observe_closure(self):
        from forge.factory.observation_controller import ObservationController
        observer=ObservationController(self.store,self.github,admission=lambda _:True,clock=lambda:self.f.now)
        return observer.observe_member('u',self.f.member,'i')

    def test_lost_closure_reply_survives_normal_observation_before_readback(self):
        identifier,effects=self.lost_closure()
        observed=self.observe_closure()
        self.assertEqual(observed['pending'],'steward_issue')
        self.assertEqual(observed['effect_id'],effects[-1])
        self.assertEqual(self.f.view()['state'],'queued')
        self.f.now+=31
        self.assertEqual(self.driver.step(effects[-1])['status'],'confirmed')
        self.assertEqual(self.f.view()['state'],'closed')
        self.assertEqual(len(self.writes),2)
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute('SELECT completed_at FROM steward_effect_groups WHERE decision_id=?',(identifier,)).fetchone()[0])
            self.assertFalse(db.execute("SELECT 1 FROM unit_gates WHERE code='membership_decision'").fetchone())

    def test_single_issue_observation_leaves_settlement_to_the_pending_closure(self):
        _,effects=self.lost_closure(approved=False)
        self.assertEqual(self.observe_closure()['pending'],'steward_issue')
        self.assertEqual(self.f.view()['state'],'queued')
        self.f.now+=31
        self.assertEqual(self.driver.step(effects[-1])['status'],'confirmed')
        self.assertEqual(self.f.view()['state'],'closed')

    def test_own_closure_does_not_clear_an_independent_membership_decision(self):
        _,effects=self.lost_closure()
        gate=self.f.life.open_gate(self.f.command(),'membership_decision',member_id=self.f.member,
            details={'reason':'requirements_changed','revision_id':'independent-revision'})['gate_id']
        self.assertEqual(self.observe_closure()['pending'],'steward_issue')
        self.f.now+=31
        self.assertEqual(self.driver.step(effects[-1])['reason'],'Accepted steward issue authority changed')
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(gate,)).fetchone()[0])
            self.assertIsNone(db.execute('SELECT completed_at FROM steward_effect_groups').fetchone()[0])
            self.assertEqual(db.execute('SELECT status FROM external_effects WHERE id=?',(effects[-1],)).fetchone()[0],'confirmed')

    def test_closure_with_changed_labels_requires_its_independent_membership_decision(self):
        _,effects=self.lost_closure()
        self.issues[1]['labels']=['new-scope']
        self.assertEqual(self.observe_closure()['pending'],'membership_decision')
        self.f.now+=31
        self.assertEqual(self.driver.step(effects[-1])['reason'],'Accepted steward issue authority changed')
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT completed_at FROM steward_effect_groups').fetchone()[0])
            self.assertTrue(db.execute("SELECT 1 FROM unit_gates WHERE code='membership_decision' AND resolved_at IS NULL").fetchone())

    def test_closure_with_changed_requirements_preserves_the_requirements_hold(self):
        _,effects=self.lost_closure()
        self.github.issue.side_effect=lambda item:(dict(self.issues[item['number']]),'new acceptance')
        self.assertEqual(self.observe_closure()['pending'],'membership_decision')
        self.f.now+=31
        self.assertEqual(self.driver.step(effects[-1])['reason'],'Accepted steward issue authority changed')
        with self.store.read_snapshot() as db:
            gate=db.execute("SELECT details_json FROM unit_gates WHERE code='membership_decision' AND resolved_at IS NULL").fetchone()
            self.assertEqual(json.loads(gate[0])['reason'],'requirements_changed')
            self.assertIsNone(db.execute('SELECT completed_at FROM steward_effect_groups').fetchone()[0])

    def test_closure_with_changed_assignee_type_is_not_normalized(self):
        self.issues[1]['assignees']=[{'login':'worker','type':'Bot'}]
        with self.store.write() as db:
            db.execute('UPDATE issues SET assignees_json=?,assignee_types_json=? WHERE id=?',
                       (json.dumps(['worker']),json.dumps({'worker':'Bot'}),'i'))
        _,effects=self.lost_closure()
        self.issues[1]['assignees'][0]['type']='User'
        self.assertEqual(self.observe_closure()['pending'],'membership_decision')
        self.f.now+=31
        self.assertEqual(self.driver.step(effects[-1])['reason'],'Accepted steward issue authority changed')
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT completed_at FROM steward_effect_groups').fetchone()[0])
            self.assertTrue(db.execute("SELECT 1 FROM unit_gates WHERE code='membership_decision' AND resolved_at IS NULL").fetchone())

    def test_changed_assignee_type_prevents_remote_write_before_observation(self):
        self.issues[1]['assignees']=[{'login':'worker','type':'Bot'}]
        with self.store.write() as db:
            db.execute('UPDATE issues SET assignees_json=?,assignee_types_json=? WHERE id=?',
                       (json.dumps(['worker']),json.dumps({'worker':'Bot'}),'i'))
        _,effects=self.queue()
        self.issues[1]['assignees'][0]['type']='User'
        self.assertEqual(self.driver.step(effects[0])['reason'],'Source issue requirements, ownership, labels or state changed')
        self.github.call.assert_not_called()

    def test_split_creates_children_before_comments_and_closure(self):
        _,effects=self.queue('split',{'children':[
            {'title':'First','body':'First scope','acceptance_criteria':['First passes']},
            {'title':'Second','body':'Second scope','acceptance_criteria':['Second passes']}],
            'intent_note':'Preserve both parts of the original work.'})
        for effect in effects:self.driver.step(effect)
        self.assertEqual(self.f.view()['state'],'closed')
        self.assertEqual([write[1] for write in self.writes],['/issues','/issues','/issues/1/comments','/issues/1'])
        self.assertIn('owner/repo#10',self.writes[2][2]['body'])
        self.assertIn('owner/repo#11',self.writes[2][2]['body'])

    def test_full_dossier_scope_is_visible_and_every_source_is_commented_then_closed(self):
        added=self.f.life.admit_issue(request_id='second',actor_id='operator',unit_id='second',issue_id='second',
            repository_id=1,repository_name='owner/repo',issue_number=2,title='Second obligation',body='Second body',
            requirements='original acceptance',observed_at=90)
        # Establish the fixture's second membership before the dossier exists,
        # retaining its first membership as history.
        with self.store.write() as db:
            db.execute("UPDATE unit_issues SET left_at=95,exit_reason='dissolved',state='removed' WHERE id=?",(added['member_id'],))
            db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) "
                "SELECT id||'-group','u',issue_id,repository_id,base_branch,requirements_revision_id,'pending',96 FROM unit_issues WHERE id=?",
                (added['member_id'],))
        self.issues[2]={'number':2,'state':'open','labels':[],'assignees':[]}
        _,effects=self.queue('drop',{'intent_note':'Both obligations are accounted for here.'})
        self.assertEqual([row['number'] for row in self.service.view('i')['scope']['issues']],[1,2])
        for effect in effects:self.driver.step(effect)
        self.assertEqual([write[0] for write in self.writes],['POST','POST','PATCH','PATCH'])
        self.assertEqual({write[1] for write in self.writes if write[0]=='PATCH'},{'/issues/1','/issues/2'})
        self.assertEqual(self.f.view()['state'],'closed')

    def test_fold_records_target_and_source_intent_before_closure(self):
        self.issues[2]={'number':2,'state':'open','labels':[],'assignees':[]}
        self.github.issue.side_effect=lambda item:(dict(self.issues[item['number']]),'r2' if item['number']==2 else 'original acceptance')
        _,effects=self.queue('fold',{'repository_id':1,'issue_number':2,'intent_note':'Continue this intent in issue 2.'})
        for effect in effects:self.driver.step(effect)
        self.assertEqual([write[1] for write in self.writes],['/issues/2/comments','/issues/1/comments','/issues/1'])
        self.assertEqual(self.issues[2]['state'],'open')
        self.assertEqual(self.f.view()['state'],'closed')

    def test_escalation_exposes_trusted_report_draft_without_publishing(self):
        _,_,job=self.f.accepted()
        from forge.harnesses.execution import fault
        details=fault('SchemaError');signature=details['signature']
        with self.store.write() as db:
            db.execute('INSERT INTO forge_problem_reports VALUES(?,?,?)',(signature,json.dumps(details),self.f.now))
            db.execute('INSERT INTO forge_problem_report_jobs VALUES(?,?)',(signature,job['id']))
        self.f.now+=1
        command=self.f.command()
        self.f.life._apply(command,'test_repaired_stall',{},lambda db,u:
            self.f.life._resolve(db,command,self.f.gate,{'repaired':True}) or {})
        self.f.gate=self.f.open_gate('forge_problem',{'source':'scheduling','reason':'stalled','owner':'dispatch'})
        _,effects=self.queue('escalate_forge',{'signature':signature})
        self.assertEqual(self.driver.step(effects[0])['reason'],'operator_report_draft')
        view=self.service.view('i')
        self.assertEqual(view['report_draft']['signature'],signature)
        self.assertEqual(view['report_draft']['status'],'draft')
        self.assertEqual(view['decision']['application']['status'],'pending')
        self.github.call.assert_not_called()
