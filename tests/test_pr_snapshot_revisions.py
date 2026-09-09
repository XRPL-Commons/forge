import copy
import json
from types import SimpleNamespace
import unittest
from forge.github import GitHub, GitHubError


class PRSnapshotRevisionTests(unittest.TestCase):
    def setUp(self):
        self.repo = SimpleNamespace(repo='Owner/App')
        self.stamp = '2026-09-09T10:00:00Z'
        self.calls = []
        self.pulls = [{'number': 2, 'state': 'open', 'updated_at': self.stamp,
            'html_url': 'https://github.com/Owner/App/pull/2',
            'head': {'sha': 'a'*40}, 'base': {'sha': 'b'*40, 'repo': {'full_name': 'owner/app'}}}]
        def run(args, **kwargs):
            self.calls.append(args)
            self.assertEqual(args, ['gh','api','--paginate','--slurp','repos/Owner/App/pulls?state=open&per_page=100'])
            return SimpleNamespace(returncode=0, stdout=json.dumps([self.pulls]))
        self.github = GitHub(run=run)

    def details(self):
        return {'2': {'kind': 'pr', 'number': 2, 'updated_at': self.stamp}}

    def test_changed_revision_uses_one_paginated_inventory_and_exact_join(self):
        details = self.details()
        details['3'] = {**details['2'], 'number': 3}
        self.pulls.append({**copy.deepcopy(self.pulls[0]), 'number':3, 'html_url':'https://github.com/Owner/App/pull/3'})
        self.github.pr_revisions(self.repo, details, {})
        self.assertEqual(len(self.calls), 1)
        for detail in details.values():
            self.assertEqual(detail['head_sha'], 'a'*40)
            self.assertEqual(detail['base_sha'], 'b'*40)
            self.assertEqual(detail['revision_status'], 'canonical')

    def test_unchanged_validated_snapshot_reuses_without_request(self):
        details = self.details(); self.github.pr_revisions(self.repo, details, {})
        previous = {'status':'ok', 'repo':'owner/app', 'item_details':details}
        self.calls.clear(); current = self.details()
        self.github.pr_revisions(self.repo, current, previous)
        self.assertEqual(self.calls, [])
        self.assertEqual(current, details)
        current['2']['updated_at'] = '2026-09-09T10:01:00Z'
        self.github.pr_revisions(self.repo, current, previous)
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(current['2']['head_sha'])

    def test_races_wrong_identity_and_invalid_sha_remain_unknown(self):
        original = copy.deepcopy(self.pulls[0])
        for changes in [{'updated_at':'2026-09-09T11:00:00Z'}, {'state':'closed'},
                        {'html_url':'https://github.com/Other/App/pull/2'},
                        {'head':{'sha':'bad'}}, {'base':{'sha':'b'*40,'repo':{'full_name':'other/app'}}}]:
            self.pulls = [{**copy.deepcopy(original), **changes}]
            details = self.details(); self.github.pr_revisions(self.repo, details, {})
            self.assertEqual(details['2']['revision_status'], 'unknown')
            self.assertIsNone(details['2']['head_sha'])
        self.pulls = []; details = self.details()
        self.github.pr_revisions(self.repo, details, {})
        self.assertIsNone(details['2']['base_sha'])

    def test_missing_timestamp_never_fetches_or_reuses(self):
        for stamp in [None, '', 'invalid', '2026-09-09']:
            details = self.details(); details['2']['updated_at'] = stamp
            self.github.pr_revisions(self.repo, details, {})
            self.assertEqual(details['2']['revision_status'], 'unknown')
        self.assertEqual(self.calls, [])

    def test_unvalidated_cache_is_not_trusted(self):
        details = self.details()
        previous = {'status':'ok','repo':'Owner/App','item_details':{'2':{**details['2'],
            'head_sha':'c'*40,'base_sha':'d'*40}}}
        self.github.pr_revisions(self.repo, details, previous)
        self.assertEqual(details['2']['head_sha'], 'a'*40)
        self.assertEqual(len(self.calls), 1)

    def test_failed_or_duplicate_inventory_never_becomes_empty_queue(self):
        self.github.run = lambda *a, **k: SimpleNamespace(returncode=1,stdout='')
        with self.assertRaises(GitHubError):
            self.github.pr_revisions(self.repo, self.details(), {})
        self.setUp(); self.pulls.append(copy.deepcopy(self.pulls[0]))
        with self.assertRaises(GitHubError):
            self.github.pr_revisions(self.repo, self.details(), {})
