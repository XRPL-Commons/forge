import hashlib
import unittest

from forge import workflow as engine, workflow_definition as policy
from tests.test_workflow_failure_cycle import WorkflowFailureCycleTests as Fixture


class RetryBudgetTests(Fixture):
    def published(self, attempt, outcome):
        a,generation=attempt
        self.now+=.01
        evidence=dict(verified=True,assignment_id=a['id'],generation=generation,role=a['role'],outcome=outcome,
                      pr=a['pr'],result_id='result-'+a['id'],published_at=self.now,digest=hashlib.sha256(a['id'].encode()).hexdigest())
        engine.record_result(self.state,a['id'],evidence,source_event_id='result:'+a['id'])
        return evidence

    def reject_observed(self, role='review'):
        builder=self.start('build')
        self.head=hashlib.sha1(builder[0]['id'].encode()).hexdigest()
        self.snapshot('review');self.release(builder)
        reviewer=self.start('review')
        if role=='audit':
            self.snapshot('audit');self.release(reviewer)
            attempt=self.start('audit')
        else:attempt=reviewer
        self.snapshot('build');self.release(attempt)
        from forge.workflow_results import before_rework
        import json
        pr=json.loads(self.github(['gh','pr','view']).stdout)
        before_rework(self.state,self.repo,pr,'forge-bot')
        return attempt

    def test_definition_full_requested_path(self):
        stage='queued';path=[stage];count=0
        for event in ['dispatch','pass','fail','pass','pass','fail','pass','pass','pass','completed']:
            if event=='fail':count+=1
            stage=policy.next_stage(stage,event,rejected_cycles=count);path.append(stage)
        self.assertEqual(path,['queued','build','review','build','review','audit','build','review','audit','merge','done'])
        with self.assertRaises(KeyError):policy.next_stage('build','merge')

    def test_three_rejected_cycles_across_roles_latch_failure_and_block_dispatch(self):
        for role,count in [('review',1),('audit',2),('review',3)]:
            self.reject_observed(role)
            issue=engine.read_state(self.state)['issues'][0]
            self.assertEqual(issue['retry']['rejected_cycles'],count)
        self.assert_issue('failed','stalled')
        for stage in ('build','review','audit','queued'):
            self.snapshot(stage)
            self.assert_issue('failed','stalled')
            with self.assertRaises(engine.Refused):self.start('build')
        self.assertEqual(sum(a['state']!='retired' for a in engine.read_state(self.state)['assignments']),0)

    def test_repeated_observation_and_receipt_do_not_consume_extra_cycles(self):
        self.reject_observed()
        from forge.workflow_results import before_rework
        import json
        pr=json.loads(self.github(['gh','pr','view']).stdout)
        for _ in range(5):
            before_rework(self.state,self.repo,pr,'forge-bot');self.snapshot('build')
        self.assertEqual(engine.read_state(self.state)['issues'][0]['retry']['rejected_cycles'],1)

    def test_worker_release_and_provider_migration_are_not_rejected_cycles(self):
        builder=self.start('build');a,generation=builder
        engine.transition(self.state,a['id'],'migrate',source_event_id=self.event(),generation=generation,
                          proof=dict(verified=True,released=True,assignment_id=a['id'],generation=generation,kind='verified_provider_handover'))
        self.assertEqual(engine.read_state(self.state)['issues'][0]['retry']['rejected_cycles'],0)

    def test_wrong_revision_or_generation_and_conflicting_result_refused(self):
        self.snapshot('review');reviewer=self.start('review')
        evidence=self.published(reviewer,'fail')
        for field,value in [('generation','$99|100|%99|10'),('pr',{**evidence['pr'],'head':'f'*40})]:
            with self.assertRaises(engine.Refused):
                engine.record_result(self.state,reviewer[0]['id'],{**evidence,field:value},source_event_id=self.event())
        with self.assertRaisesRegex(engine.Refused,'assignment_result_conflict'):
            engine.record_result(self.state,reviewer[0]['id'],{**evidence,'outcome':'pass'},source_event_id=self.event())

    def test_explicit_retry_preserves_history_and_requires_fresh_eligibility(self):
        for _ in range(3):self.reject_observed()
        before=len(engine.history(self.state,limit=1000));self.now+=1
        engine.retry_failed(self.state,self.repo,17,reason='Approved repair plan',source_event_id=self.event())
        self.assert_issue('build','stalled')
        self.assertEqual(engine.read_state(self.state)['issues'][0]['retry']['rejected_cycles'],0)
        self.snapshot('build')
        self.assert_issue('build')
        self.assertGreater(len(engine.history(self.state,limit=1000)),before)
        self.start('build')

    def test_hold_survives_verified_rejection(self):
        self.snapshot('review');reviewer=self.start('review')
        self.now+=1
        engine.reconcile(self.state,self.repo,'held',self.now,[dict(number=17,stage='review',disposition='stalled',reason='hold',prs=[reviewer[0]['pr']])],source_event_id=self.event())
        self.published(reviewer,'fail')
        issue=engine.read_state(self.state)['issues'][0]
        self.assertEqual((issue['stage'],issue['disposition'],issue['reason']),('build','stalled','hold'))

    def test_two_rejections_of_same_build_count_once(self):
        builder=self.start('build');self.snapshot('review');self.release(builder)
        reviewer=self.start('review');self.published(reviewer,'fail');self.release(reviewer)
        self.snapshot('review');second=self.start('review');self.published(second,'fail');self.release(second)
        self.assertEqual(engine.read_state(self.state)['issues'][0]['retry']['rejected_cycles'],1)

    def test_audit_cannot_skip_review_by_changing_labels(self):
        self.snapshot('review');reviewer=self.start('review')
        self.published(reviewer,'fail');self.release(reviewer)
        self.snapshot('audit')
        with self.assertRaises(engine.Refused):self.start('audit')
        revision=engine.read_state(self.state)['repositories'][0]['revision']
        with self.assertRaisesRegex(engine.Refused,'verified_review_pass_required'):
            engine.reserve(self.state,self.repo,'audit','Fixture-audit',revision,[17],pr=reviewer[0]['pr'],source_event_id=self.event())

    def test_result_transaction_rolls_back_counter_and_event_together(self):
        from unittest.mock import patch
        self.snapshot('review');reviewer=self.start('review')
        with patch('forge.workflow._event',side_effect=RuntimeError('disk error')):
            with self.assertRaises(RuntimeError):self.published(reviewer,'fail')
        issue=engine.read_state(self.state)['issues'][0]
        self.assertEqual(issue['retry']['rejected_cycles'],0)
        self.assertEqual(issue['stage'],'review')

    def test_late_pre_retry_result_cannot_fail_new_budget(self):
        for _ in range(3):self.reject_observed()
        self.now+=1;engine.retry_failed(self.state,self.repo,17,reason='Repair approved',source_event_id=self.event())
        from forge.workflow_results import before_rework
        import json
        before_rework(self.state,self.repo,json.loads(self.github(['gh','pr','view']).stdout),'forge-bot')
        self.assertEqual(engine.read_state(self.state)['issues'][0]['retry']['rejected_cycles'],0)


if __name__=='__main__':unittest.main()
