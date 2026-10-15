"""forge/merge-policy scope: every PR by default, with a passing path for human PRs."""
import copy
import unittest
from unittest.mock import Mock

from forge.factory.config import merge_check_scope, policy, validate
from forge.factory.github import GitHub, GitHubUnavailable
from forge.factory.merge_checks import ReviewPullsController, merge_policy_rule
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


class ReviewPolicyTests(unittest.TestCase):
    def test_automatic_human_pass_publisher_is_absent(self):
        self.assertFalse(hasattr(GitHub, 'human_merge_policy'))
        self.assertTrue(callable(ReviewPullsController.reconcile))


if __name__ == '__main__':
    unittest.main()
