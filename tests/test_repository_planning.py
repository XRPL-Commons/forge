"""Planning suggestions acquire authority only through atomic operator acceptance."""
import tempfile
import unittest
from unittest.mock import Mock

from forge.store.db import Store
from forge.store.models import Command, VersionConflict, TransitionError
from forge.store.repository_planning import RepositoryPlanning
from forge.store.transitions import Lifecycle


class PlanningTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store.create(temporary.name);self.now=100
        self.lifecycle=Lifecycle(self.store,clock=lambda:self.now)
        self.planning=RepositoryPlanning(self.store,clock=lambda:self.now)
        self.config={'ready_label':'ready','hold_labels':['hold','wip']}
        for n in range(1,4):
            self.lifecycle.admit_issue(request_id='admit-'+str(n),actor_id='operator',unit_id='unit-'+str(n),
                issue_id='issue-'+str(n),repository_id=1,repository_name='example/project',issue_number=n,
                title='Issue '+str(n),body='Complete requirement',requirements={'acceptance':['complete']},observed_at=90)

    def rows(self):
        source={'kind':'commit','identity':'a'*40,'description':'Fix landed','url':'https://github.com/example/project/commit/'+'a'*40}
        return [dict(action='close',members=['issue-1'],reason='Already fixed',rule_quote=None,parameters={'evidence':[source]}),
                dict(action='park',members=['issue-2'],reason='Content rule',rule_quote='Park content',parameters={'add':['content']}),
                dict(action='question',members=['issue-3'],reason='Scope unclear',rule_quote=None,parameters={'question':'What should change?'})]

    def proposal(self):
        self.planning.configure(1,request_id='rules',actor_id='operator',expected_revision=0,rules='Park content')
        self.planning.begin(1,proposal_id='proposal',base_branch='main',request_id='begin',actor_id='planner')
        self.planning.publish('proposal',expected_version=1,rows=self.rows(),rationale='One backlog pass',request_id='publish',actor_id='planner')
        return self.planning.proposal('proposal')['rows']

    def accept(self,rows,**kwargs):
        return self.planning.accept('proposal',expected_version=2,accept_rows=[r['id'] for r in rows],reject_rows=[],
            request_id='accept',actor_id='github:7',config=self.config,**kwargs)

    def test_proposal_never_changes_work_until_atomic_acceptance_and_replay(self):
        rows=self.proposal()
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM unit_gates').fetchone()[0],0)
        result=self.accept(rows)
        self.assertEqual(result['status'],'accepted');self.assertEqual(len(result['effects']),2)
        self.assertEqual(self.accept(rows),result)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],2)
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='triage_sync' AND resolved_at IS NULL").fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)
        public=self.planning.proposal('proposal')
        self.assertEqual(public['rows'][0]['effects'][0]['status'],'pending')
        self.assertEqual(public['rows'][1]['rule_quote'],'Park content')

    def test_stale_input_rolls_back_every_effect_and_decision(self):
        rows=self.proposal()
        self.lifecycle.pause(Command('pause','operator','unit-2',1))
        with self.assertRaises(VersionConflict):self.accept(rows)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM triage_proposal_rows WHERE status='accepted'").fetchone()[0],0)
            self.assertFalse(db.execute("SELECT 1 FROM repository_planning_requests WHERE request_id='accept'").fetchone())

    def test_rules_change_invalidates_acceptance_and_missing_dispositions_roll_back(self):
        rows=self.proposal()
        self.planning.configure(1,request_id='new-rules',actor_id='operator',expected_revision=1,rules='Keep content')
        with self.assertRaises(VersionConflict):self.accept(rows)
        self.planning.begin(1,proposal_id='incomplete',base_branch='main',request_id='begin-2',actor_id='planner')
        with self.assertRaises(TransitionError):
            self.planning.publish('incomplete',expected_version=1,rows=self.rows()[:1],rationale='Incomplete',request_id='bad-publish',actor_id='planner')
        self.assertEqual(self.planning.proposal('incomplete')['rows'],[])

    def test_note_history_is_bounded_inspectable_and_source_changes_need_refresh(self):
        with self.store.read_snapshot() as db:revision=db.execute("SELECT current_revision_id FROM issues WHERE id='issue-1'").fetchone()[0]
        source={'kind':'issue_revision','identity':revision,'description':'Operator requirement','url':'https://github.com/example/project/issues/1'}
        args=dict(repository_id=1,note_id='convention',expected_revision=0,kind='convention',content='Use existing UI controls.',sources=[source],request_id='note',actor_id='github:7')
        saved=self.planning.note(**args)
        self.assertEqual(self.planning.note(**args),saved)
        self.assertFalse(self.planning.repository(1)['notes'][0]['needs_refresh'])
        self.planning.note(**{**args,'expected_revision':1,'content':'Use the revised controls.','request_id':'note-2'})
        with self.assertRaises(VersionConflict):self.planning.note(**{**args,'request_id':'stale-note'})
        self.lifecycle.observe_issue(Command('changed','github','unit-1',1),'issue-1',state='open',title='Changed',body='New requirements',requirements={'acceptance':['new']},remote_updated_at='2026-10-10T10:00:00Z',generation=1)
        self.assertTrue(self.planning.repository(1)['notes'][0]['needs_refresh'])
        with self.store.read_snapshot() as db:self.assertEqual(db.execute('SELECT count(*) FROM repository_note_revisions').fetchone()[0],2)

    def test_edit_and_reject_have_versioned_history_without_external_authority(self):
        rows=self.proposal();row=self.rows()[0];row.update(action='keep',parameters={},reason='Needs investigation first')
        self.planning.edit_row('proposal',rows[0]['id'],expected_version=2,value=row,request_id='edit',actor_id='github:7')
        with self.assertRaises(VersionConflict):self.accept(rows)
        self.planning.accept('proposal',expected_version=3,accept_rows=[],reject_rows=[rows[1]['id']],request_id='reject',actor_id='github:7',config=self.config)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM triage_row_history WHERE row_id=?',(rows[0]['id'],)).fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)

    def test_lost_close_response_reconciles_once_and_park_keeps_work_fenced(self):
        from copy import deepcopy
        from forge.factory.github import GitHubUnavailable
        from forge.factory.triage_effects import TriageEffects
        rows=self.proposal();result=self.accept(rows)
        remote={f'issue-{n}':{'number':n,'state':'open','state_reason':None,'labels':[],'assignees':[]} for n in range(1,4)}
        github=Mock();github.issue.side_effect=lambda item:(deepcopy(remote[item['key']]),{'acceptance':['complete']})
        calls=[];lost=[True]
        def call(role,item,method,path,payload=None):
            calls.append((method,path,payload));issue=remote[item['key']]
            if method=='PATCH':
                issue.update(payload)
                if lost.pop():raise GitHubUnavailable('Lost response after GitHub completed closure')
            elif method=='POST':issue['labels']=sorted(set(issue['labels'])|set(payload['labels']))
            elif method=='DELETE':issue['labels'].remove(path.rsplit('/',1)[1])
        github.call.side_effect=call
        effects=TriageEffects(self.store,github,config=self.config,clock=lambda:self.now)
        first=effects.step(result['effects'][0]);self.assertEqual(first['status'],'pending')
        reads = github.issue.call_count
        self.assertEqual(effects.step(result['effects'][0])['reason'], 'retry_delay')
        self.assertEqual(github.issue.call_count, reads)
        self.now += 30
        with self.store.read_snapshot() as db:version=db.execute("SELECT version FROM units WHERE id='unit-1'").fetchone()[0]
        self.lifecycle.observe_issue(Command('observed-close','supervisor','unit-1',version),'issue-1',state='closed',state_reason='completed',
            title='Issue 1',body='Complete requirement',requirements={'acceptance':['complete']},remote_updated_at='2026-10-10T00:00:00Z',generation=1)
        replay=effects.step(result['effects'][0]);self.assertEqual(replay['status'],'confirmed')
        self.assertEqual(sum(method=='PATCH' for method,_,_ in calls),1)
        closed=self.planning.members(rows[0]['id'])['items'][0]
        self.assertEqual((closed['github_state'],closed['observed_at']),('closed',self.now))
        self.assertEqual(self.planning.proposal('proposal')['rows'][0]['effects'][0]['status'],'confirmed')
        self.assertEqual(effects.step(result['effects'][1])['status'],'confirmed')
        self.assertEqual(remote['issue-2']['labels'],['content','hold'])
        with self.store.read_snapshot() as db:
            issue=db.execute("SELECT github_state,state_reason FROM issues WHERE id='issue-1'").fetchone()
            self.assertEqual(tuple(issue),('closed','completed'))
            self.assertTrue(db.execute("SELECT 1 FROM unit_gates WHERE unit_id='unit-2' AND code='label_hold' AND member_id IS NOT NULL AND resolved_at IS NULL").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_changed_remote_ownership_prevents_any_triage_write(self):
        from forge.factory.triage_effects import TriageEffects
        rows=self.proposal();result=self.accept(rows)
        github=Mock();github.issue.return_value=({'number':1,'state':'open','labels':[],'assignees':[{'login':'someone'}]}, {'acceptance':['complete']})
        effect=TriageEffects(self.store,github,config=self.config,clock=lambda:self.now)
        self.assertEqual(effect.step(result['effects'][0])['status'],'failed')
        github.call.assert_not_called()

    def test_triage_retry_cap_stops_writes_but_allows_later_readback(self):
        from copy import deepcopy
        from forge.factory.github import GitHubUnavailable
        from forge.factory.triage_effects import TriageEffects
        rows = self.proposal(); accepted = self.accept(rows)
        identifier = accepted['effects'][0]
        remote = {'number': 1, 'state': 'open', 'state_reason': None, 'labels': [], 'assignees': []}
        github = Mock()
        github.issue.side_effect = lambda item: (deepcopy(remote), {'acceptance': ['complete']})
        github.call.side_effect = GitHubUnavailable('unconfirmed write')
        config = {**self.config, 'reliability': {'effect_max_attempts': 2, 'effect_retry_cap_seconds': 60}}
        effects = TriageEffects(self.store, github, config=config, clock=lambda: self.now)
        self.assertEqual(effects.step(identifier)['reason'], 'github_unavailable')
        self.now += 30
        self.assertEqual(effects.step(identifier)['reason'], 'effect_reconciliation')
        self.assertEqual(github.call.call_count, 2)
        with self.store.read_snapshot() as db:
            gate = dict(db.execute("SELECT * FROM unit_gates WHERE code='forge_problem' AND unit_id='unit-1'").fetchone())
            self.assertIsNone(gate['resolved_at'])
            self.assertEqual(db.execute('SELECT attempts FROM external_effects WHERE id=?', (identifier,)).fetchone()[0], 2)
        self.now += 60
        self.assertEqual(effects.step(identifier)['reason'], 'effect_reconciliation')
        self.assertEqual(github.call.call_count, 2)
        remote.update(state='closed', state_reason='completed')
        self.now += 60
        self.assertEqual(effects.step(identifier)['status'], 'confirmed')
        self.assertEqual(github.call.call_count, 2)
        from forge.store.scheduling import Scheduling
        with self.store.read_snapshot() as db:
            version = db.execute("SELECT version FROM units WHERE id='unit-1'").fetchone()[0]
        answer = Scheduling(self.store, clock=lambda: self.now).recheck(
            Command('reconciled', 'supervisor', 'unit-1', version), gate['id'])
        self.assertTrue(answer['resolved'])

    def test_queued_request_is_atomic_and_does_not_leave_orphan_drafts(self):
        queued=self.planning.queue(1,request_id='queued',actor_id='operator',base_branch='main')
        self.assertEqual(self.planning.queue(1,request_id='queued',actor_id='operator',base_branch='main'),queued)
        with self.assertRaises(TransitionError):self.planning.queue(1,request_id='other',actor_id='operator',base_branch='main')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM triage_proposals').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM repository_triage_runs').fetchone()[0],1)

    def test_fresh_independent_evidence_accepts_overnight_proposal_without_repinning(self):
        rows=self.proposal();self.now=1000
        with self.assertRaises(TransitionError):self.accept(rows)
        with self.store.read_snapshot() as db:
            proof=[dict(row)|{'verified':True,'observed_at':self.now} for row in db.execute(
                'SELECT issue_id,issue_version,requirements_revision_id FROM triage_proposal_inputs WHERE proposal_id=?',('proposal',))]
        self.accept(rows,issue_evidence=proof)
        # Fresh observation timestamps are evidence, not a different user request.
        proof[0]['observed_at']+=1
        self.assertEqual(self.accept(rows,issue_evidence=proof)['status'],'accepted')

    def test_accept_composes_group_and_durable_issue_effect_in_one_transaction(self):
        from forge.store.delivery import Delivery
        self.planning.begin(1,proposal_id='combined',base_branch='main',request_id='combine-begin',actor_id='planner')
        rows=[{'action':'group','members':['issue-1','issue-2'],'reason':'One coherent delivery','rule_quote':None,
            'parameters':{'dependencies':[{'issue_id':'issue-2','prerequisite_issue_id':'issue-1'}],'existing_prs':[],'publication_target':{'kind':'new'}}},
            {'action':'park','members':['issue-3'],'reason':'Content can wait','rule_quote':None,'parameters':{'add':[]}}]
        self.planning.publish('combined',expected_version=1,rows=rows,rationale='Group and park',request_id='combined-publish',actor_id='planner')
        public=self.planning.proposal('combined')
        receipt=self.planning.accept('combined',expected_version=2,accept_rows=[r['id'] for r in public['rows']],reject_rows=[],
            request_id='combined-accept',actor_id='operator',config=self.config,compose_in=Delivery(self.store,clock=lambda:self.now).compose_in)
        with self.store.read_snapshot() as db:
            self.assertEqual(len(receipt['units']),1)
            self.assertEqual(db.execute('SELECT count(*) FROM unit_issues WHERE unit_id=? AND left_at IS NULL',(receipt['units'][0],)).fetchone()[0],2)
            self.assertEqual(len(receipt['effects']),1)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_two_member_effects_share_delivery_without_fencing_or_clearing_each_other(self):
        from forge.store.delivery import Delivery
        from forge.store.repository_planning import pin_in
        from forge.factory.triage_effects import TriageEffects
        delivery=Delivery(self.store,clock=lambda:self.now)
        with self.store.read_snapshot() as db:
            members=[{k:v for k,v in pin_in(db,key).items() if k!='candidate_id'} for key in ['issue-1','issue-2']]
        args=dict(repository_id=1,base_branch='main',members=members,rationale='Related',dependencies=[],existing_prs=[])
        delivery.compose(Command('compose','operator','group',1),**args,approval={'approved':True,'composition_hash':delivery.composition_hash(**args)})
        rows=self.proposal();accepted=self.accept(rows)
        with self.store.read_snapshot() as db:
            gates=db.execute("SELECT member_id FROM unit_gates WHERE unit_id='group' AND code='triage_sync' AND resolved_at IS NULL").fetchall()
            self.assertEqual(len(gates),2);self.assertTrue(all(g[0] for g in gates))
            before=db.execute("SELECT fencing_generation FROM units WHERE id='group'").fetchone()[0]
        remote={n:{'number':n,'state':'open','state_reason':None,'labels':[],'assignees':[]} for n in (1,2)}
        github=Mock();github.issue.side_effect=lambda item:(remote[item['number']],{'acceptance':['complete']})
        def call(role,item,method,path,payload=None):
            issue=remote[item['number']]
            if method=='PATCH':issue.update(payload)
            if method=='POST':issue['labels']=sorted(set(issue['labels'])|set(payload['labels']))
            return issue
        github.call.side_effect=call
        effects=TriageEffects(self.store,github,config=self.config,clock=lambda:self.now)
        self.assertEqual(effects.step(accepted['effects'][0])['status'],'confirmed')
        with self.store.write() as db:
            # A fresh observation of the closed sibling changes unit/issue
            # counters while retaining the approved live membership.
            db.execute("UPDATE units SET version=version+1 WHERE id='group'")
            db.execute("UPDATE issues SET version=version+1 WHERE id='issue-1'")
        self.assertEqual(effects.step(accepted['effects'][1])['status'],'confirmed')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT fencing_generation FROM units WHERE id='group'").fetchone()[0],before)
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='triage_sync' AND resolved_at IS NULL").fetchone()[0],0)
            hold=db.execute("SELECT member_id FROM unit_gates WHERE code='label_hold' AND resolved_at IS NULL").fetchone()
            self.assertEqual(hold[0],db.execute("SELECT id FROM unit_issues WHERE unit_id='group' AND issue_id='issue-2'").fetchone()[0])
