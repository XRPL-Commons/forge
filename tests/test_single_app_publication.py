"""One Forge App publishes every verdict; retired role Apps stay readable history."""
import copy
import unittest
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.github import GitHub, GitHubUnavailable, own_build_pr, own_grouped_pr
from forge.settings import ROLES
from tests.test_settings import configured

HEAD = 'a' * 40


def single_app_config():
    config = configured()
    controller = config['connections']['controller']
    config['retired_apps'] = [
        {'app_id': config['connections'][role]['app_id'], 'slug': 'forge-' + role, 'roles': [role], 'retired_at': 1}
        for role in ('build', 'review', 'audit')]
    config['connections'] = {role: copy.deepcopy(controller) for role in ROLES}
    return config


class SingleAppPublicationTests(unittest.TestCase):
    def setUp(self):
        settings = Mock()
        settings.load.return_value = single_app_config()
        settings.view.return_value = {'roles': {role: {'app_slug': 'forge'} for role in ROLES}}
        client = Mock()
        client.jwt.return_value = 'jwt'
        client.request.return_value = {'token': 'token'}
        self.github = GitHub(settings, validate({}), client=client)
        self.app_id = settings.load.return_value['connections']['controller']['app_id']
        self.comments, self.reviews, self.runs, self.posts = [], [], [], []

        def pages(role, item, path, key=None):
            if path.endswith('/comments'):
                return copy.deepcopy(self.comments)
            if path.endswith('/reviews'):
                return copy.deepcopy(self.reviews)
            return copy.deepcopy(self.runs)

        def call(role, item, method, path, payload=None):
            self.posts.append((role, method, path, payload))
            if path.endswith('/comments'):
                self.comments.append({'id': len(self.comments) + 1, 'body': payload['body'], 'user': {'login': 'forge[bot]'}})
            elif path == '/check-runs':
                self.runs.append({'id': 500 + len(self.runs), 'app': {'id': self.app_id}, 'head_sha': payload['head_sha'],
                                  'name': payload['name'], 'external_id': payload['external_id'],
                                  'status': payload['status'], 'conclusion': payload.get('conclusion'),
                                  'output': payload['output']})
        self.github.pages = Mock(side_effect=pages)
        self.github.call = Mock(side_effect=call)

    def job(self, role):
        return {'id': role + '-job', 'role': role, 'publisher': 'forge[bot]', 'repo': 'example/project', 'repo_id': 1,
                'candidate': {'pr': 7, 'head': HEAD}}

    def test_review_and_audit_verdicts_are_separate_checks_and_comments_from_one_app(self):
        review = self.github.verdict(self.job('review'), {'outcome': 'pass', 'summary': 'Looks right'})
        audit = self.github.verdict(self.job('audit'), {'outcome': 'fail', 'summary': 'Missing a test'})
        self.assertNotEqual(review, audit)
        # Never a PR review: GitHub does not count an App's approval.
        self.assertFalse(any(path.endswith('/reviews') for _, method, path, _ in self.posts if method == 'POST'))
        self.assertEqual({(r['name'], r['conclusion'], r['app']['id']) for r in self.runs},
                         {('forge/review', 'success', self.app_id), ('forge/audit', 'failure', self.app_id)})
        bodies = [c['body'] for c in self.comments]
        self.assertTrue(any('forge/review: passed' in body and 'Looks right' in body for body in bodies))
        self.assertTrue(any('forge/audit: changes needed' in body and 'Missing a test' in body for body in bodies))
        # Replaying a verdict publishes nothing new.
        count = len(self.posts)
        self.assertEqual(self.github.verdict(self.job('review'), {'outcome': 'pass', 'summary': 'Looks right'}), review)
        self.assertEqual(len(self.posts), count)

    def test_observe_only_never_publishes(self):
        self.assertIsNone(self.github.verdict(self.job('review'), {'outcome': 'pass', 'summary': 'x'}, observe_only=True))
        self.assertEqual(self.posts, [])

    def test_a_pending_verdict_the_retired_review_app_already_posted_settles(self):
        marker = '<!-- forge-verdict review-job -->'
        self.reviews = [{'id': 1, 'body': 'ok\n\n' + marker, 'user': {'login': 'forge-review[bot]'},
                         'commit_id': HEAD, 'state': 'APPROVED'}]
        self.runs = [{'id': 40, 'name': 'forge/review', 'app': {'id': 2}, 'external_id': 'review-job', 'head_sha': HEAD,
                      'status': 'completed', 'conclusion': 'success', 'output': {'summary': 'ok'}}]
        job = self.job('review')
        self.assertEqual(self.github.verdict(job, {'outcome': 'pass', 'summary': 'ok'}, observe_only=True), 40)
        self.assertEqual(self.posts, [])

    def test_migrated_lost_verdict_reply_is_read_before_any_new_write(self):
        marker = '<!-- forge-verdict review-job -->'
        self.reviews = [{'id': 1, 'body': marker, 'user': {'login': 'forge-review[bot]'},
                         'commit_id': HEAD, 'state': 'APPROVED'}]
        self.runs = [{'id': 40, 'name': 'forge/review', 'app': {'id': 2}, 'external_id': 'review-job',
                      'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success', 'output': {'summary': 'ok'}}]
        report = {'outcome': 'pass', 'summary': 'ok'}
        # The old App wrote before migration; its reply was lost. Normal retry
        # must reuse that receipt, even if this retry's local reply is also lost.
        self.assertEqual(self.github.verdict(self.job('review'), report), 40)
        self.assertEqual(self.github.verdict(self.job('review'), report, observe_only=True), 40)
        self.assertEqual(self.posts, [])

    def test_obsolete_retired_run_does_not_hide_current_exact_receipt(self):
        self.runs = [{'id': 40, 'name': 'forge/review', 'app': {'id': 2}, 'external_id': 'effect',
                      'head_sha': HEAD, 'status': 'in_progress', 'output': {'summary': 'waiting'}}]
        receipt = self.github.check({}, 'review', {'head': HEAD}, 'effect', 'success', 'ok')
        self.assertNotEqual(receipt, 40)
        self.assertEqual(self.github.check({}, 'review', {'head': HEAD}, 'effect', 'success', 'ok',
                                          observe_only=True), receipt)
        self.assertEqual(len(self.posts), 1)

    def test_retired_receipt_must_match_exact_effect_head_and_outcome(self):
        original = {'id': 40, 'name': 'forge/review', 'app': {'id': 2}, 'external_id': 'effect',
                    'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success', 'output': {'summary': 'ok'}}
        for mismatch in ({'name': 'forge/audit'}, {'app': {'id': 999}}, {'external_id': 'another'},
                         {'head_sha': 'b' * 40}, {'conclusion': 'failure'}, {'output': {'summary': 'different'}}):
            with self.subTest(mismatch=mismatch):
                self.runs = [{**original, **mismatch}]
                self.assertIsNone(self.github.check({}, 'review', {'head': HEAD}, 'effect', 'success', 'ok',
                                                    observe_only=True))
        self.assertEqual(self.posts, [])

    def test_two_exact_receipts_across_apps_still_require_reconciliation(self):
        original = {'id': 40, 'name': 'forge/review', 'app': {'id': 2}, 'external_id': 'effect',
                    'head_sha': HEAD, 'status': 'completed', 'conclusion': 'success', 'output': {'summary': 'ok'}}
        self.runs = [original, {**original, 'id': 41, 'app': {'id': self.app_id}}]
        for observe_only in (True, False):
            with self.assertRaisesRegex(GitHubUnavailable, 'Duplicate'):
                self.github.check({}, 'review', {'head': HEAD}, 'effect', 'success', 'ok', observe_only=observe_only)
        self.assertEqual(self.posts, [])

    def test_gates_accept_evidence_a_retired_app_published(self):
        item = {'candidate': {'head': HEAD}, 'evidence': {'review': {'check_id': 40}, 'audit': {'check_id': 41}}}
        self.runs = [{'id': 40, 'name': 'forge/review', 'app': {'id': 2}, 'status': 'completed', 'conclusion': 'success'},
                     {'id': 41, 'name': 'forge/audit', 'app': {'id': self.app_id}, 'status': 'completed', 'conclusion': 'success'}]
        self.assertTrue(self.github.gates(item))
        # An unrelated App's run never counts, even with the accepted name.
        self.runs[0]['app']['id'] = 999
        self.assertFalse(self.github.gates(item))

    def test_retired_build_bot_prs_remain_forge_owned(self):
        pr = {'head': {'ref': 'forge/issue-3-x', 'repo': {'id': 1}}, 'base': {'repo': {'id': 1}},
              'user': {'login': 'forge-build[bot]', 'type': 'Bot'}, 'state': 'open', 'merged': False}
        self.assertEqual(self.github.publishers('build'), ['forge[bot]', 'forge-build[bot]'])
        self.assertTrue(own_build_pr(pr, 1, 3, self.github.publishers('build')))
        self.assertFalse(own_build_pr({**pr, 'user': {'login': 'someone[bot]', 'type': 'Bot'}}, 1, 3,
                                      self.github.publishers('build')))

    def grouped_pr(self):
        from forge.store.transitions import _id
        # Match _reserve_group: the persisted unit UUID plus request[:8].
        branch = 'forge/unit-' + _id('group-request', 'unit') + '-12345678'
        return {'number': 7, 'head': {'ref': branch, 'sha': HEAD, 'repo': {'id': 1}},
                'base': {'repo': {'id': 1}}, 'user': {'login': 'forge[bot]', 'type': 'Bot'},
                'state': 'open', 'merged': False, 'draft': False}

    def test_grouped_inventory_cannot_take_the_human_automatic_pass_path(self):
        from forge.factory.merge_checks import HumanMergeChecks
        pr = self.grouped_pr()
        self.github.pages = Mock(return_value=[pr])
        item = {'repo': 'example/project', 'repo_id': 1}
        observed = self.github.pulls(item)
        self.assertTrue(observed[0]['forge_owned'])
        self.github.human_merge_policy = Mock()
        self.assertEqual(HumanMergeChecks(self.github, validate({})).reconcile(item, observed), [])
        self.github.human_merge_policy.assert_not_called()
        # A grouped delivery never becomes issue-specific continuation authority.
        self.assertFalse(own_build_pr(pr, 1, 3, self.github.publishers('build')))

    def test_grouped_ownership_requires_known_bot_and_exact_local_branch(self):
        pr = self.grouped_pr()
        publishers = self.github.publishers('build') + self.github.publishers('controller')
        self.assertTrue(own_grouped_pr(pr, 1, publishers))
        retired = {**pr, 'user': {'login': 'forge-build[bot]', 'type': 'Bot'}}
        self.assertTrue(own_grouped_pr(retired, 1, publishers))
        for changed in (
            {'user': {'login': 'other[bot]', 'type': 'Bot'}},
            {'user': {'login': 'forge[bot]', 'type': 'User'}},
            {'head': {**pr['head'], 'repo': {'id': 2}}},
            {'base': {'repo': {'id': 2}}}, {'state': 'closed'}, {'merged': True},
            {'head': {**pr['head'], 'ref': 'forge/unit-guessed-12345678'}},
            {'head': {**pr['head'], 'ref': pr['head']['ref'] + '-extra'}},
        ):
            with self.subTest(changed=changed):
                self.assertFalse(own_grouped_pr({**pr, **changed}, 1, publishers))

    def test_new_checks_always_come_from_the_current_app(self):
        self.runs = [{'id': 40, 'name': 'forge/review', 'app': {'id': 2}, 'external_id': 'effect', 'head_sha': HEAD,
                      'status': 'in_progress', 'output': {'summary': 'x'}}]
        check = self.github.check({}, 'review', {'head': HEAD}, 'effect', 'cancelled', 'Stopped')
        self.assertNotEqual(check, 40)
        self.assertEqual(self.posts[-1][1:3], ('POST', '/check-runs'))

    def test_comment_read_back_must_be_unique(self):
        marker = '<!-- forge-verdict review-job -->'
        self.comments = [{'id': n, 'body': marker, 'user': {'login': 'forge[bot]'}} for n in (1, 2)]
        with self.assertRaises(GitHubUnavailable):
            self.github.verdict(self.job('review'), {'outcome': 'pass', 'summary': 'x'})


if __name__ == '__main__':
    unittest.main()
