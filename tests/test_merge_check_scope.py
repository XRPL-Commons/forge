"""forge/merge-policy scope: every PR by default, with a passing path for human PRs."""
import copy
import unittest
from unittest.mock import Mock

from forge.factory.config import merge_check_scope, policy, validate
from forge.factory.github import GitHub, GitHubUnavailable
from forge.factory.merge_checks import ReviewPullsController
from forge.settings import ROLES
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



class ReviewPolicyTests(unittest.TestCase):
    def test_automatic_human_pass_publisher_is_absent(self):
        self.assertFalse(hasattr(GitHub, 'human_merge_policy'))
        self.assertTrue(callable(ReviewPullsController.reconcile))


if __name__ == '__main__':
    unittest.main()
