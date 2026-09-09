"""Trusted adapter tests use temporary state and fake GitHub/tmux observations."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import time
import unittest
from unittest.mock import patch

from forge import workflow as engine
from forge import workflow_control as control
from forge.control import atomic_json, snapshot_path
from tests.host_isolation import deny_host_tmux


class WorkflowControlTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.state=self.root/'state';self.config=self.root/'repos.d';self.config.mkdir()
        self.repo='Org/Fixture';self.session='Fixture-build';self.gen='$1|100|%1|10'
        (self.config/'one.repo').write_text('REPO=Org/Fixture\nOWNER_LABEL=auto:owned-by-test\n')
        self.now=time.time();self.calls=[]
        self.snap={'repo':self.repo,'enabled':True,'status':'ok','observed_at':self.now,
            'counts':{'builder':1,'ready':1,'review':0,'audit':0,'wip':0,'sentback':0,'limbo':0,'conflicts':0,'held':0},
            'work':{'ready':[1],'wip':[],'review':[],'audit':[],'sentback':[],'limbo':[],'conflicts':[],'held':[]},
            'schedule':{'eligible':[1],'active':[],'handed_off':[],'reserving':[],'ordered':[1],
                        'observed_at':self.now,'first_ready':{'1':self.now},'blocked':{}},
            'item_details':{'1':{'number':1,'kind':'issue','state':'open','title':'Assigned issue',
                                'labels':['ready'],'url':'https://github.com/Org/Fixture/issues/1'}}}
        self.save()
        self.pr={'number':9,'url':'https://github.com/Org/Fixture/pull/9','state':'OPEN','isDraft':False,
            'headRefOid':'a'*40,'baseRefOid':'b'*40,'labels':[{'name':'ready-for-review'}],
            'author':{'login':'forge-bot'},'closingIssuesReferences':[{'number':1,'url':'https://github.com/Org/Fixture/issues/1'}]}
        self.issue={'number':1,'url':'https://github.com/Org/Fixture/issues/1','state':'OPEN','labels':[{'name':'wip'}]}

    def save(self):atomic_json(snapshot_path(self.state,self.repo),self.snap)

    def github(self,args,**kwargs):
        self.calls.append(args)
        if args[:3]==['gh','pr','view']:value=self.pr
        elif args[:3]==['gh','issue','view']:value=self.issue
        else:self.fail('unexpected external command: '+repr(args))
        return SimpleNamespace(returncode=0,stdout=json.dumps(value),stderr='')

    def reserve(self,session=None):
        return control.reserve(self.config,self.state,session or self.session,run=self.github)

    def bind(self,a):
        return engine.transition(self.state,a['id'],'bind',source_event_id='bind',generation=self.gen,provider='codex')

    def review_snapshot(self):
        self.snap['work']['ready']=[];self.snap['work']['wip']=[1];self.snap['work']['review']=[9]
        self.snap['schedule'].update(eligible=[],active=[1],handed_off=[1],reserving=[])
        self.snap['item_details']['1']['labels']=['wip']
        self.snap['item_details']['9']={'number':9,'kind':'pr','state':'open','title':'Exact PR','body':'Closes #1',
            'labels':['ready-for-review'],'url':self.pr['url']}
        self.save()

    def test_quarantine_refuses_before_github_or_new_reservation(self):
        with patch('forge.execution_retirement.is_quarantined',return_value=True):
            with self.assertRaisesRegex(engine.Refused,'preserved_workspace_requires_reconciliation'):
                self.reserve()
        self.assertEqual(self.calls,[])
        self.assertEqual(engine.read_state(self.state)['assignments'],[])

    def test_builder_reservation_targets_scheduler_first_without_remote_guess(self):
        self.snap['schedule']['eligible']=[1,2];self.save()
        a=self.reserve()
        self.assertEqual(a['issue_numbers'],[1]);self.assertIsNone(a['pr'])
        self.assertEqual(self.calls,[])
        slot,actual=control.current(self.state,self.session)
        self.assertEqual(actual['id'],a['id']);self.assertEqual(slot['state'],'reserved')

    def test_stale_or_unknown_snapshot_cannot_create_reservation(self):
        for change in ({'observed_at':self.now-301},{'status':'unknown'}):
            original=copy.deepcopy(self.snap);self.snap.update(change);self.save()
            with self.assertRaises(engine.Refused):self.reserve()
            self.snap=original;self.save()
        self.assertEqual(engine.read_state(self.state)['assignments'],[])

    def test_review_uses_canonical_link_and_exact_head_base(self):
        self.review_snapshot();a=self.reserve('Fixture-review')
        self.assertEqual(a['issue_numbers'],[1]);self.assertEqual(a['pr'],{'number':9,'head':'a'*40,'base':'b'*40})
        self.assertEqual([c[1:3] for c in self.calls],[['pr','view'],['issue','view']])

    def test_canonical_target_rejects_hold_draft_conflict_and_cross_repo_link(self):
        for change in ({'isDraft':True},{'labels':[{'name':'ready-for-review'},{'name':'hold'}]},
                       {'labels':[{'name':'ready-for-review'},{'name':'ready-for-audit'}]},
                       {'closingIssuesReferences':[{'number':1,'url':'https://github.com/Other/Repo/issues/1'}]}):
            original=copy.deepcopy(self.pr);self.pr.update(change)
            with self.assertRaises(engine.Refused):control.canonical_pr(self.state,self.repo,9,'review',self.github)
            self.pr=original

    def test_wrong_returned_issue_number_is_not_authoritative(self):
        self.issue['number']=2
        with self.assertRaises(engine.Refused):control.canonical_pr(self.state,self.repo,9,'review',self.github)

    def test_canonical_closed_issue_does_not_get_assigned(self):
        self.issue['state']='CLOSED'
        with self.assertRaises(engine.Refused):control.canonical_pr(self.state,self.repo,9,'review',self.github)

    def test_unchanged_snapshot_clock_does_not_change_revision_or_history(self):
        _,first=control.reconcile_repository(self.state,self.repo,now=self.now)
        count=len(engine.history(self.state))
        self.snap['observed_at']+=1;self.snap['schedule']['observed_at']+=1;self.save()
        _,second=control.reconcile_repository(self.state,self.repo,now=self.now+1)
        self.assertEqual(first,second)
        self.assertEqual(len(engine.history(self.state)),count)

    def test_guarded_next_matches_repository_case_and_exact_generation(self):
        a=self.reserve();self.bind(a)
        with patch.object(control,'observe_generation',return_value={'id':self.gen}):
            self.assertEqual(control.next_for_assignment(self.state,self.repo,a['id'],{'eligible':[1,2]}),1)
            self.assertIsNone(control.next_for_assignment(self.state,self.repo,a['id'],{'eligible':[2]}))
        with patch.object(control,'observe_generation',return_value={'id':'$2|100|%2|20'}):
            with self.assertRaises(engine.Refused):control.next_for_assignment(self.state,self.repo,a['id'],{'eligible':[1]})

    def test_mutation_cannot_bind_replacement_generation(self):
        a=self.reserve()
        with patch.object(control,'observe_generation',return_value={'id':'$2|100|%2|20'}):
            with self.assertRaises(engine.Refused):control.mutate(self.config,self.state,self.session,'bind',self.gen,'codex')
        self.assertIsNone(control.current(self.state,self.session)[1]['generation'])

    def test_partial_send_stays_uncertain_without_successful_delivery_action(self):
        a=self.reserve();self.bind(a)
        with patch.object(control,'observe_generation',return_value={'id':self.gen}):
            control.mutate(self.config,self.state,self.session,'uncertain',self.gen,reason='dispatch_in_progress')
        self.assertEqual(control.current(self.state,self.session)[1]['state'],'uncertain')
        self.assertEqual(control.current(self.state,self.session)[0]['state'],'blocked')

    def test_bootstrap_records_actual_unknown_occupied_slot_without_assignment(self):
        observation={self.session:{'generation_id':self.gen,'state':'idle','provider':'codex'}}
        with patch('forge.activity.observe_tmux',return_value=observation):control.observe_slots(self.config,self.state,bootstrap=True)
        slot,a=control.current(self.state,self.session)
        self.assertIsNone(a);self.assertTrue(slot['bootstrap']);self.assertEqual(slot['state'],'retiring')
        self.assertEqual(slot['reason'],'started_before_assignment_tracking')
        with self.assertRaises(engine.Refused):self.reserve()

    def test_new_unassigned_execution_is_not_mislabelled_legacy(self):
        with patch('forge.activity.observe_tmux',return_value={self.session:{'generation_id':self.gen,'state':'busy','provider':'claude'}}):
            control.observe_slots(self.config,self.state)
        slot,a=control.current(self.state,self.session)
        self.assertFalse(slot['bootstrap']);self.assertEqual(slot['reason'],'execution_without_assignment')

    def test_live_legacy_slot_counts_against_new_reservation_capacity(self):
        engine.observe_slot(self.state,'other-build','Other/Repo','build',self.gen,'blocked',bootstrap=True,source_event_id='legacy')
        with patch.dict('os.environ',{'MAX_SESSIONS':'1','MAX_SESSIONS_PER_ROLE':'2'}):
            with self.assertRaisesRegex(engine.Refused,'global_slot_capacity'):self.reserve()

    def test_prompt_binds_exact_target_and_forbids_sibling_selection(self):
        a=self.reserve();prompt=control.assignment_prompt(self.state,self.session)
        self.assertIn(a['id'],prompt);self.assertIn('issue #1',prompt)
        self.assertIn('do not select or claim any other issue',prompt)

    def absent(self,args,**kwargs):
        if args[:2]==['tmux','list-sessions']:
            return SimpleNamespace(returncode=0,stdout='',stderr='')
        return self.github(args,**kwargs)

    def hold_target(self):
        self.snap['schedule']['eligible']=[];self.snap['work']['ready']=[];self.snap['work']['held']=[1]
        self.snap['item_details']['1']['labels']=['ready','hold'];self.save()

    def test_prestart_provider_block_reuses_same_valid_reservation(self):
        a=self.reserve()
        engine.transition(self.state,a['id'],'blocked',generation=None,reason='provider_unavailable',source_event_id='provider-block')
        b=control.reserve(self.config,self.state,self.session,run=self.absent)
        self.assertEqual(a['id'],b['id']);self.assertEqual(b['state'],'blocked')
        self.assertEqual(len(engine.read_state(self.state)['assignments']),1)

    def test_invalid_unbound_reservation_cancels_only_after_confirmed_absence(self):
        a=self.reserve();self.hold_target()
        with self.assertRaises(engine.Refused):control.reserve(self.config,self.state,self.session,run=self.absent)
        slot,current=control.current(self.state,self.session)
        self.assertIsNone(current);self.assertEqual(slot['state'],'free')
        old=next(x for x in engine.read_state(self.state)['assignments'] if x['id']==a['id'])
        self.assertEqual(old['state'],'retired')
        self.assertEqual(old['reason'],'unstarted_assignment_became_ineligible')

    def test_unknown_tmux_read_preserves_invalid_reservation(self):
        a=self.reserve();self.hold_target()
        def unknown(args,**kwargs):return SimpleNamespace(returncode=1,stdout='',stderr='permission denied')
        with self.assertRaisesRegex(engine.Refused,'absence_unverified'):
            control.reserve(self.config,self.state,self.session,run=unknown)
        self.assertEqual(control.current(self.state,self.session)[1]['id'],a['id'])

    def test_fresh_hold_fences_bind_before_any_worker_input(self):
        a=self.reserve();self.hold_target()
        with patch.object(control,'observe_generation',return_value={'id':self.gen}):
            control.mutate(self.config,self.state,self.session,'bind',self.gen,'codex')
        a=control.current(self.state,self.session)[1]
        self.assertEqual(a['generation'],self.gen)
        with self.assertRaises(engine.Refused):control.validate_target(self.config,self.state,a,run=self.absent)

    def test_external_claim_after_reservation_is_not_implicitly_owned(self):
        a=self.reserve()
        self.snap['schedule'].update(eligible=[],active=[1],reserving=[1])
        self.snap['work']['ready']=[];self.snap['work']['wip']=[1]
        self.snap['item_details']['1']['labels']=['wip'];self.save()
        with self.assertRaises(engine.Refused):control.validate_target(self.config,self.state,a,run=self.absent)

    def observe_assigned(self,phase,thread='22222222-2222-4222-8222-222222222222'):
        observation={self.session:{'state':phase,'generation_id':self.gen,'provider':'codex',
            'provider_run_id':'11111111-1111-4111-8111-111111111111',
            'provider_thread_id':thread,'execution_started_at':self.now-1,'reason':'codex_execution_'+phase}}
        with patch('forge.activity.observe_tmux',return_value=observation):control.observe_slots(self.config,self.state)

    def test_short_turn_completed_before_first_sample_retires_and_keeps_execution(self):
        a=self.reserve();self.bind(a)
        engine.transition(self.state,a['id'],'submitted',generation=self.gen,source_event_id='submitted')
        self.observe_assigned('idle')
        slot,current=control.current(self.state,self.session)
        self.assertEqual(slot['state'],'retiring')
        self.assertEqual(current['execution']['run_id'],'11111111-1111-4111-8111-111111111111')
        self.assertEqual(current['execution']['started_at'],self.now-1)

    def test_later_thread_identity_is_recorded_without_duplicate_working_events(self):
        a=self.reserve();self.bind(a)
        engine.transition(self.state,a['id'],'submitted',generation=self.gen,source_event_id='submitted')
        self.observe_assigned('busy',None)
        self.observe_assigned('busy')
        current=control.current(self.state,self.session)[1]
        self.assertEqual(current['execution']['thread_id'],'22222222-2222-4222-8222-222222222222')
        before=len(engine.history(self.state));self.observe_assigned('busy')
        self.assertEqual(len(engine.history(self.state)),before)

    def test_actual_next_cli_accepts_lowercase_assignment_repo(self):
        import contextlib
        import io
        from forge.scheduling import main
        a=self.reserve();self.bind(a)
        output=io.StringIO()
        with patch('forge.scheduling.SchedulingGitHub') as api, patch('forge.scheduling.gate_untriaged'), patch.object(control,'observe_generation',return_value={'id':self.gen}), contextlib.redirect_stdout(output):
            api.return_value.items.return_value=[{'number':1,'title':'Assigned issue','body':'','state':'open','labels':[{'name':'ready'}]}]
            api.return_value.dependency_map.return_value={1:[]}
            code=main(['next','--repo',self.repo.lower(),'--assignment',a['id'],'--config',str(self.config),'--state',str(self.state)])
        self.assertEqual(code,0);self.assertEqual(output.getvalue().strip(),'1')

    def test_confirmed_missing_worker_remains_occupied_with_explicit_reason(self):
        a=self.reserve();self.bind(a)
        engine.transition(self.state,a['id'],'submitted',generation=self.gen,source_event_id='submitted')
        with patch('forge.activity.observe_tmux',return_value={self.session:{'state':'exited','reason':'session_absent'}}):
            control.observe_slots(self.config,self.state)
        slot,current=control.current(self.state,self.session)
        self.assertEqual(slot['state'],'blocked');self.assertEqual(slot['assignment_id'],a['id'])
        self.assertEqual(current['reason'],'assigned_worker_missing')
        self.assertEqual(current['generation'],self.gen)

    def test_replacement_generation_is_not_adopted_and_old_assignment_remains(self):
        a=self.reserve();self.bind(a)
        with patch('forge.activity.observe_tmux',return_value={self.session:{'state':'busy','generation_id':'$2|101|%2|20','provider':'codex'}}):
            control.observe_slots(self.config,self.state)
        slot,current=control.current(self.state,self.session)
        self.assertEqual(current['generation'],self.gen);self.assertEqual(slot['state'],'blocked')
        self.assertEqual(current['reason'],'assignment_generation_mismatch')

    def test_observer_reclaims_invalid_never_submitted_target_even_when_queue_empty(self):
        a=self.reserve();self.hold_target()
        with patch('forge.activity.observe_tmux',return_value={self.session:{'state':'exited','reason':'session_absent'}}):
            control.observe_slots(self.config,self.state,run=self.github)
        slot,current=control.current(self.state,self.session)
        self.assertIsNone(current);self.assertEqual(slot['state'],'free')
        self.assertEqual(engine.read_state(self.state)['assignments'][0]['id'],a['id'])

    def test_observer_preserves_unknown_absence_and_unknown_target(self):
        a=self.reserve();self.hold_target()
        with patch('forge.activity.observe_tmux',return_value={self.session:{'state':'unknown','reason':'tmux_unavailable'}}):
            control.observe_slots(self.config,self.state,run=self.github)
        self.assertEqual(control.current(self.state,self.session)[1]['id'],a['id'])
        self.snap['observed_at']=self.now-400;self.save()
        with patch('forge.activity.observe_tmux',return_value={self.session:{'state':'exited','reason':'session_absent'}}):
            control.observe_slots(self.config,self.state,run=self.github)
        self.assertEqual(control.current(self.state,self.session)[1]['id'],a['id'])

    def test_observer_retains_valid_never_submitted_reservation(self):
        a=self.reserve()
        with patch('forge.activity.observe_tmux',return_value={self.session:{'state':'exited','reason':'no_tmux_server'}}):
            control.observe_slots(self.config,self.state,run=self.github)
        self.assertEqual(control.current(self.state,self.session)[1]['id'],a['id'])

    def test_invalid_unstarted_migration_is_retired_with_distinct_proof(self):
        a=self.reserve();self.bind(a)
        engine.transition(self.state,a['id'],'submitted',generation=self.gen,source_event_id='submitted')
        engine.transition(self.state,a['id'],'blocked',generation=self.gen,source_event_id='blocked')
        proof=dict(verified=True,released=True,kind='verified_provider_handover',assignment_id=a['id'],generation=self.gen)
        engine.transition(self.state,a['id'],'migrate',generation=self.gen,proof=proof,source_event_id='migrate')
        self.hold_target()
        with patch('forge.activity.observe_tmux',return_value={self.session:{'state':'exited','reason':'session_absent'}}):
            control.observe_slots(self.config,self.state,run=self.github)
        slot,current=control.current(self.state,self.session)
        self.assertIsNone(current);self.assertEqual(slot['state'],'free')
        old=next(row for row in engine.read_state(self.state)['assignments'] if row['id']==a['id'])
        self.assertIsNotNone(old['submitted_at']);self.assertEqual(old['attempt'],2)
        events=engine.history(self.state)
        self.assertEqual(events[-1]['payload']['proof']['kind'],'verified_unstarted_attempt')

    def test_canonical_author_and_conflicting_repository_reference_are_rejected(self):
        with self.assertRaisesRegex(engine.Refused,'author_changed'):
            control.canonical_pr(self.state,self.repo,9,'review',self.github,author='someone-else')
        self.pr['closingIssuesReferences'][0]['repository']={'nameWithOwner':'Other/Repository'}
        with self.assertRaisesRegex(engine.Refused,'identity_conflict'):
            control.canonical_pr(self.state,self.repo,9,'review',self.github)
