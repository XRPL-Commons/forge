import json
import tempfile
import unittest
from pathlib import Path

from forge.issue_flow import read_flow
from forge.observability import connect


class IssueFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.rows = [{'repo': 'owner/app', 'short': 'app', 'queue_status': 'ok',
                      'work': {}, 'schedule': {}, 'build': {'up': True, 'busy': False},
                      'review': {'up': True, 'busy': True}}]

    def save(self, number, kind='issue', **values):
        data = {'number': number, 'title': f'Title {number}', 'state': 'open',
                'stage': 'pending', 'labels': [], **values}
        db = connect(self.state, write=True)
        with db:
            db.execute('INSERT OR REPLACE INTO items VALUES(?,?,?,?,?,?,?,?)',
                       ('owner/app', number, kind, data['stage'], '2026-01-01', '2026-01-02', None, json.dumps(data)))
        db.close()

    def pr(self, number, issue=1, **values):
        self.save(number, 'pr', linked_issues=[{'repo': 'owner/app', 'number': issue, 'source': 'closing_keyword'}], **values)

    def test_multiple_prs_count_issue_once_and_earliest_unfinished(self):
        self.save(1, labels=['wip'])
        self.pr(10, stage='review')
        self.pr(11, stage='audit')
        flow = read_flow(self.state, self.rows)
        self.assertEqual(flow['counts']['in_flight'], 1)
        self.assertEqual(len(flow['issues']), 1)
        self.assertEqual(flow['issues'][0]['step'], 2)
        self.assertEqual(flow['issues'][0]['pr_numbers'], [10, 11])
        self.assertTrue(flow['issues'][0]['blockers'])

    def test_merged_pr_does_not_complete_open_issue(self):
        self.save(1)
        self.pr(10, state='closed', stage='completed', merged_at='2026-01-01')
        flow = read_flow(self.state, self.rows)
        self.assertEqual(flow['counts']['done'], 0)
        self.assertEqual(flow['issues'][0]['state'], 'unknown')
        self.save(1, state='closed', state_reason='completed')
        self.assertEqual(read_flow(self.state, self.rows)['counts']['done'], 1)

    def test_cross_repository_reference_does_not_alias(self):
        self.save(1)
        self.save(10, 'pr', stage='audit', linked_issues=[{'repo': 'other/app', 'number': 1}])
        flow = read_flow(self.state, self.rows)
        self.assertEqual(flow['counts']['in_flight'], 0)
        self.assertEqual(flow['backlog'][0]['pr_numbers'], [])
        self.assertEqual(len(flow['unlinked_prs']), 1)

    def test_snapshot_overrides_stale_pr_stage(self):
        self.save(1, labels=['wip'])
        self.pr(10, stage='audit')
        self.rows[0]['work'] = {'review': [10]}
        item = read_flow(self.state, self.rows)['issues'][0]
        self.assertEqual(item['state'], 'review')
        self.assertTrue(item['workers'][0]['active'])
        self.assertIn('unverified', item['workers'][0]['basis'])

    def test_hold_preserves_stage_but_not_running_worker(self):
        self.save(1, labels=['hold'])
        self.pr(10, stage='audit')
        item = read_flow(self.state, self.rows)['issues'][0]
        self.assertEqual(item['state'], 'held')
        self.assertEqual(item['step'], 3)
        self.assertEqual(item['workers'], [])
        self.assertTrue(item['blockers'])

    def test_missing_database_claim_is_not_active_work(self):
        self.rows[0]['work'] = {'wip': [5]}
        self.rows[0]['schedule'] = {'active': [5]}
        flow = read_flow(self.state, self.rows)
        self.assertEqual(flow['status'], 'unavailable')
        self.assertEqual(flow['counts']['in_flight'], 1)
        self.assertFalse(flow['issues'][0]['workers'][0]['active'])
        self.assertFalse((self.state / 'observability.sqlite3').exists())

    def test_stale_snapshot_cannot_create_claims(self):
        self.rows[0].update(queue_status='stale', work={'wip': [5]})
        self.assertEqual(read_flow(self.state, self.rows)['issues'], [])

    def test_dependency_blocked_ready_issue_has_reason(self):
        self.save(1, labels=['ready'])
        self.rows[0]['schedule'] = {'blocked': {'1': ['triage_pending']}}
        item = read_flow(self.state, self.rows)['issues'][0]
        self.assertEqual(item['state'], 'held')
        self.assertEqual(item['blockers'], ['triage pending'])

    def test_active_claim_admission_exclusion_is_not_a_blocker(self):
        self.save(1, labels=['wip'])
        self.rows[0]['work'] = {'wip': [1]}
        self.rows[0]['schedule'] = {'blocked': {'1': ['active_claim', 'not_ready']}}
        flow = read_flow(self.state, self.rows)
        self.assertEqual(flow['issues'][0]['state'], 'build')
        self.assertEqual(flow['issues'][0]['blockers'], [])
        self.assertEqual(flow['counts']['blocked'], 0)

    def test_canonical_reference_uses_url_repository_and_rejects_disagreement(self):
        self.save(1)
        references = [
            {'number': 1, 'url': 'https://github.com/other/app/issues/1'},
            {'number': 1, 'repository': {'nameWithOwner': 'other/app'}},
            {'number': 1},
            {'number': 1, 'repository': {'nameWithOwner': 'owner/app'}, 'url': 'https://github.com/other/app/issues/1'},
            {'number': 1, 'url': 'https://github.com/owner/app/issues/2'},
        ]
        for reference in references:
            with self.subTest(reference=reference):
                self.save(10, 'pr', stage='review', raw_pr_summary={'closingIssuesReferences': [reference]})
                flow = read_flow(self.state, self.rows)
                self.assertEqual(flow['counts']['in_flight'], 0)
                self.assertEqual(len(flow['unlinked_prs']), 1)
        self.save(10, 'pr', stage='review', raw_pr_summary={'closingIssuesReferences': [{'number': 1, 'url': 'https://github.com/owner/app/issues/1'}]})
        self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['pr_numbers'], [10])

    def test_closed_pr_review_requirement_is_not_a_blocker(self):
        self.save(1, labels=['wip'])
        self.pr(10, state='closed', merged_at='2026-01-01', stage='completed', review_decision='REVIEW_REQUIRED', human_approval_required=True)
        self.pr(11, stage='build')
        item = read_flow(self.state, self.rows)['issues'][0]
        self.assertEqual(item['state'], 'build')
        self.assertEqual(item['blockers'], [])

    def test_issue_projection_excludes_raw_content_and_evidence(self):
        self.save(1, labels=['wip'], body='large raw content', raw={'body': 'raw'}, evidence=[{'body': 'review'}], summary='summary')
        item = read_flow(self.state, self.rows)['issues'][0]
        self.assertEqual(item['title'], 'Title 1')
        self.assertEqual(item['observed_at'], '2026-01-02')
        for key in ('body', 'raw', 'evidence', 'summary', 'labels'):
            self.assertNotIn(key, item)

    def audited_pr(self, **overrides):
        head = 'a' * 40
        return {'stage': 'audit', 'labels': ['critical', 'ready-for-audit'],
                'review_decision': 'REVIEW_REQUIRED', 'summary_status': 'ok',
                'head_sha': head, 'summary_head_sha': head,
                'evidence': [{'kind': 'audit', 'created_at': '2026-01-02', 'commit_id': head,
                              'body': '## Security audit — PASS · Criticality: CRITICAL\nAwaiting human approval.',
                              'url': 'https://github.com/owner/app/pull/10#review'}], **overrides}

    def test_current_audited_critical_pr_exposes_action_without_drawer(self):
        self.save(1, labels=['wip'])
        self.pr(10, **self.audited_pr())
        item = read_flow(self.state, self.rows)['issues'][0]
        self.assertEqual((item['state'], item['step']), ('held', 3))
        self.assertEqual(item['actions'][0]['kind'], 'human_approval')
        self.assertEqual(item['actions'][0]['link'], 'https://github.com/owner/app/pull/10')
        self.assertIn('authorized human', item['actions'][0]['next'])

    def test_critical_label_and_review_required_do_not_prove_audit_pass(self):
        self.save(1)
        for change in [{'evidence': []}, {'summary_head_sha': 'b' * 40},
                       {'base_sha': 'c' * 40}, {'summary_status': 'unavailable'},
                       {'evidence': [{'kind': 'audit', 'commit_id': 'b' * 40,
                                      'body': 'Security audit PASS Criticality: CRITICAL awaiting human approval'}]}]:
            with self.subTest(change=change):
                self.pr(10, **self.audited_pr(**change))
                self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['actions'], [])

    def test_later_failed_audit_supersedes_earlier_pass(self):
        self.save(1)
        data = self.audited_pr()
        data['evidence'].append({'kind': 'audit', 'created_at': '2026-01-03', 'commit_id': 'a' * 40,
                                 'body': 'Security audit FAIL Criticality: CRITICAL'})
        self.pr(10, **data)
        self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['actions'], [])

    def test_pr_inventory_closing_body_links_issue_before_evidence_enrichment(self):
        self.save(799, labels=['wip'])
        self.save(802, 'pr', stage='review', body='Closes #799\n\nFix authentication.',
                  linked_issues=[], evidence_status='pending')
        self.rows[0]['work'] = {'wip': [799], 'review': [802]}
        flow = read_flow(self.state, self.rows)
        self.assertEqual(flow['unlinked_prs'], [])
        item = flow['issues'][0]
        self.assertEqual((item['number'], item['state'], item['pr_numbers']), (799, 'review', [802]))
        self.assertIn('PR #802: closing_keyword_inventory', item['basis'])

    def test_inventory_reference_keeps_cross_repository_identity(self):
        self.save(1, labels=['wip'])
        for body in ['Fixes other/app#1', 'Resolves https://github.com/other/app/issues/1']:
            with self.subTest(body=body):
                self.save(10, 'pr', stage='review', body=body)
                flow = read_flow(self.state, self.rows)
                self.assertEqual(flow['issues'][0]['pr_numbers'], [])
                self.assertEqual(len(flow['unlinked_prs']), 1)
        for body in ['Fixes owner/app#1', 'Resolves https://github.com/owner/app/issues/1']:
            with self.subTest(body=body):
                self.save(10, 'pr', stage='review', body=body)
                self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['pr_numbers'], [10])

    def test_code_samples_and_discussion_are_not_issue_links(self):
        self.save(1, labels=['wip'])
        for body in ['See #1', '`Closes #1`', '```\nCloses #1\n```', '    Closes #1', '> Closes #1', '~~~md\nCloses #1\n~~~']:
            with self.subTest(body=body):
                self.save(10, 'pr', stage='review', body=body)
                self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['pr_numbers'], [])

    def test_current_inventory_body_supersedes_stale_collected_text_link(self):
        self.save(1, labels=['wip'])
        self.save(2, labels=['wip'])
        self.pr(10, stage='review', body='Closes #2')
        flow = read_flow(self.state, self.rows)
        items = {i['number']: i for i in flow['issues']}
        self.assertEqual(items[1]['pr_numbers'], [])
        self.assertEqual(items[2]['pr_numbers'], [10])

    def test_missing_pr_inventory_does_not_guess_link_from_single_claim(self):
        self.save(1, labels=['wip'])
        self.rows[0]['work'] = {'wip': [1], 'review': [10]}
        item = read_flow(self.state, self.rows)['issues'][0]
        self.assertEqual(item['state'], 'build')
        self.assertEqual(item['pr_numbers'], [])

    def test_explicit_critical_policy_wait_with_no_github_review_rule(self):
        self.save(1)
        for decision in ('UNKNOWN', '', None):
            with self.subTest(decision=decision):
                self.pr(10, **self.audited_pr(review_decision=decision))
                item = read_flow(self.state, self.rows)['issues'][0]
                self.assertEqual(item['attention_state'], 'CRITICAL')
                self.assertIn('Forge critical-work human approval policy', item['actions'][0]['basis'])
        data = self.audited_pr()
        data['evidence'][0]['body'] = '## Auditor: PASS — Criticality: CRITICAL — awaiting human approval'
        self.pr(10, **data)
        self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['attention_state'], 'CRITICAL')

    def stall_history(self, queue=(10,), pause=True):
        from forge.activity import record
        record(self.state, 'pipeline_observed', 'app-audit', repo='owner/app', role='audit',
               metadata={'queued': list(queue), 'snapshot_status': 'ok'}, now=1000)
        if pause:
            record(self.state, 'paused', 'app-audit', repo='owner/app', role='audit',
                   metadata={'reason': 'no_progress'}, now=1100)
        self.rows[0]['audit'] = {'up': True, 'busy': False}
        self.rows[0]['work'] = {'audit': [10]}
        return {'sessions': {'app-audit': {'reason': 'no_progress', 'until': 4700}}}

    def test_stalled_audit_requires_pause_receipt_bound_to_current_issue_queue(self):
        self.save(1, labels=['wip'])
        self.pr(10, stage='audit')
        health = self.stall_history()
        item = read_flow(self.state, self.rows, health, now=1200)['issues'][0]
        self.assertEqual((item['state'], item['step'], item['attention_state']), ('audit', 3, 'STALLED'))
        self.assertEqual(item['actions'][0]['kind'], 'stalled_audit')
        self.assertEqual(item['workers'], [])
        self.assertFalse(item['on_hold'])
        self.assertIsNone(read_flow(self.state, self.rows, health, now=4800)['issues'][0]['attention_state'])

    def test_new_audit_queue_never_inherits_previous_stall(self):
        self.save(1)
        self.pr(10, stage='audit')
        health = self.stall_history(queue=(9,))
        self.assertIsNone(read_flow(self.state, self.rows, health, now=1200)['issues'][0]['attention_state'])

    def test_cooldown_without_explicit_pause_receipt_stays_session_level(self):
        self.save(1)
        self.pr(10, stage='audit')
        health = self.stall_history(pause=False)
        self.assertIsNone(read_flow(self.state, self.rows, health, now=1200)['issues'][0]['attention_state'])

    def test_replacement_session_does_not_inherit_stall(self):
        from forge.activity import record
        self.save(1)
        self.pr(10, stage='audit')
        health = self.stall_history()
        record(self.state, 'session_created', 'app-audit', repo='owner/app', role='audit', now=1150)
        self.assertIsNone(read_flow(self.state, self.rows, health, now=1200)['issues'][0]['attention_state'])

    def test_fresh_snapshot_pr_inventory_resolves_collector_gap(self):
        self.save(16, labels=['wip'])
        self.rows[0].update(observed_at=1200, work={'wip': [16], 'review': [22]},
            item_details={'22': {'number': 22, 'kind': 'pr', 'state': 'open',
                               'title': 'Implementation', 'body': 'Closes #16',
                               'labels': ['ready-for-review'], 'updated_at': '2026-09-08T00:00:00Z'}})
        flow = read_flow(self.state, self.rows)
        self.assertEqual(flow['unlinked_prs'], [])
        item = flow['issues'][0]
        self.assertEqual((item['number'], item['state'], item['pr_numbers']), (16, 'review', [22]))
        self.assertIn('PR #22: fresh snapshot inventory', item['basis'])
        db = connect(self.state)
        self.assertIsNone(db.execute('SELECT 1 FROM items WHERE number=22').fetchone())
        db.close()

    def test_snapshot_pr_without_explicit_reference_stays_unlinked(self):
        self.save(16, labels=['wip'])
        self.rows[0].update(work={'wip': [16], 'review': [22]},
            item_details={'22': {'number': 22, 'kind': 'pr', 'state': 'open',
                                'title': 'Similar title', 'body': 'See #16'}})
        flow = read_flow(self.state, self.rows)
        self.assertEqual(flow['issues'][0]['pr_numbers'], [])
        self.assertEqual(flow['unlinked_prs'][0]['number'], 22)

    def test_snapshot_inventory_rejects_stale_or_mismatched_identity(self):
        self.save(16, labels=['wip'])
        self.rows[0].update(queue_status='stale', work={'review': [22]},
            item_details={'22': {'number': 22, 'kind': 'pr', 'state': 'open', 'body': 'Closes #16'}})
        self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['pr_numbers'], [])
        self.rows[0]['queue_status'] = 'ok'
        self.rows[0]['item_details']['22']['number'] = 23
        self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['pr_numbers'], [])

    def test_fresh_snapshot_issue_title_and_observation_are_preserved(self):
        self.rows[0].update(observed_at=1200, work={'wip': [16]},
            item_details={'16': {'number': 16, 'kind': 'issue', 'state': 'open',
                               'title': 'Known new issue', 'body': '', 'labels': ['wip']}})
        item = read_flow(self.state, self.rows)['issues'][0]
        self.assertEqual(item['title'], 'Known new issue')
        self.assertEqual(item['observed_at'], 1200)
        self.assertEqual(item['basis'][0], 'fresh snapshot inventory')

    def wait_receipt(self, eligible=True, at=1200, **candidate_changes):
        import hashlib
        path = self.state / 'human-wait-checks'
        path.mkdir(exist_ok=True)
        candidate = {'pr': 10, 'head': 'a' * 40, 'base': 'b' * 40, 'issues': [1],
                     'audit_url': 'https://github.com/owner/app/pull/10#pullrequestreview-7', **candidate_changes}
        (path / (hashlib.sha256(b'owner/app').hexdigest() + '.json')).write_text(json.dumps({
            'repo': 'owner/app', 'eligible': eligible, 'observed_at': at,
            'reason': 'verified_awaiting_human_approval' if eligible else 'approval_requires_recheck',
            'evidence': {'repo': 'owner/app', 'candidates': [candidate]}}))
        self.rows[0].update(observed_at=1200, work={'audit': [10]}, item_details={
            '10': {'number': 10, 'kind': 'pr', 'state': 'open', 'body': 'Closes #1',
                   'labels': ['critical', 'ready-for-audit']}})

    def test_fresh_verified_runtime_receipt_bypasses_slow_audit_collection(self):
        self.save(1, labels=['wip'])
        self.pr(10, stage='audit', evidence_status='pending')
        self.wait_receipt()
        item = read_flow(self.state, self.rows, now=1250)['issues'][0]
        self.assertEqual(item['attention_state'], 'CRITICAL')
        self.assertIn('Fresh local human-wait check', item['actions'][0]['basis'])
        self.assertEqual(item['actions'][0]['head_sha'], 'a' * 40)

    def test_failed_runtime_check_invalidates_older_collected_approval_wait(self):
        self.save(1)
        self.pr(10, **self.audited_pr())
        self.wait_receipt(eligible=False)
        item = read_flow(self.state, self.rows, now=1250)['issues'][0]
        self.assertIsNone(item['attention_state'])
        self.assertEqual(item['actions'], [])

    def test_wait_receipt_requires_fresh_matching_queue_and_critical_labels(self):
        self.save(1)
        self.pr(10, stage='audit')
        self.wait_receipt()
        self.assertIsNone(read_flow(self.state, self.rows, now=1600)['issues'][0]['attention_state'])
        self.rows[0]['work']['audit'] = [10, 11]
        self.assertIsNone(read_flow(self.state, self.rows, now=1250)['issues'][0]['attention_state'])
        self.rows[0]['work']['audit'] = [10]
        self.rows[0]['item_details']['10']['labels'] = ['ready-for-review']
        self.assertIsNone(read_flow(self.state, self.rows, now=1250)['issues'][0]['attention_state'])

    def test_newer_local_candidate_contradiction_rejects_runtime_receipt(self):
        self.save(1)
        self.pr(10, stage='audit', head_sha='c' * 40)
        self.wait_receipt()
        # save() observes in 2026, later than this test's runtime receipt.
        self.assertIsNone(read_flow(self.state, self.rows, now=1250)['issues'][0]['attention_state'])

    def test_explicit_audit_pass_heading_is_supported(self):
        self.save(1)
        data = self.audited_pr()
        data['evidence'][0]['body'] = '## Audit: PASS — Criticality: CRITICAL — awaiting human approval'
        self.pr(10, **data)
        self.assertEqual(read_flow(self.state, self.rows)['issues'][0]['attention_state'], 'CRITICAL')
