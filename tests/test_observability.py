import tempfile
import fcntl
import unittest
from pathlib import Path
from unittest.mock import patch
from forge import observability as obs


class FakeGitHub:
    def __init__(self, items):
        self.items = items
        self.fail = False
        self.calls = []

    def get(self, endpoint, pages=False):
        self.calls.append(endpoint)
        if self.fail:
            raise RuntimeError('private error text')
        if '/reviews?' in endpoint:
            return [{'id': 1, 'body': 'Criticality: NORMAL\nFindings reviewed.', 'user': {'login': 'agent'},
                     'state': 'APPROVED', 'commit_id': 'abc', 'html_url': 'https://github.com/o/r/pull/2#review-1'}]
        if '/comments?' in endpoint:
            return [{'id': 3, 'body': 'Historical context', 'user': {'login': 'author'}, 'html_url': 'https://github.com/o/r/issues/1#issuecomment-3'}]
        if '/pulls/' in endpoint:
            return {'merged_at': None, 'head': {'sha': 'abc'}, 'base': {'sha': 'def'}, 'body': 'Fixes #1'}
        return [x for x in self.items if x['state'] == 'open'] if 'state=open' in endpoint else self.items


def issue(number=1, **changes):
    return dict({'number': number, 'title': 'A task', 'body': 'A  deterministic\nsummary',
                 'labels': [], 'state': 'open', 'created_at': obs.now(), 'updated_at': obs.now(),
                 'html_url': f'https://github.com/o/r/issues/{number}'}, **changes)


class ObservabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state = self.temp.name

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_database_read_does_not_create(self):
        self.assertEqual(obs.read_overview(self.state)['status'], 'unavailable')
        self.assertEqual(list(Path(self.state).iterdir()), [])

    def test_classification(self):
        cases = [({'labels': ['wip']}, 'build'), ({'labels': ['hold', 'wip']}, 'held'),
                 ({'labels': ['ready-for-review', 'ready-for-audit']}, 'unknown'),
                 ({'state': 'closed', 'state_reason': 'not_planned'}, 'closed'),
                 ({'state': 'closed', 'state_reason': 'completed'}, 'completed'),
                 ({'pull_request': {}}, 'unknown')]
        for changes, stage in cases:
            self.assertEqual(obs.classify(issue(**changes)), stage)

    def test_repeated_snapshot_and_real_observation_change(self):
        client = FakeGitHub([issue()])
        obs.collect(self.state, ['o/r'], client=client)
        obs.collect(self.state, ['o/r'], client=client)
        self.assertEqual(len(obs.read_overview(self.state)['events']), 1)
        client.items = [issue(labels=['wip'], updated_at='2099-01-01T00:00:00Z')]
        obs.collect(self.state, ['o/r'], client=client)
        result = obs.read_repository(self.state, 'o/r')
        self.assertEqual(result['events'][0]['previous_stage'], 'pending')
        self.assertEqual(result['items'][0]['stage'], 'build')
        self.assertEqual(result['items'][0]['summary'], 'A deterministic summary')

    def test_failure_preserves_snapshot_and_cursor(self):
        client = FakeGitHub([issue()])
        obs.collect(self.state, ['o/r'], client=client)
        previous = obs.read_repository(self.state, 'o/r')['repos'][0]
        client.fail = True
        obs.collect(self.state, ['o/r'], client=client)
        result = obs.read_repository(self.state, 'o/r')
        self.assertEqual(result['item_count'], 1)
        self.assertEqual(result['repos'][0]['cursor'], previous['cursor'])
        self.assertEqual(result['repos'][0]['observed_at'], previous['observed_at'])
        self.assertNotIn('private error', str(result))

    def test_evidence_backlog_resumes_and_audit_is_not_approval(self):
        client = FakeGitHub([issue(), issue(2, pull_request={}, labels=['ready-for-audit'])])
        obs.collect(self.state, ['o/r'], evidence_limit=1, client=client)
        self.assertEqual(obs.read_overview(self.state)['status'], 'partial')
        obs.collect(self.state, ['o/r'], evidence_limit=1, client=client)
        result = obs.read_repository(self.state, 'o/r')
        self.assertEqual(result['status'], 'ok')
        pr = next(x for x in result['items'] if x['kind'] == 'pr')
        self.assertEqual(pr['review_decision'], 'UNKNOWN')
        self.assertEqual(pr['evidence'][0]['kind'], 'audit')
        self.assertEqual(pr['linked_issues'][0]['number'], 1)
        self.assertEqual(result['counts']['pending'], 1)

    def test_merged_truth_survives_changed_stub(self):
        raw = issue(2, pull_request={}, state='closed', state_reason='completed')
        self.assertEqual(obs.classify(raw), 'unknown')
        prior = obs.normalized('o/r', raw)
        prior.update(merged_at=obs.now(), raw_pr={'merged': True}, head_sha='a')
        updated = obs.normalized('o/r', raw, prior)
        self.assertEqual(updated['stage'], 'completed')
        self.assertEqual(updated['raw_pr'], {'merged': True})
        self.assertEqual(updated['head_sha'], 'a')

    def test_pr_summary_counts_real_check_outcomes(self):
        class SummaryClient(FakeGitHub):
            def pr_summary(self, repo, number):
                return {'headRefOid': 'abc', 'reviewDecision': 'CHANGES_REQUESTED',
                        'mergeStateStatus': 'BLOCKED', 'statusCheckRollup': [
                            {'status': 'COMPLETED', 'conclusion': 'FAILURE'},
                            {'status': 'COMPLETED', 'conclusion': 'SKIPPED'},
                            {'status': 'IN_PROGRESS'}, {'state': 'SUCCESS'}]}
        client = SummaryClient([issue(2, pull_request={})])
        obs.collect(self.state, ['o/r'], client=client)
        pr = obs.read_repository(self.state, 'o/r')['items'][0]
        self.assertEqual(pr['review_decision'], 'CHANGES_REQUESTED')
        self.assertEqual(pr['merge_state'], 'BLOCKED')
        self.assertEqual(pr['checks'], {'total': 4, 'failed': 1, 'pending': 1,
                                       'passed': 1, 'skipped': 1, 'unknown': 0})
        self.assertEqual(pr['summary_head_sha'], 'abc')

    def test_summary_failure_does_not_discard_evidence(self):
        class FailedSummary(FakeGitHub):
            def pr_summary(self, repo, number):
                raise RuntimeError('sensitive stderr')
        obs.collect(self.state, ['o/r'], client=FailedSummary([issue(2, pull_request={})]))
        pr = obs.read_repository(self.state, 'o/r')['items'][0]
        self.assertEqual(pr['evidence_status'], 'ok')
        self.assertTrue(pr['evidence'])
        self.assertEqual(pr['review_decision'], 'UNKNOWN')
        self.assertEqual(pr['summary_error'], 'RuntimeError')
        self.assertIsNone(pr['checks'])

    def test_pr_summary_head_race_is_unknown(self):
        class ChangedHead(FakeGitHub):
            def pr_summary(self, repo, number):
                return {'headRefOid': 'changed', 'reviewDecision': 'APPROVED',
                        'statusCheckRollup': []}
        data = obs.normalized('o/r', issue(2, pull_request={}))
        obs.enrich(ChangedHead([]), 'o/r', data)
        self.assertEqual(data['review_decision'], 'UNKNOWN')
        self.assertEqual(data['summary_status'], 'unavailable')
        self.assertTrue(data['evidence'])

    def test_refresh_rotates_over_limit_and_prioritizes_initial_backlog(self):
        client = FakeGitHub([issue(n, pull_request={}) for n in range(1, 6)])
        with patch.object(obs, 'now', return_value='2026-09-07T00:00:00Z'):
            obs.collect(self.state, ['o/r'], evidence_limit=5, client=client)
        client.items.append(issue(6, state='closed', state_reason='completed'))
        seen = set()
        for minute in (11, 22, 33):
            client.calls.clear()
            with patch.object(obs, 'now', return_value=f'2026-09-07T00:{minute}:00Z'):
                result = obs.collect(self.state, ['o/r'], evidence_limit=2, client=client)
                self.assertEqual(result[0]['status'], 'partial')
                self.assertGreater(obs.read_overview(self.state)['pending_evidence'], 0)
            seen.update(int(call.split('/')[4]) for call in client.calls if '/reviews?' in call)
            if minute == 11:
                self.assertTrue(any('/issues/6/comments?' in call for call in client.calls))
        self.assertEqual(seen, {1, 2, 3, 4, 5})
        db = obs.connect(self.state)
        self.assertTrue(all(row[0] is not None for row in db.execute('SELECT evidence_updated_at FROM items')))
        db.close()

    def test_concurrent_collection_rejected(self):
        with (Path(self.state) / 'observability.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(RuntimeError, 'already running'):
                obs.collect(self.state, ['o/r'], client=FakeGitHub([]))

    def test_initial_merged_enrichment_refines_one_event(self):
        class MergedClient(FakeGitHub):
            def get(self, endpoint, pages=False):
                result = super().get(endpoint, pages)
                if '/pulls/' in endpoint and not pages:
                    result['merged_at'] = obs.now()
                return result
        obs.collect(self.state, ['o/r'], client=MergedClient([issue(2, pull_request={}, state='closed')]))
        result = obs.read_overview(self.state)
        self.assertEqual(len(result['events']), 1)
        self.assertIsNone(result['events'][0]['previous_stage'])
        self.assertEqual(result['events'][0]['stage'], 'completed')

    def test_open_pr_evidence_refreshes_without_issue_update(self):
        client = FakeGitHub([issue(2, pull_request={})])
        obs.collect(self.state, ['o/r'], client=client)
        db = obs.connect(self.state, write=True)
        with db:
            db.execute('UPDATE items SET evidence_updated_at=?', ('2001-01-01T00:00:00Z',))
        db.close()
        client.calls.clear()
        obs.collect(self.state, ['o/r'], client=client)
        self.assertTrue(any('/reviews?' in call for call in client.calls))

    def test_corrupt_database_is_unavailable(self):
        (Path(self.state) / 'observability.sqlite3').write_text('invalid sqlite')
        self.assertEqual(obs.read_overview(self.state)['status'], 'unavailable')

    def test_disabled_repositories_excluded(self):
        obs.collect(self.state, ['o/r', 'o/disabled'], client=FakeGitHub([issue()]))
        result = obs.read_overview(self.state, repos=['o/r'])
        self.assertEqual(result['item_count'], 1)
        self.assertTrue(all(x['repo'] == 'o/r' for x in result['events']))
        self.assertEqual(obs.read_overview(self.state, repos=[])['status'], 'unavailable')

    def test_dense_chart_and_separate_counts(self):
        obs.collect(self.state, ['o/r'], client=FakeGitHub([issue()]))
        result = obs.read_overview(self.state, days=7)
        self.assertEqual(len(result['daily']), 7)
        self.assertTrue(all(day['within_coverage'] for day in result['daily']))
        self.assertEqual(result['counts_by_kind']['issue']['pending'], 1)
        self.assertEqual(result['counts_by_kind']['pr']['pending'], 0)
        self.assertEqual(result['pending_evidence'], 0)

    def test_all_old_open_items_and_completed_lifecycle(self):
        client = FakeGitHub([issue(created_at='2001-01-01T00:00:00Z'),
                             issue(2, state='closed', state_reason='completed', closed_at=obs.now())])
        obs.collect(self.state, ['o/r'], client=client)
        result = obs.read_overview(self.state)
        self.assertTrue(any('state=open' in x for x in client.calls))
        self.assertEqual(result['counts']['pending'], 1)
        self.assertEqual(sum(x['completed'] for x in result['daily']), 1)
        self.assertTrue(all(x['previous_stage'] is None for x in result['events']))


if __name__ == '__main__':
    unittest.main()


class AuditEvidenceFormatTests(unittest.TestCase):
    def test_live_markdown_formats_and_handoff_distinction(self):
        from forge.observability import classify_evidence
        for body in ('## Security audit — PASS', '**Criticality: CRITICAL**', 'Criticality: NORMAL', '## Criticality classification — auditor decision'):
            item = classify_evidence({'kind': 'review', 'body': body})
            self.assertEqual(item['kind'], 'audit')
            self.assertEqual(item['source_kind'], 'review')
        item = classify_evidence({'kind': 'comment', 'body': '## Reviewer: PASS → handing to security audit'})
        self.assertEqual(item['kind'], 'review')
        self.assertEqual(item['source_kind'], 'comment')


class CoverageTests(unittest.TestCase):
    def test_newly_enabled_repository_is_reported_as_uncollected(self):
        from forge.observability import connect, read_overview
        with tempfile.TemporaryDirectory() as state:
            db = connect(state, write=True)
            db.execute("INSERT INTO repositories VALUES('o/one','2026-09-07T00:00:00Z','2026-08-08T00:00:00Z',NULL,'ok','2026-09-07T00:00:00Z')")
            db.commit()
            db.close()
            result = read_overview(state, repos=['o/one', 'o/new'])
            self.assertEqual(result['status'], 'partial')
            self.assertIn({'repo': 'o/new', 'error': 'No local collection yet'}, result['errors'])
