"""forge/merge-policy scope: every PR by default, with a passing path for human PRs."""
import copy
import unittest
from unittest.mock import Mock

from forge.factory.config import merge_check_scope, policy, validate
from forge.factory.github import GitHub, GitHubUnavailable
from forge.factory.merge_checks import HumanMergeChecks, merge_policy_rule
from forge.settings import ROLES
from forge_client.contracts import baseline
from tests.test_single_app_publication import single_app_config

HUMAN, FORGE = 'b' * 40, 'c' * 40


class ScopeSettingTests(unittest.TestCase):
    def test_all_pull_requests_by_default_and_forge_only_is_explicit(self):
        config = validate({})
        self.assertEqual(merge_check_scope(config, 10), 'all')
        config = validate({'repository_merge_check_scopes': {'10': 'forge'}})
        self.assertEqual(merge_check_scope(config, 10), 'forge')
        self.assertEqual(merge_check_scope(config, 11), 'all')
        for bad in ({'10': 'some'}, {'x': 'all'}, {'0': 'all'}, []):
            with self.assertRaises(ValueError):
                validate({'repository_merge_check_scopes': bad})

    def test_scope_never_changes_a_candidates_policy_hash(self):
        before = policy(validate({}), 10)
        after = policy(validate({'repository_merge_check_scopes': {'10': 'forge'}}), 10)
        self.assertEqual(before, after)

    def test_baseline_requires_the_check_from_the_forge_app_only_for_scope_all(self):
        forge_only = baseline('main', 42, 'forge')
        self.assertEqual([rule['type'] for rule in forge_only['rules']], ['pull_request'])
        self.assertEqual(baseline('main', 42), forge_only)
        every = baseline('main', 42, 'all')
        self.assertEqual(every['rules'][1], merge_policy_rule(42))
        # Additive only: the review rule and both bypass actors are unchanged.
        self.assertEqual(every['rules'][0], forge_only['rules'][0])
        self.assertEqual(every['bypass_actors'], forge_only['bypass_actors'])
        self.assertEqual([actor for actor in every['bypass_actors'] if actor['actor_type'] == 'Integration'],
                         [{'actor_id': 42, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'}])


class HumanMergePolicyTests(unittest.TestCase):
    def setUp(self):
        settings = Mock()
        settings.load.return_value = single_app_config()
        settings.view.return_value = {'roles': {role: {'app_slug': 'forge'} for role in ROLES}}
        self.github = GitHub(settings, validate({}), client=Mock())
        self.app_id = settings.load.return_value['connections']['controller']['app_id']
        self.runs, self.posts = {}, []
        def pages(role, item, path, key=None):
            head = path.split('/commits/')[1].split('/')[0]
            return copy.deepcopy(self.runs.get(head, []))
        def call(role, item, method, path, payload=None):
            self.posts.append((role, method, path))
            if path == '/check-runs':
                self.runs.setdefault(payload['head_sha'], []).append(
                    {'id': 900 + len(self.posts), 'name': payload['name'], 'app': {'id': self.app_id},
                     'external_id': payload['external_id'], 'head_sha': payload['head_sha'],
                     'status': payload['status'], 'conclusion': payload.get('conclusion'), 'output': payload['output']})
        self.github.pages = Mock(side_effect=pages)
        self.github.call = Mock(side_effect=call)
        self.item = {'repo': 'example/project', 'repo_id': 1}

    def pr(self, number, head, *, forge=False, **extra):
        return {'number': number, 'state': 'open', 'draft': False, 'forge_owned': forge,
                'head': {'sha': head, 'repo': {'id': 1}}, 'base': {'repo': {'id': 1}}, **extra}

    def test_a_human_pr_gets_one_passing_check_from_the_forge_app(self):
        checks = HumanMergeChecks(self.github, validate({}))
        results = checks.reconcile(self.item, [self.pr(5, HUMAN)])
        self.assertEqual(results[0]['status'], 'published')
        run, = self.runs[HUMAN]
        self.assertEqual((run['name'], run['conclusion'], run['app']['id'], run['external_id']),
                         ('forge/merge-policy', 'success', self.app_id, 'human:5'))
        self.assertIn('Forge review and audit do not apply', run['output']['summary'])
        # Reconciling again is idempotent.
        posts = len(self.posts)
        self.assertEqual(checks.reconcile(self.item, [self.pr(5, HUMAN)])[0]['check_id'], run['id'])
        self.assertEqual(len(self.posts), posts)

    def test_forge_prs_and_shared_commits_only_pass_through_forge_gates(self):
        checks = HumanMergeChecks(self.github, validate({}))
        results = checks.reconcile(self.item, [self.pr(6, FORGE, forge=True), self.pr(7, FORGE)])
        self.assertEqual([r['status'] for r in results], ['forge_candidate'])
        self.assertEqual(self.posts, [])
        # A commit where the merge controller already published keeps its verdict.
        self.runs[HUMAN] = [{'id': 1, 'name': 'forge/merge-policy', 'app': {'id': self.app_id}, 'external_id': 'effect-1',
                             'head_sha': HUMAN, 'status': 'completed', 'conclusion': 'failure', 'output': {}}]
        self.assertEqual(checks.reconcile(self.item, [self.pr(8, HUMAN)])[0]['status'], 'forge_candidate')
        self.assertEqual(self.posts, [])
        with self.assertRaises(ValueError):
            self.github.human_merge_policy(self.item, self.pr(6, FORGE, forge=True))

    def test_forge_scope_leaves_human_prs_alone(self):
        checks = HumanMergeChecks(self.github, validate({'repository_merge_check_scopes': {'1': 'forge'}}))
        self.assertEqual(checks.reconcile(self.item, [self.pr(5, HUMAN)]), [])
        self.github.pages.assert_not_called()

    def test_drafts_closed_and_foreign_base_prs_are_skipped(self):
        checks = HumanMergeChecks(self.github, validate({}))
        pulls = [self.pr(5, HUMAN, draft=True), self.pr(6, HUMAN, state='closed'),
                 {**self.pr(7, HUMAN), 'base': {'repo': {'id': 2}}}]
        self.assertEqual(checks.reconcile(self.item, pulls), [])
        self.assertEqual(self.posts, [])

    def test_unconfirmed_publication_is_reported_and_retried_later(self):
        self.github.call = Mock(side_effect=GitHubUnavailable('down', status=502))
        result, = HumanMergeChecks(self.github, validate({})).reconcile(self.item, [self.pr(5, HUMAN)])
        self.assertEqual((result['status'], result['http_status']), ('unconfirmed', 502))


if __name__ == '__main__':
    unittest.main()
