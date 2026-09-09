import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from forge import workflow
from forge.workflow_lifecycle import prepare, confirmed, reconcile


class WorkflowLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.generation = '$1|100|%2|10'
        self.proof = {'reason':'verified_handoff', 'evidence': {
            'repo':'Owner/App','role':'build','session_id':'$1','session_created':100,'pane_id':'%2','pane_pid':10}}

    def assignment(self):
        now = time.time()
        workflow.reconcile(self.state, 'owner/app', 'r1', now,
            [{'number':1,'stage':'build','disposition':'pending'}],source_event_id='observe')
        a = workflow.reserve(self.state,'owner/app','build','app-build','r1',[1],source_event_id='reserve')
        workflow.transition(self.state,a['id'],'bind',generation=self.generation,provider='codex',source_event_id='bind')
        workflow.transition(self.state,a['id'],'submitted',generation=self.generation,source_event_id='submit')
        workflow.transition(self.state,a['id'],'working',generation=self.generation,source_event_id='working')
        return a['id']

    def test_legacy_without_ledger_remains_unchanged(self):
        self.assertIsNone(prepare(self.state,'app-build',self.proof))
        self.assertEqual(confirmed(self.state,None),{'status':'legacy'})
        self.assertEqual(list(self.state.iterdir()),[])

    def test_prepared_intent_never_frees_worker_or_finishes_issue(self):
        self.assignment()
        ticket = prepare(self.state,'app-build',self.proof)
        self.assertEqual(reconcile(self.state),[])
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'working')
        self.assertEqual(confirmed(self.state,ticket)['status'],'applied')
        view = workflow.read_state(self.state)
        self.assertEqual(view['slots'][0]['state'],'free')
        self.assertEqual(view['assignments'][0]['state'],'retired')
        self.assertEqual(view['issues'][0]['stage'],'build')
        self.assertEqual(confirmed(self.state,ticket)['status'],'applied')
        self.assertEqual(len([e for e in workflow.history(self.state) if e['source_event_id'].startswith('release:')]),1)

    def test_published_rejection_survives_release_replay_and_counts_once(self):
        now=time.time();pr={'number':9,'head':'a'*40,'base':'b'*40}
        workflow.reconcile(self.state,'owner/app','r1',now,[dict(number=1,stage='review',prs=[pr])],source_event_id='facts')
        a=workflow.reserve(self.state,'owner/app','review','app-review','r1',[1],pr=pr,source_event_id='reserve')
        workflow.transition(self.state,a['id'],'bind',generation=self.generation,provider='codex',source_event_id='bind')
        workflow.transition(self.state,a['id'],'submitted',generation=self.generation,source_event_id='submit')
        self.proof['evidence'].update(role='review',assignment_id=a['id'],pr=9,assigned_head=pr['head'],assigned_base=pr['base'],
                                      review_outcome='fail',review_id='R1',review_at=time.time(),review_digest='c'*64)
        ticket=prepare(self.state,'app-review',self.proof)
        self.assertEqual(workflow.read_state(self.state)['issues'][0]['retry']['rejected_cycles'],0)
        with patch('forge.workflow.transition',side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(confirmed(self.state,ticket)['status'],'pending')
        self.assertEqual(workflow.read_state(self.state)['issues'][0]['retry']['rejected_cycles'],1)
        self.assertEqual(reconcile(self.state)[0]['status'],'applied')
        view=workflow.read_state(self.state)
        self.assertEqual(view['issues'][0]['retry']['rejected_cycles'],1)
        self.assertEqual(view['issues'][0]['stage'],'build')
        self.assertEqual(view['slots'][0]['state'],'free')
        self.assertEqual(len([e for e in workflow.history(self.state) if e['operation']=='issue_result']),1)

    def test_provider_migration_preserves_target_and_assignment(self):
        ident = self.assignment()
        ticket = prepare(self.state,'app-build',self.proof,action='migrate')
        self.assertEqual(confirmed(self.state,ticket)['status'],'applied')
        view = workflow.read_state(self.state)
        a = view['assignments'][0]
        self.assertEqual((a['id'],a['issue_numbers'],a['state'],a['generation'],a['provider'],a['attempt']),
                         (ident,[1],'reserved',None,None,2))
        self.assertEqual(view['slots'][0]['state'],'reserved')

    def test_failed_ledger_write_keeps_durable_confirmed_outbox(self):
        self.assignment()
        ticket = prepare(self.state,'app-build',self.proof)
        with patch('forge.workflow.transition',side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(confirmed(self.state,ticket)['status'],'pending')
        pending = self.state/'workflow-releases'/(ticket['id']+'.json')
        self.assertEqual(json.loads(pending.read_text())['phase'],'confirmed')
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'working')
        self.assertEqual(reconcile(self.state)[0]['status'],'applied')
        self.assertFalse(pending.exists())
        self.assertEqual(reconcile(self.state),[])
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'free')

    def test_preparation_read_errors_and_generation_mismatch_fail_closed(self):
        self.assignment()
        with patch('forge.workflow.read_state',side_effect=sqlite3.DatabaseError('corrupt')):
            with self.assertRaisesRegex(ValueError,'workflow_release_prepare_unavailable'):
                prepare(self.state,'app-build',self.proof)
        self.proof['evidence']['pane_pid'] = 99
        with self.assertRaisesRegex(ValueError,'generation_mismatch'):
            prepare(self.state,'app-build',self.proof)
        self.assertFalse((self.state/'workflow-releases').exists())

    def test_unknown_legacy_slot_frees_only_stopped_generation(self):
        workflow.observe_slot(self.state,'app-build','owner/app','build',self.generation,'working',
            source_event_id='legacy',bootstrap=True)
        ticket = prepare(self.state,'app-build',self.proof)
        self.assertEqual(confirmed(self.state,ticket)['status'],'applied')
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'free')
        self.assertEqual(workflow.read_state(self.state)['assignments'],[])

    def test_initialized_missing_slot_can_record_verified_free(self):
        workflow.reconcile(self.state,'owner/app','r1',time.time(),[],source_event_id='observe')
        ticket=prepare(self.state,'app-build',self.proof)
        self.assertEqual(confirmed(self.state,ticket)['status'],'applied')
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'free')

    def test_failed_confirmation_write_never_replays_prepared_intent(self):
        self.assignment()
        ticket=prepare(self.state,'app-build',self.proof)
        with patch('forge.workflow_lifecycle._write',side_effect=OSError('disk unavailable')):
            self.assertEqual(confirmed(self.state,ticket)['status'],'pending')
        self.assertEqual(reconcile(self.state),[])
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'working')

    def test_release_failures_preserve_slot_and_success_survives_ledger_failure(self):
        from forge.handoff import release
        from types import SimpleNamespace
        from tests.host_isolation import deny_host_tmux
        deny_host_tmux(self)
        self.assignment()
        decision = {'eligible':True, **self.proof}
        calls=[]
        fail_kill=[True]
        def runner(args, **kwargs):
            calls.append(args)
            self.assertEqual(args,['tmux','kill-session','-t','$1'])
            return SimpleNamespace(returncode=1 if fail_kill[0] else 0, stdout='', stderr='')
        with patch('forge.human_wait._locked',return_value=True), \
             patch('forge.handoff.check',return_value=decision), \
             patch('forge.launch_budget.refund',return_value={'status':'not_applicable'}):
            with patch('forge.workflow.read_state',side_effect=sqlite3.DatabaseError('unreadable')):
                result=release(None,self.state,'app-build',run=runner)
            self.assertFalse(result['released'])
            self.assertEqual(calls,[])
            result=release(None,self.state,'app-build',run=runner)
            self.assertFalse(result['released'])
            self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'working')
            self.assertEqual(reconcile(self.state),[])
            fail_kill[0]=False
            with patch('forge.workflow.transition',side_effect=sqlite3.OperationalError('locked')):
                result=release(None,self.state,'app-build',run=runner)
            self.assertTrue(result['released'])
            self.assertEqual(result['workflow']['status'],'pending')
        self.assertEqual(reconcile(self.state)[0]['status'],'applied')
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'free')

    def test_delayed_legacy_receipt_never_clears_new_generation(self):
        workflow.observe_slot(self.state,'app-build','owner/app','build',self.generation,'working',
            source_event_id='legacy',bootstrap=True)
        ticket=prepare(self.state,'app-build',self.proof)
        with patch('forge.workflow.observe_slot',side_effect=sqlite3.OperationalError('locked')):
            self.assertEqual(confirmed(self.state,ticket)['status'],'pending')
        proof={'verified':True,'released':True,'kind':'verified_release','generation':self.generation}
        workflow.observe_slot(self.state,'app-build','owner/app','build',self.generation,'free',
            source_event_id='verified-other-observer',proof=proof)
        workflow.observe_slot(self.state,'app-build','owner/app','build','$2|200|%3|20','working',
            source_event_id='replacement',bootstrap=True)
        self.assertEqual(reconcile(self.state)[0]['status'],'pending')
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['generation'],'$2|200|%3|20')

    def test_live_mixed_case_repo_matches_normalized_slot_but_not_other_repo(self):
        workflow.observe_slot(self.state,'Map-review','XRPL-Commons/Map','review',self.generation,'retiring',
            source_event_id='map-legacy',bootstrap=True)
        proof={'reason':'verified_review_complete','evidence':{
            **self.proof['evidence'],'repo':'XRPL-Commons/Map','role':'review'}}
        ticket=prepare(self.state,'Map-review',proof)
        self.assertEqual(ticket['repo'],'xrpl-commons/map')
        self.assertEqual(confirmed(self.state,ticket)['status'],'applied')
        self.assertEqual(workflow.read_state(self.state)['slots'][0]['state'],'free')
        proof['evidence']['repo']='Other-Org/Map'
        with self.assertRaisesRegex(ValueError,'workflow_release_repository_mismatch'):
            prepare(self.state,'Map-review',proof)

    def test_unbound_startup_cancellation_requires_successful_verified_shell_release(self):
        from forge.startup import release
        from types import SimpleNamespace
        now=time.time()
        workflow.reconcile(self.state,'owner/app','r1',now,[{'number':1,'stage':'build'}],source_event_id='observe')
        a=workflow.reserve(self.state,'owner/app','build','app-build','r1',[1],source_event_id='reserve')
        now=time.time()  # Model shell creation after the durable reservation.
        decision={'eligible':True,'reason':'verified_interrupted_startup','evidence':{
            **self.proof['evidence'],'session_created':int(now),'creation_event':1}}
        with self.assertRaisesRegex(ValueError,'assignment_unbound'):
            prepare(self.state,'app-build',decision)
        failure=[True]
        def runner(args,**kwargs):
            self.assertEqual(args,['tmux','kill-session','-t','$1'])
            return SimpleNamespace(returncode=int(failure[0]),stdout='',stderr='')
        with patch('forge.startup._locked',return_value=True), patch('forge.startup.check',return_value=decision):
            result=release(None,self.state,'app-build',run=runner)
            self.assertFalse(result['released'])
            self.assertEqual(workflow.read_state(self.state)['assignments'][0]['state'],'reserved')
            failure[0]=False
            result=release(None,self.state,'app-build',run=runner)
            self.assertTrue(result['released'])
            self.assertEqual(result['workflow']['status'],'applied')
        view=workflow.read_state(self.state)
        self.assertEqual(view['assignments'][0]['state'],'retired')
        self.assertIsNone(view['assignments'][0]['generation'])
        self.assertEqual(view['slots'][0]['state'],'free')
        event=[e for e in workflow.history(self.state) if e['source_event_id'].startswith('release:')][0]
        self.assertEqual(event['payload']['proof']['kind'],'verified_no_dispatch')
        self.assertIsNone(event['payload']['proof']['generation'])
        self.assertEqual(event['payload']['proof']['released_generation'],f'$1|{int(now)}|%2|10')

    def test_unbound_startup_never_cancels_newer_or_unverified_reservation(self):
        now=time.time()
        workflow.reconcile(self.state,'owner/app','r1',now,[{'number':1,'stage':'build'}],source_event_id='observe')
        workflow.reserve(self.state,'owner/app','build','app-build','r1',[1],source_event_id='reserve')
        decision={'reason':'verified_interrupted_startup','evidence':{
            **self.proof['evidence'],'session_created':int(now)-60,'creation_event':1}}
        with self.assertRaisesRegex(ValueError,'assignment_unbound'):
            prepare(self.state,'app-build',decision,unbound_startup=True)
        decision['evidence']['session_created']=int(now)
        decision['evidence'].pop('creation_event')
        with self.assertRaisesRegex(ValueError,'assignment_unbound'):
            prepare(self.state,'app-build',decision,unbound_startup=True)
        decision['evidence']['creation_event']=1
        decision['reason']='verified_handoff'
        with self.assertRaisesRegex(ValueError,'assignment_unbound'):
            prepare(self.state,'app-build',decision,unbound_startup=True)

    def test_migrated_previously_submitted_assignment_is_not_undispatched(self):
        self.assignment()
        ticket=prepare(self.state,'app-build',self.proof,action='migrate')
        self.assertEqual(confirmed(self.state,ticket)['status'],'applied')
        self.assertIsNotNone(workflow.read_state(self.state)['assignments'][0]['submitted_at'])
        proof={'reason':'verified_interrupted_startup','evidence':{
            **self.proof['evidence'],'session_id':'$2','session_created':int(time.time()),'creation_event':2}}
        with self.assertRaisesRegex(ValueError,'assignment_unbound'):
            prepare(self.state,'app-build',proof,unbound_startup=True)
        self.assertEqual(workflow.read_state(self.state)['assignments'][0]['state'],'reserved')
