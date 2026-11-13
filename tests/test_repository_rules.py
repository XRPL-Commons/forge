import base64
import unittest
from unittest.mock import Mock

from forge.factory.github import GitHub, GitHubUnavailable, GitHubBudgetExhausted


def classic(count=2, stale=True, owners=False):
    return {'required_pull_request_reviews': {'required_approving_review_count': count,
            'dismiss_stale_reviews': stale, 'require_code_owner_reviews': owners,
            'bypass_pull_request_allowances': {'apps': [{'id': 123456}]}}}


# Ruleset fields follow https://docs.github.com/en/rest/repos/rules#create-a-repository-ruleset
# CODEOWNERS uses a singular field here; classic branch protection uses plural.
def rule(count=2, stale=True, owners=False, identifier=12):
    return {'type': 'pull_request', 'ruleset_id': identifier, 'ruleset_source': 'example',
            'ruleset_source_type': 'Organization', 'parameters': {
                'required_approving_review_count': count, 'dismiss_stale_reviews_on_push': stale,
                'require_code_owner_review': owners}}


class RepositoryRuleTests(unittest.TestCase):
    item = {'repo_id': 71, 'repo': 'example/project'}

    def reader(self, protection=None, rules=None, bypass='always'):
        settings = Mock(); settings.load.return_value = {'repositories': [], 'connections': {'controller': {'app_id': 123456}}}
        github = GitHub(settings, {'human_reviewers': ['maintainer']}, clock=lambda: 1234)
        def read(role, item, method, suffix, payload=None):
            self.assertEqual(method, 'GET')
            if '/protection' in suffix:
                if protection is None:
                    raise GitHubUnavailable('Absent', status=404)
                if isinstance(protection, Exception):
                    raise protection
                return protection
            if '/rules/branches/' in suffix:
                if isinstance(rules, Exception):
                    raise rules
                return rules or []
            if '/rulesets/' in suffix:
                if isinstance(bypass, Exception):
                    raise bypass
                return {'id': int(suffix.rsplit('/', 1)[1]), 'current_user_can_bypass': bypass}
            if '/contents/' in suffix:
                return {'content': base64.b64encode(b'* @maintainer\n').decode()}
            raise AssertionError(suffix)
        github.call = Mock(side_effect=read)
        return github

    def shared(self,reader,scope='all'):
        connection={'app_id':123456,'installation_id':1,'private_key_env':'FORGE_KEY'}
        reader.settings.load.return_value={'repositories': [], 'connections':{role:dict(connection) for role in ('build','review','audit','controller')}}
        reader.runtime={'repository_merge_check_scopes':{'71':scope}}
        return reader

    def policy_rule(self,app_id=123456):
        return {'type':'required_status_checks','ruleset_id':13,
                'parameters':{'required_status_checks':[{'context':'forge/merge-policy','integration_id':app_id}]}}

    def test_shared_app_observes_existing_checks_without_requiring_a_forge_ruleset(self):
        for app_id in (None, -1, 123457, True, 123456):
            reader = self.shared(self.reader(None, [rule(4), self.policy_rule(app_id)]))
            report = reader.protection_report(self.item, {'base_ref': 'main'})
            self.assertTrue(reader.protections(self.item, {'base_ref': 'main'}))
            self.assertEqual(report['required_approvals'], 4)
            self.assertEqual([r['id'] for r in report['requirements']], ['readable'])
            self.assertTrue(all(call.args[2] == 'GET' for call in reader.call.call_args_list))

    def test_review_scope_does_not_require_external_github_enforcement(self):
        for scope in ('all', 'forge'):
            self.assertTrue(self.shared(self.reader(None, []), scope).protections(self.item, {'base_ref': 'main'}))

    def test_classic_check_is_observed_without_imposing_forge_app_binding(self):
        protection = classic()
        protection['required_status_checks'] = {'contexts': ['external-ci']}
        self.assertTrue(self.shared(self.reader(protection, [])).protections(self.item, {'base_ref': 'main'}))

    def test_classic_ruleset_and_mixed_share_the_same_minimum(self):
        for protection, rules in [(classic(), []), (None, [rule()]),
                                  (classic(3, False), [rule(0, True)])]:
            with self.subTest(protection=protection):
                github = self.reader(protection, rules)
                self.assertTrue(github.protections(self.item, {'base_ref': 'main'}))
                report = github.protection_report(self.item, {'base_ref': 'main'})
                self.assertEqual(report['repo_id'], 71)
                self.assertEqual(report['observed_at'], 1234)
                self.assertNotIn('bypass', report)
                self.assertNotIn('bypass_actors', report)

    def test_no_rules_or_partial_rules_do_not_block_supervisor_admission(self):
        for rules in [[], [rule(1)], [rule(stale=False)]]:
            self.assertTrue(self.reader(None, rules).protections(self.item, {'base_ref': 'main'}))

    def test_inherited_sources_stricter_counts_and_split_requirements(self):
        report = self.reader(None, [rule(4, False), rule(0, True, identifier=13)]).protection_report(self.item, {'base_ref': 'release/v2'})
        self.assertEqual(report['required_approvals'], 4)
        self.assertEqual(report['requirements'][0]['sources'][0]['id'], 12)
        self.assertEqual(report['requirements'][0]['sources'][1]['id'], 13)
        self.assertEqual(report['sources'][0]['source_type'], 'Organization')

    def test_absent_classic_does_not_hide_ruleset_and_unknown_never_passes(self):
        for status in [403, 429, 500]:
            self.assertFalse(self.reader(GitHubUnavailable('Unknown', status=status), [rule()]).protections(self.item, {'base_ref': 'main'}))
            self.assertFalse(self.reader(classic(), GitHubUnavailable('Unknown', status=status)).protections(self.item, {'base_ref': 'main'}))

    def test_budget_exhaustion_never_becomes_a_protection_failure(self):
        for protection, rules in [(GitHubBudgetExhausted('Cycle ended'), []),
                                  (classic(), GitHubBudgetExhausted('Cycle ended'))]:
            with self.assertRaises(GitHubBudgetExhausted):
                self.reader(protection, rules).protection_report(self.item, {'base_ref': 'main'})

    def test_malformed_rules_fail_closed(self):
        for rules in [[None], [{'type': 'pull_request'}], [rule('2')], [rule(stale='true')]]:
            self.assertFalse(self.reader(None, rules).protections(self.item, {'base_ref': 'main'}))

    def test_active_branch_endpoint_and_pagination(self):
        github = self.reader(None, [])
        first_page = [{'type': 'non_fast_forward', 'ruleset_id': i} for i in range(100)]
        def read(role, item, method, suffix):
            if '/protection' in suffix:
                raise GitHubUnavailable('Absent', status=404)
            self.assertIn('/rules/branches/release%2Fv2?', suffix)
            return first_page if suffix.endswith('&page=1') else [rule()]
        github.call = Mock(side_effect=lambda *a: {'current_user_can_bypass': 'always'} if '/rulesets/' in a[3] else read(*a))
        self.assertTrue(github.protections(self.item, {'base_ref': 'release/v2'}))
        self.assertEqual(github.call.call_count, 3)
        # GitHub's branch endpoint returns active, applicable rules only. Do not
        # infer enforcement from a ruleset inventory that includes disabled/evaluate.
        self.assertTrue(all('/rulesets' not in c.args[3] for c in github.call.call_args_list[:3]))

    def test_other_constraints_remain_visible_not_waived(self):
        report = self.reader(None, [rule(), {'type': 'merge_queue', 'ruleset_id': 12}]).protection_report(self.item, {'base_ref': 'main'})
        self.assertEqual(report['additional_rules'][0]['type'], 'merge_queue')

    def test_controller_bypass_is_never_an_admission_requirement(self):
        for bypass in ('never', None, GitHubUnavailable('Hidden', status=403), 'unexpected'):
            reader = self.reader(None, [rule()], bypass=bypass)
            report = reader.protection_report(self.item, {'base_ref': 'main'})
            self.assertNotIn('bypass', report)
            self.assertTrue(reader.protections(self.item, {'base_ref': 'main'}))
            self.assertFalse(any('/rulesets/' in call.args[3] for call in reader.call.call_args_list))
