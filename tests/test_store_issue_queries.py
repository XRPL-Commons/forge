import json
import unittest
from unittest.mock import patch

from forge.store.queries import Queries
from tests import test_store_completion as helpers


class IssueQueryTests(unittest.TestCase):
    setUp = helpers.StoreCompletionTests.setUp
    admit = helpers.StoreCompletionTests.admit
    view = helpers.StoreCompletionTests.view
    command = helpers.StoreCompletionTests.command
    observe = helpers.StoreCompletionTests.observe
    candidate = helpers.StoreCompletionTests.candidate
    adopt = helpers.StoreCompletionTests.adopt
    merge = helpers.StoreCompletionTests.merge

    def test_issue_pages_and_detail_share_lifecycle_and_skip_bodies_on_lists(self):
        self.admit(2)
        queries = Queries(self.store)
        first = queries.issues(limit=1)
        self.assertEqual(first['next_cursor'], [1, 1])
        item = first['items'][0]
        self.assertNotIn('body', item)
        self.assertNotIn('requirements', item)
        detail = queries.issue('issue-1')
        self.assertEqual(detail['lifecycle'], item['lifecycle'])
        self.assertEqual(detail['body'], 'Implement the requirement')
        second = queries.issues(after=first['next_cursor'], limit=1)
        self.assertEqual([row['id'] for row in second['items']], ['issue-2'])
        self.assertIsNone(second['next_cursor'])

    def test_gate_on_issue_matches_unit_and_clearing_one_hold_preserves_another(self):
        queries = Queries(self.store)
        self.observe(labels=['hold'])
        self.lifecycle.pause(self.command())
        self.observe(labels=[], generation=2)
        issue = queries.issue('issue-1')['lifecycle']
        unit = queries.unit('unit-1')
        self.assertEqual(issue['state'], unit['members'][0]['projected_state'])
        self.assertEqual(issue['reason_code'], unit['reason_code'])
        self.assertEqual(issue['reason_code'], 'operator_pause')
        self.lifecycle.resume(self.command())
        self.assertEqual(queries.issue('issue-1')['lifecycle']['state'], 'queued')

    def test_reopened_issue_does_not_inherit_old_merge_then_uses_new_membership(self):
        queries = Queries(self.store)
        candidate = self.adopt()['candidate_id']
        self.merge(candidate)
        self.assertEqual(queries.issue('issue-1')['lifecycle']['state'], 'done')
        self.observe(state='open', reason='reopened')
        self.assertEqual(queries.issue('issue-1')['lifecycle']['state'], 'untracked')
        self.assertEqual(self.view()['state'], 'merged')
        self.assertEqual(self.view()['members'][0]['projected_state'], 'untracked')
        # A later title/label observation can report state_reason=null. It must
        # not erase the fact that this issue reopened after its old completion.
        self.observe(state='open', reason=None, generation=2)
        self.assertEqual(queries.issue('issue-1')['lifecycle']['state'], 'untracked')
        detail = queries.issue('issue-1')
        self.lifecycle.admit_known_issue(request_id='new-delivery', actor_id='operator', unit_id='new-unit',
            issue_id='issue-1', expected_issue_version=detail['version'])
        self.assertEqual(queries.issue('issue-1')['lifecycle']['unit_id'], 'new-unit')
        self.assertEqual(queries.issue('issue-1')['lifecycle']['state'], 'queued')

    def test_completed_elsewhere_dropped_and_unknown_closure_have_distinct_projection(self):
        queries = Queries(self.store)
        for generation, (reason, expected) in enumerate([('completed', 'done'), ('not_planned', 'dropped'),
                                                        ('duplicate', 'dropped'), (None, 'blocked')], 1):
            self.observe(state='closed', reason=reason, generation=generation)
            self.assertEqual(queries.issue('issue-1')['lifecycle']['state'], expected)
            self.assertEqual(self.view()['members'][0]['projected_state'], expected)

    def test_untracked_inventory_issue_has_no_fabricated_unit(self):
        # A completed delivery's reopened issue is known inventory, but has no
        # live membership. No new unit is created just because somebody reads it.
        self.observe(state='closed', reason='completed')
        queries = Queries(self.store)
        version = queries.issue('issue-1')['version']
        self.lifecycle.close_unit(self.command(), close_code='github_completed', evidence={'issue_version': version})
        self.observe(state='open', reason='reopened', generation=2)
        result = queries.issues(repository_id=1, github_state='open')['items'][0]['lifecycle']
        self.assertEqual(result['state'], 'untracked')
        self.assertIsNone(result['unit_id'])
        self.assertEqual(result['historical_unit_id'], 'unit-1')
        self.assertEqual(queries.issues(repository_id=2)['items'], [])

    def test_list_queries_do_not_fetch_output_requirements_body_or_import_archive(self):
        original = self.store._connect_path
        statements = []
        def connection(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch.object(self.store, '_connect_path', side_effect=connection):
            Queries(self.store).issues()
            Queries(self.store).jobs('unit-1')
        reads = [sql.lower() for sql in statements if sql.lstrip().upper().startswith('SELECT')]
        for word in ('requirements_json', 'r.body', 'job_artifacts', 'import_records'):
            self.assertFalse(any(word in sql for sql in reads), word)

    def test_invalid_page_and_cursor_rejected(self):
        queries = Queries(self.store)
        for options in ({'limit': 101}, {'after': [1]}, {'after': [True, 2]}, {'repository_id': True},
                        {'github_state': 'all'}, {'limit': 0}):
            with self.assertRaises(ValueError):
                queries.issues(**options)


if __name__ == '__main__':
    unittest.main()
