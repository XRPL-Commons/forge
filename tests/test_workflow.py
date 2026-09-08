"""Workflow authority tests use temporary SQLite only: no GitHub or workers."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from forge import workflow as w


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.state=Path(self.temp.name);self.n=0;self.now=1000
        self.gen='$1|900|%1|10'
        self.repo='Owner/Repo'
        self.pr={'number':9,'head':'a'*40,'base':'b'*40}
        self.sync()

    def event(self):
        self.n+=1;return 'event-'+str(self.n)

    def sync(self, *, repo=None, revision='v1', observed_at=1000, issues=None):
        return w.reconcile(self.state,repo or self.repo,revision,observed_at,
            issues if issues is not None else [{'number':1,'stage':'queued','prs':[]}],
            source_event_id=self.event(),now=self.now)

    def reserve(self, *, repo=None, role='build', session='repo-build', revision='v1', numbers=None, **kwargs):
        return w.reserve(self.state,repo or self.repo,role,session,revision,[1] if numbers is None else numbers,
                         source_event_id=self.event(),now=self.now,**kwargs)

    def change(self,a,action,**kwargs):
        kwargs.setdefault('generation',self.gen)
        return w.transition(self.state,a['id'],action,source_event_id=self.event(),now=self.now,**kwargs)

    def bound(self):
        a=self.reserve();return self.change(a,'bind',provider='codex')

    def submitted(self):
        return self.change(self.bound(),'submitted')

    def proof(self,a,kind='verified_handoff'):
        return dict(verified=True,released=True,assignment_id=a['id'],generation=self.gen,kind=kind)

    def view(self):return w.read_state(self.state)

    def test_exact_assignment_and_slot_lifecycle_are_atomic(self):
        a=self.reserve();v=self.view()
        self.assertEqual(v['issues'][0]['stage'],'build')
        self.assertEqual(v['slots'][0]['assignment_id'],a['id'])
        self.assertEqual(v['slots'][0]['state'],'reserved')
        self.change(a,'bind',provider='codex');self.change(a,'submitted');self.change(a,'working')
        v=self.view();self.assertEqual(v['issues'][0]['disposition'],'working');self.assertEqual(v['slots'][0]['state'],'working')
        self.change(a,'idle');self.assertEqual(self.view()['issues'][0]['disposition'],'pending')
        self.change(a,'handoff',proof=self.proof(a));self.assertEqual(self.view()['slots'][0]['state'],'retiring')
        self.change(a,'retired',proof=self.proof(a));v=self.view()
        self.assertEqual(v['slots'][0]['state'],'free');self.assertIsNone(v['slots'][0]['assignment_id'])
        self.assertEqual(v['issues'][0]['stage'],'build') # releasing a worker never completes an issue

    def test_running_process_survives_stage_advance_without_regressing_issue(self):
        a=self.submitted()
        self.now=1001
        self.sync(revision='v2',observed_at=1001,issues=[{'number':1,'stage':'review','prs':[self.pr]}])
        self.change(a,'working')
        view=self.view()
        self.assertEqual(view['slots'][0]['state'],'working')
        self.assertEqual(view['assignments'][0]['state'],'working')
        self.assertEqual((view['issues'][0]['stage'],view['issues'][0]['disposition']),('review','pending'))
        self.change(a,'idle')
        with self.assertRaisesRegex(w.Refused,'target_changed'):self.change(a,'submitted')

    def test_running_old_pr_revision_does_not_claim_new_revision_during_observe_or_reconcile(self):
        self.sync(issues=[{'number':1,'stage':'review','prs':[self.pr]}])
        a=self.reserve(role='review',pr=self.pr)
        self.change(a,'bind',provider='codex');self.change(a,'submitted')
        for key in ('head','base'):
            with self.subTest(changed=key):
                changed={**self.pr,key:'c'*40}
                self.now+=1
                self.sync(revision='v'+str(self.now),observed_at=self.now,issues=[{'number':1,'stage':'review','prs':[changed]}])
                self.change(a,'working')
                self.assertEqual(self.view()['slots'][0]['state'],'working')
                self.assertEqual(self.view()['issues'][0]['disposition'],'pending')
                self.now+=1
                self.sync(revision='v'+str(self.now),observed_at=self.now,issues=[{'number':1,'stage':'review','prs':[changed]}])
                self.assertEqual(self.view()['issues'][0]['disposition'],'pending')
                self.change(a,'idle')
                with self.assertRaisesRegex(w.Refused,'pr_revision_changed'):self.change(a,'submitted')

    def test_running_process_preserves_authoritative_critical_and_stalled_states(self):
        a=self.submitted()
        for disposition in ('critical','stalled'):
            with self.subTest(disposition=disposition):
                self.now+=1
                self.sync(revision='v'+str(self.now),observed_at=self.now,issues=[
                    {'number':1,'stage':'build','disposition':disposition,'reason':'recorded_issue_blocker'}])
                self.change(a,'working')
                self.assertEqual(self.view()['slots'][0]['state'],'working')
                issue=self.view()['issues'][0]
                self.assertEqual(issue['disposition'],disposition)
                self.assertEqual(issue['reason'],'recorded_issue_blocker')

    def test_event_replay_is_idempotent_but_payload_change_refused(self):
        args=(self.state,self.repo,'build','repo-build','v1',[1])
        a=w.reserve(*args,source_event_id='reserve-once',now=1000)
        b=w.reserve(*args,source_event_id='reserve-once',now=1200)
        self.assertEqual(a,b)
        self.assertEqual(len(self.view()['assignments']),1)
        with self.assertRaisesRegex(w.Refused,'source_event_conflict'):
            w.reserve(*args,source_event_id='reserve-once',provider='claude',now=1200)

    def test_stale_revision_or_snapshot_blocks_new_dispatch(self):
        with self.assertRaisesRegex(w.Refused,'revision_changed'):self.reserve(revision='old')
        self.now=1301
        with self.assertRaisesRegex(w.Refused,'not_fresh'):self.reserve()

    def test_old_observation_cannot_regress_state_and_absence_not_closure(self):
        self.now=1002;self.sync(revision='v2',observed_at=1002,issues=[{'number':1,'stage':'audit','disposition':'critical','prs':[self.pr]}])
        result=self.sync(revision='old',observed_at=1001)
        self.assertEqual(result['status'],'ignored_stale')
        self.now=1003;self.sync(revision='v3',observed_at=1003,issues=[])
        self.assertEqual(self.view()['issues'][0]['stage'],'audit')

    def test_completion_and_cancellation_are_distinct(self):
        self.sync(issues=[{'number':1,'stage':'done'},{'number':2,'stage':'cancelled'},{'number':3,'stage':'unknown'}])
        self.assertEqual([x['stage'] for x in self.view()['issues']],['done','cancelled','unknown'])
        with self.assertRaisesRegex(w.Refused,'not_dispatchable'):self.reserve()

    def test_issue_projection_cannot_invent_working(self):
        with self.assertRaisesRegex(w.Refused,'invalid_issue_state'):
            self.sync(issues=[{'number':1,'stage':'build','disposition':'working'}])

    def test_concurrent_role_claims_have_one_winner(self):
        barrier=threading.Barrier(2)
        def attempt(i):
            barrier.wait()
            try:return w.reserve(self.state,self.repo,'build','worker-'+str(i),'v1',[1],source_event_id='race-'+str(i),now=1000)['id']
            except w.Refused:return None
        with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(attempt,[1,2]))
        self.assertEqual(sum(x is not None for x in results),1)
        self.assertEqual(len(self.view()['assignments']),1)

    def test_legacy_occupied_slots_count_against_capacity(self):
        w.observe_slot(self.state,'old-build','other/repo','build',self.gen,'blocked',bootstrap=True,
                       source_event_id=self.event(),now=1000)
        slot=self.view()['slots'][0]
        self.assertIsNone(slot['assignment_id']);self.assertEqual(slot['bootstrap'],1)
        self.assertEqual(slot['reason'],'started_before_assignment_tracking')
        with self.assertRaisesRegex(w.Refused,'global_slot_capacity'):self.reserve(max_slots=1)
        with self.assertRaisesRegex(w.Refused,'role_slot_capacity'):self.reserve(max_slots=9,max_per_role=1)
        self.assertEqual(len(self.view()['assignments']),0)

    def test_reserved_not_started_slots_also_count_against_capacity(self):
        self.reserve();self.sync(repo='other/repo')
        with self.assertRaisesRegex(w.Refused,'global_slot_capacity'):
            self.reserve(repo='other/repo',session='other-build',max_slots=1)

    def test_legacy_occupancy_requires_proof_to_clear(self):
        args=(self.state,'old-build','other/repo','build',self.gen)
        w.observe_slot(*args,'working',source_event_id=self.event(),bootstrap=True,now=1000)
        with self.assertRaisesRegex(w.Refused,'proof_required'):
            w.observe_slot(*args,'free',source_event_id=self.event(),now=1000)
        proof=dict(verified=True,released=True,generation=self.gen,kind='verified_release')
        w.observe_slot(*args,'free',source_event_id=self.event(),proof=proof,now=1000)
        self.assertEqual(self.view()['slots'][0]['state'],'free')

    def test_generation_replacement_never_changes_bound_work(self):
        a=self.bound()
        with self.assertRaisesRegex(w.Refused,'generation_changed'):
            self.change(a,'submitted',generation='$2|999|%2|20')
        self.assertEqual(self.view()['assignments'][0]['state'],'reserved')
        with self.assertRaisesRegex(w.Refused,'slot_has_assignment'):
            w.observe_slot(self.state,'repo-build',self.repo,'build',self.gen,'free',source_event_id=self.event(),now=1000)

    def test_partial_delivery_cannot_be_resubmitted_or_expire(self):
        a=self.bound();self.change(a,'uncertain',reason='literal_send_uncertain')
        self.now=999999
        with self.assertRaises(w.Refused):self.change(a,'submitted')
        self.assertEqual(self.view()['slots'][0]['state'],'blocked')
        self.assertEqual(self.view()['issues'][0]['disposition'],'stalled')

    def test_provider_handover_preserves_assignment_and_target(self):
        a=self.submitted();self.change(a,'blocked',reason='usage_limit')
        result=self.change(a,'migrate',proof=self.proof(a,'verified_provider_handover'))
        self.assertEqual(result['id'],a['id']);self.assertEqual(result['issue_numbers'],[1])
        self.assertIsNone(result['generation']);self.assertIsNone(result['provider']);self.assertEqual(result['attempt'],2)
        self.change(a,'bind',generation='$2|1000|%2|20',provider='claude')
        self.assertEqual(self.view()['slots'][0]['provider'],'claude')
        with self.assertRaisesRegex(w.Refused,'generation_changed'):self.change(a,'working')

    def test_prestart_provider_block_can_bind_when_available(self):
        a=self.reserve();self.change(a,'blocked',generation=None,reason='provider_unavailable')
        self.assertEqual(self.view()['slots'][0]['state'],'blocked')
        self.change(a,'bind',provider='claude')
        self.assertEqual(self.view()['slots'][0]['state'],'starting')

    def test_review_requires_exact_current_canonical_pr_link(self):
        self.sync(issues=[{'number':1,'stage':'review','prs':[self.pr]}])
        with self.assertRaisesRegex(w.Refused,'current_pr'):self.reserve(role='review',session='repo-review')
        with self.assertRaisesRegex(w.Refused,'revision_mismatch'):
            self.reserve(role='review',session='repo-review',pr=dict(self.pr,head='c'*40))
        a=self.reserve(role='review',session='repo-review',pr=self.pr)
        self.assertEqual(a['pr'],self.pr)

    def test_head_change_invalidates_pending_execution(self):
        self.sync(issues=[{'number':1,'stage':'review','prs':[self.pr]}])
        a=self.reserve(role='review',session='repo-review',pr=self.pr)
        self.now=1001;self.sync(revision='v2',observed_at=1001,issues=[{'number':1,'stage':'review','prs':[dict(self.pr,head='c'*40)]}])
        self.change(a,'bind',provider='codex')
        self.assertEqual(self.view()['slots'][0]['state'],'starting')
        with self.assertRaisesRegex(w.Refused,'pr_revision_changed'):self.change(a,'submitted')

    def test_multi_issue_pr_is_one_assignment_and_unlinked_pr_is_explicit(self):
        self.sync(issues=[{'number':n,'stage':'audit','prs':[self.pr]} for n in (1,2)])
        a=self.reserve(role='audit',session='repo-audit',numbers=[1,2],pr=self.pr)
        self.assertEqual(a['issue_numbers'],[1,2])
        self.sync(repo='other/repo',issues=[])
        b=self.reserve(repo='other/repo',role='review',session='other-review',numbers=[],pr=self.pr)
        self.assertEqual(b['issue_numbers'],[])

    def test_critical_hold_cannot_be_dispatched(self):
        self.sync(issues=[{'number':1,'stage':'audit','disposition':'critical','prs':[self.pr]}])
        with self.assertRaisesRegex(w.Refused,'not_dispatchable'):self.reserve(role='audit',session='repo-audit',pr=self.pr)

    def test_outbox_and_history_are_atomic_immutable_paginated(self):
        a=self.bound();self.change(a,'submitted')
        rows=w.history(self.state,limit=2);self.assertEqual(len(rows),2)
        following=w.history(self.state,after=rows[-1]['id']);self.assertTrue(all(x['id']>rows[-1]['id'] for x in following))
        w.ack_outbox(self.state,rows[-1]['id'],now=1000)
        self.assertEqual(w.history(self.state,pending_outbox=True),following)
        db=sqlite3.connect(self.state/'workflow.sqlite3')
        with self.assertRaisesRegex(sqlite3.IntegrityError,'append-only'):db.execute('DELETE FROM events')
        db.close()

    def test_failed_event_commit_rolls_back_assignment_slot_and_issue(self):
        before=self.view()
        with patch.object(w,'_event',side_effect=RuntimeError('simulated disk failure')):
            with self.assertRaises(RuntimeError):self.reserve()
        self.assertEqual(self.view(),before)
        self.assertEqual(len(w.history(self.state)),1)

    def test_corrupt_database_fails_closed_without_replacement(self):
        path=self.state/'workflow.sqlite3'
        for side in self.state.glob('workflow.sqlite3*'):side.unlink()
        path.write_bytes(b'corrupt private ledger')
        with self.assertRaises(sqlite3.DatabaseError):self.reserve()
        self.assertEqual(path.read_bytes(),b'corrupt private ledger')

    def test_database_permissions_and_uninitialized_read(self):
        self.assertEqual((self.state/'workflow.sqlite3').stat().st_mode&0o777,0o600)
        self.assertEqual(w.read_state(self.state/'absent')['status'],'uninitialized')
        self.assertFalse((self.state/'absent').exists())

    def test_same_facts_refresh_freshness_without_transition_history_noise(self):
        a=self.submitted();self.change(a,'working')
        before=len(w.history(self.state))
        self.now=1005
        result=self.sync(observed_at=1005)
        self.assertEqual(result['status'],'refreshed')
        self.assertEqual(len(w.history(self.state)),before)
        self.assertEqual(self.view()['repositories'][0]['observed_at'],1005)
        self.assertEqual(self.view()['issues'][0]['stage'],'build')
        self.assertEqual(self.view()['issues'][0]['disposition'],'working')

    def test_initialized_database_disappearance_never_reverts_to_legacy(self):
        self.assertTrue((self.state/'workflow.initialized').exists())
        for file in self.state.glob('workflow.sqlite3*'):file.unlink()
        with self.assertRaisesRegex(w.Refused,'database_missing'):self.view()
        with self.assertRaisesRegex(w.Refused,'database_missing'):self.reserve()
        with self.assertRaisesRegex(w.Refused,'database_missing'):w.history(self.state)
        self.assertFalse((self.state/'workflow.sqlite3').exists())

    def test_reserved_work_cannot_be_overwritten_by_free_slot_observation(self):
        a=self.reserve()
        with self.assertRaisesRegex(w.Refused,'slot_has_assignment'):
            w.observe_slot(self.state,'repo-build',self.repo,'build',None,'free',source_event_id=self.event(),now=1000)
        self.assertEqual(self.view()['slots'][0]['assignment_id'],a['id'])

    def test_repeated_refreshed_source_event_keeps_idempotence_contract(self):
        self.now=1001
        arguments=(self.state,self.repo,'v1',1001,[{'number':1,'stage':'queued','prs':[]}])
        a=w.reconcile(*arguments,source_event_id='refresh-once',now=1001)
        b=w.reconcile(*arguments,source_event_id='refresh-once',now=1002)
        self.assertEqual(a,b)
        with self.assertRaisesRegex(w.Refused,'source_event_conflict'):
            w.reconcile(self.state,self.repo,'v1',1001,[],source_event_id='refresh-once',now=1002)

    def test_idle_execution_retains_assignment_in_retiring_slot(self):
        a=self.submitted();self.change(a,'idle')
        self.assertEqual(self.view()['slots'][0]['state'],'retiring')
        self.assertEqual(self.view()['assignments'][0]['state'],'submitted')
        self.assertEqual(self.view()['issues'][0]['stage'],'build')

    def test_execution_thread_and_start_time_follow_each_exact_dispatch(self):
        execution={'run_id':'11111111-1111-4111-8111-111111111111','thread_id':'22222222-2222-4222-8222-222222222222','started_at':999}
        a=self.submitted();self.change(a,'working',execution=execution)
        self.assertEqual(self.view()['assignments'][0]['execution'],execution)
        self.assertEqual(self.view()['slots'][0]['execution'],execution)
        other=dict(execution,run_id='33333333-3333-4333-8333-333333333333')
        with self.assertRaisesRegex(w.Refused,'without_dispatch'):self.change(a,'working',execution=other)
        self.change(a,'idle',execution=execution)
        self.change(a,'submitted')
        self.assertIsNone(self.view()['assignments'][0]['execution'])
        self.change(a,'working',execution=other)
        self.assertEqual(self.view()['slots'][0]['execution'],other)
        self.assertTrue(any(e['payload'].get('after',{}).get('execution')==execution for e in w.history(self.state) if isinstance(e['payload'].get('after'),dict)))

    def test_execution_metadata_rejects_raw_fields_and_invalid_identity(self):
        a=self.submitted()
        for value in ({'run_id':'invalid','started_at':999},
                      {'run_id':'11111111-1111-4111-8111-111111111111','started_at':899},
                      {'run_id':'11111111-1111-4111-8111-111111111111','started_at':999,'raw_prompt':'PRIVATE'}):
            with self.assertRaisesRegex(w.Refused,'execution_identity'):self.change(a,'working',execution=value)

    def test_durable_send_intent_requires_proven_delivery_before_submission(self):
        a=self.bound();self.change(a,'uncertain',reason='dispatch_in_progress')
        self.assertEqual(self.view()['slots'][0]['state'],'blocked')
        with self.assertRaisesRegex(w.Refused,'delivery_proof'):self.change(a,'submitted')
        with self.assertRaises(w.Refused):self.change(a,'working')
        proof=dict(verified=True,kind='verified_delivery',assignment_id=a['id'],generation=self.gen)
        self.change(a,'submitted',proof=proof)
        self.assertEqual(self.view()['assignments'][0]['state'],'submitted')
        self.assertEqual(self.view()['issues'][0]['disposition'],'pending')
        self.assertEqual(self.view()['slots'][0]['state'],'starting')

    def test_only_proven_never_dispatched_reservation_can_cancel_without_generation(self):
        a=self.reserve()
        proof=dict(verified=True,released=True,assignment_id=a['id'],generation=None,kind='verified_no_dispatch')
        with self.assertRaisesRegex(w.Refused,'proof_required'):
            self.change(a,'retired',generation=None,proof=dict(proof,kind='verified_release'))
        self.change(a,'retired',generation=None,proof=proof)
        self.assertEqual(self.view()['slots'][0]['state'],'free')
        self.assertEqual(self.view()['issues'][0]['stage'],'build')

    def test_no_dispatch_proof_cannot_cancel_bound_assignment(self):
        a=self.bound()
        proof=dict(verified=True,released=True,assignment_id=a['id'],generation=None,kind='verified_no_dispatch')
        with self.assertRaises(w.Refused):self.change(a,'retired',generation=None,proof=proof)
        self.assertEqual(self.view()['slots'][0]['state'],'starting')

    def test_no_dispatch_cancellation_rejects_previously_submitted_migration(self):
        a=self.submitted();self.change(a,'blocked',reason='usage_limit')
        self.change(a,'migrate',proof=self.proof(a,'verified_provider_handover'))
        proof=dict(verified=True,released=True,assignment_id=a['id'],generation=None,kind='verified_no_dispatch')
        with self.assertRaisesRegex(w.Refused,'reservation_was_dispatched'):
            self.change(a,'retired',generation=None,proof=proof)
        self.assertEqual(self.view()['slots'][0]['state'],'reserved')
        self.assertIsNotNone(self.view()['assignments'][0]['submitted_at'])

    def test_unstarted_migration_proof_retires_only_slot_preserving_prior_work(self):
        a=self.submitted();before=self.view()['assignments'][0]['submitted_at']
        self.change(a,'blocked',reason='usage_limit');self.change(a,'migrate',proof=self.proof(a,'verified_provider_handover'))
        proof=dict(verified=True,released=True,assignment_id=a['id'],generation=None,kind='verified_unstarted_attempt')
        self.change(a,'retired',generation=None,proof=proof)
        v=self.view();self.assertEqual(v['slots'][0]['state'],'free')
        self.assertEqual(v['assignments'][0]['submitted_at'],before)
        self.assertEqual(v['assignments'][0]['issue_numbers'],[1]);self.assertEqual(v['issues'][0]['stage'],'build')

    def test_unstarted_migration_proof_rejects_initial_attempt(self):
        a=self.reserve();proof=dict(verified=True,released=True,assignment_id=a['id'],generation=None,kind='verified_unstarted_attempt')
        with self.assertRaisesRegex(w.Refused,'migration_required'):self.change(a,'retired',generation=None,proof=proof)
        self.assertEqual(self.view()['slots'][0]['state'],'reserved')

    def test_unstarted_migration_proof_cannot_retire_bound_current_attempt(self):
        a=self.submitted();self.change(a,'migrate',proof=self.proof(a,'verified_provider_handover'))
        current='$2|1000|%2|20';self.change(a,'bind',generation=current,provider='claude')
        self.change(a,'submitted',generation=current)
        proof=dict(verified=True,released=True,assignment_id=a['id'],generation=None,kind='verified_unstarted_attempt')
        with self.assertRaises(w.Refused):self.change(a,'retired',generation=None,proof=proof)
        with self.assertRaises(w.Refused):self.change(a,'retired',generation=current,proof=dict(proof,generation=current))
        self.assertEqual(self.view()['slots'][0]['state'],'starting')
