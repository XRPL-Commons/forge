"""Optional GitHub enforcement preserves Forge's independent authorization."""
from copy import deepcopy
import io
import json
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from forge.app_access import AppClient, AccessError
from forge.factory.github import GitHubUnavailable
from forge.repository_setup import observation_key
from forge.publication_qualification import signing_context
from forge.repository_policy import protection_ready
from forge.settings import digest, validate
from tests import test_repository_rules as rules
from tests import test_repository_merge_policy as policies
from tests import test_store_merge_controller as merges
from tests import test_repository_setup as setup


class OptionalProtectionTests(unittest.TestCase):
    def reader(self, protection=None, effective=None):
        reader = rules.RepositoryRuleTests().reader(protection, effective, bypass='never')
        reader.settings.load.return_value['repositories'] = [dict(id=71, name='example/project', enabled=True, github_protection='optional')]
        return reader

    def test_optional_observes_real_rules_without_requiring_baseline_or_bypass(self):
        reader = self.reader(None, [rules.rule(4), {'type': 'required_signatures', 'ruleset_id': 9}])
        report = reader.protection_report(rules.RepositoryRuleTests.item, {'base_ref': 'main'})
        self.assertTrue(protection_ready(report))
        self.assertEqual(report['required_approvals'], 4)
        self.assertEqual(report['additional_rules'][0]['type'], 'required_signatures')
        self.assertNotIn('bypass', report)
        self.assertTrue(all(call.args[2] == 'GET' for call in reader.call.call_args_list))
        self.assertFalse(any('/rulesets/' in call.args[3] for call in reader.call.call_args_list))

    def test_actor_specific_github_decision_is_left_to_final_merge_api(self):
        reader = self.reader(None, [rules.rule(4)])
        reader.pr = Mock(return_value={'mergeable_state': 'blocked'})
        candidate = dict(pr=1, base_ref='main', head='a'*40, base='b'*40, merge_request=True)
        self.assertTrue(reader.protections(rules.RepositoryRuleTests.item, candidate))
        reader.pr.assert_not_called()

    def test_only_explicit_plan_unavailable_is_accepted(self):
        unavailable = GitHubUnavailable('Plan', status=403, plan_required=True)
        reader = self.reader(unavailable, unavailable)
        report = reader.protection_report(rules.RepositoryRuleTests.item, {'base_ref': 'main'})
        self.assertTrue(protection_ready(report))
        self.assertEqual(report['plan_unavailable_sources'], ['classic', 'rulesets'])
        reader.settings.load.return_value['repositories'][0]['github_protection'] = 'required'
        self.assertTrue(reader.protections(rules.RepositoryRuleTests.item, {'base_ref': 'main'}))
        for error in (GitHubUnavailable('Forbidden', status=403), GitHubUnavailable('Network'), GitHubUnavailable('Server', status=500)):
            self.assertFalse(self.reader(error, []).protections(rules.RepositoryRuleTests.item, {'base_ref': 'main'}))
            self.assertFalse(self.reader(None, error).protections(rules.RepositoryRuleTests.item, {'base_ref': 'main'}))
        self.assertFalse(self.reader(None, [None]).protections(rules.RepositoryRuleTests.item, {'base_ref': 'main'}))

    def test_transport_retains_only_exact_plan_signal(self):
        message = 'Upgrade to GitHub Pro or make this repository public to enable this feature.'
        for status, body, expected in ((403, {'message': message}, True), (403, {'message': 'Resource not accessible by integration'}, False),
                                        (500, {'message': message}, False), (403, ['malformed'], False)):
            error = HTTPError('https://api.github.com/repos/owner/repo/rulesets', status, 'Forbidden', {}, io.BytesIO(json.dumps(body).encode()))
            with patch('forge.app_access.build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(AccessError) as raised:
                    AppClient().request('GET', '/repos/owner/repo/rulesets', 'synthetic')
                self.assertIs(raised.exception.plan_required, expected)
                self.assertNotIn(message, str(raised.exception))
                error.close()

    def test_legacy_configuration_choice_no_longer_changes_admission(self):
        fixture = setup.RepositorySetupTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        service = fixture.service; service.check(10)
        original = fixture.settings.load()
        for mode in ('required', 'optional'):
            changed = deepcopy(original); changed['repositories'][0]['github_protection'] = mode
            validate(changed); fixture.settings.save_config(changed)
            self.assertEqual(observation_key(original, original['repositories'][0]), observation_key(changed, changed['repositories'][0]))
            self.assertTrue(service.view(10)['repositories'][0]['protection_ready'])
            self.assertNotIn('github_protection', service.view(10)['repositories'][0])

    def test_onboarding_can_resume_legacy_policy_without_using_it(self):
        from tests import test_onboarding
        fixture = test_onboarding.SetupTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        fixture.connected()
        settings = fixture.setup.settings()
        config = settings.load()
        config['repositories'] = [{'id': 10, 'name': 'example/project', 'enabled': False, 'github_protection': 'required'}]
        settings.save_config(config)
        fixture.setup.select(10)
        self.assertEqual(settings.load()['repositories'][0]['github_protection'], 'required')

    def test_private_plan_signing_observation_still_requires_verified_branch_and_rules(self):
        denied = AccessError('Plan', status=403, plan_required=True)
        def call(method, suffix):
            if suffix == '/branches/main': return {'name': 'main'}
            raise denied
        self.assertEqual(signing_context(call, 'main', optional=True), [])
        with self.assertRaises(AccessError): signing_context(call, 'main')
        denied.plan_required = False
        with self.assertRaises(AccessError): signing_context(call, 'main', optional=True)
        def classic(method, suffix):
            if suffix == '/branches/main': return {'name': 'main'}
            if suffix.endswith('/required_signatures'): return {'enabled': True}
            return {'required_pull_request_reviews': {}}
        with self.assertRaisesRegex(ValueError, 'classic_signing_rule_unsupported'):
            signing_context(classic, 'main', optional=True)

    def test_existing_baseline_can_publish_own_check_before_final_rule_admission(self):
        from forge.factory.effect_controller import EffectController
        for final_state, expected_merges in (('clean', 1), ('blocked', 1)):
            with self.subTest(final_state=final_state):
                fixture = merges.MergeControllerTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
                fixture.prepare_candidate()
                fixture.remote.pull['mergeable_state'] = 'blocked'
                reader = self.reader(None, [rules.rule(4)])
                reader.pr = fixture.remote.pr
                fixture.remote.protections = lambda item, candidate: reader.protections({**item, 'repo_id': 71}, candidate)
                queued = fixture.controller.step('u')
                self.assertIn('effect_id', queued)
                fixture.remote.on_check = lambda: fixture.remote.pull.update(mergeable_state=final_state)
                driver = EffectController(fixture.store, fixture.remote, admission=fixture.admission, clock=lambda: fixture.now)
                driver.step(queued['effect_id'])
                self.assertEqual(fixture.remote.merge_calls, expected_merges)
                self.assertIn('publish-check', fixture.remote.reads)



    def test_optional_real_report_does_not_replace_review_audit(self):
        fixture = merges.MergeControllerTests(); fixture.setUp(); self.addCleanup(fixture.doCleanups)
        effect = fixture.prepare_candidate()
        unavailable = GitHubUnavailable('Plan', status=403, plan_required=True)
        reader = self.reader(unavailable, unavailable)
        fixture.remote.protections = lambda item, candidate: reader.protections({**item, 'repo_id': 71}, candidate)
        self.assertIsNotNone(fixture.admission(effect))
        for role in ('review', 'audit'):
            saved = deepcopy(fixture.remote.runs)
            fixture.remote.runs = [run for run in saved if run['name'] != 'forge/' + role]
            self.assertIsNone(fixture.admission(effect))
            fixture.remote.runs = saved
        unavailable.plan_required = False
        self.assertIsNone(fixture.admission(effect))


class OptionalPolicyAuthorityTests(policies.connections.Fixture):
    def setUp(self):
        super().setUp()
        from forge.settings import Settings
        from forge.repository_merge_policy import ClientMergePolicy
        self.settings = Settings(self.root, clock=lambda: self.now)
        self.policy = ClientMergePolicy(self.settings, self.service)
        self.credentials = self.token()
        self.token_value = self.credentials['access_token']
        self.base = dict(repository=self.target, instance=self.credentials['connection']['instance'], generation=self.generation, policy='github_protection')

    def test_retired_setting_cannot_create_or_change_repository_policy(self):
        for action in ('read', 'set'):
            request = dict(self.base, action=action)
            if action == 'set': request.update(mode='optional', revision='a'*64, request_id='c'*32)
            self.assertError('policy_retired', lambda: self.policy.request(self.token_value, request))
        self.assertEqual(self.settings.load()['repositories'], [])
