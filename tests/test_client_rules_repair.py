"""Effective branch repairs preserve rules and uncertain write identity."""
from copy import deepcopy
import unittest

from forge_client.contracts import SetupError
from forge_client import rules


class GitHub:
    def __init__(self):
        self.target = {'id': 10, 'name': 'owner/project', 'branch': 'main', 'admin': True}
        self.rule = {'type': 'pull_request', 'ruleset_id': 42, 'ruleset_source_type': 'Repository',
                     'parameters': {'required_approving_review_count': 2}}
        self.value = {'id': 42, 'name': 'Reviews', 'target': 'branch', 'enforcement': 'active',
                      'conditions': {'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
                      'rules': [{'type': 'pull_request', 'parameters': {'required_approving_review_count': 2}}],
                      'bypass_actors': [{'actor_id': 99, 'actor_type': 'Team', 'bypass_mode': 'pull_request'}]}
        self.classic = None
        self.admin = True
        self.calls = []
        self.lose_response = False

    def _request(self, method, path, payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        if '/rules/branches/' in path: return [deepcopy(self.rule)]
        if path.endswith('/protection'):
            if self.classic is None: raise SetupError('github_not_found', '')
            return {'required_pull_request_reviews': self.classic}
        if path.endswith('/required_pull_request_reviews'):
            if method == 'PATCH': self.classic.update(deepcopy(payload))
            # GET API represents actors as objects; PATCH accepts slugs.
            value = deepcopy(self.classic)
            for key, field in (('users', 'login'), ('teams', 'slug'), ('apps', 'slug')):
                value['bypass_pull_request_allowances'][key] = [{field: row} if isinstance(row, str) else row for row in value['bypass_pull_request_allowances'][key]]
            return value
        if '/user/installations?' in path:
            return {'installations': [{'app_id': 7, 'app_slug': 'forge-controller'}]}
        if '/memberships/orgs/' in path: return {'state': 'active', 'role': 'admin' if self.admin else 'member'}
        if '/rulesets/42' in path:
            if method == 'PUT':
                self.value.update(deepcopy(payload))
                if self.lose_response: raise SetupError('github_timeout', '')
            return deepcopy(self.value)
        raise AssertionError((method, path))

    def repository(self, name): return deepcopy(self.target)
    def rulesets(self, name): return [{'id': 42, 'name': 'Reviews', 'source_type': 'Repository'}]


class RepairTests(unittest.TestCase):
    def test_preparation_repairs_existing_needs_setup_rule_instead_of_creating_baseline(self):
        from forge_client.preparation import Preparation
        from forge_client.contracts import connection_identity
        github = GitHub()
        observed = {'version': 1, 'repository': {'id': 10, 'name': 'owner/project'}, 'revision': 'a'*64,
                    'roles': {role: {'app_id': number, 'installation_ids': [number + 100], 'access': 'ready'}
                              for role, number in zip(('build', 'review', 'audit', 'controller'), (4, 5, 6, 7))},
                    'readiness': {'discovered': True, 'enabled': False, 'access_ready': True,
                                  'protection_ready': False, 'protection_status': 'needs_setup'},
                    'baseline': {'branch': 'main', 'required_approvals': 2, 'dismiss_stale_reviews': True}}
        class Forge:
            def authorize(self, *args): pass
            def requirements(self, target): return deepcopy(observed)
            def verify(self, *args): observed['readiness'].update(protection_ready=True, protection_status='ready')
        preparation = Preparation(github, Forge(), instance='instance', generation='generation', apps=connection_identity(observed), clock=lambda: 100)
        plan = preparation.plan('owner/project')
        self.assertEqual([row['kind'] for row in plan['steps']], ['github.ruleset.repair_bypass'])
        self.assertEqual(preparation.apply(plan)['status'], 'ready')
        self.assertEqual([method for method, _, _ in github.calls if method != 'GET'], ['PUT'])

    def test_only_bypass_actors_are_written_and_previous_actors_and_rules_survive(self):
        github = GitHub()
        before = deepcopy(github.value)
        plan = rules.inspect(github, github.target, 7)
        self.assertFalse(plan['blockers'])
        self.assertTrue(all(method == 'GET' for method, _, _ in github.calls))
        rules.apply(github, github.target, plan['steps'][0], 7)
        writes = [(method, path, body) for method, path, body in github.calls if method != 'GET']
        self.assertEqual(len(writes), 1)
        self.assertEqual(set(writes[0][2]), {'bypass_actors'})
        self.assertEqual(github.value['rules'], before['rules'])
        self.assertEqual(github.value['bypass_actors'][0], before['bypass_actors'][0])
        self.assertFalse(rules.inspect(github, github.target, 7)['steps'])

    def test_uncertain_response_is_reconciled_by_readback_without_second_put(self):
        github = GitHub(); github.lose_response = True
        step = rules.inspect(github, github.target, 7)['steps'][0]
        with self.assertRaises(SetupError): rules.apply(github, github.target, step, 7)
        self.assertTrue(rules.confirmed(github, github.target, step))
        self.assertEqual(sum(method == 'PUT' for method, _, _ in github.calls), 1)

    def test_concurrent_change_refuses_write(self):
        github = GitHub()
        step = rules.inspect(github, github.target, 7)['steps'][0]
        github.value['bypass_actors'].append({'actor_type': 'Team', 'actor_id': 100, 'bypass_mode': 'always'})
        with self.assertRaises(SetupError) as error: rules.apply(github, github.target, step, 7)
        self.assertEqual(error.exception.code, 'plan_stale')
        self.assertTrue(all(method == 'GET' for method, _, _ in github.calls))

    def test_org_admin_is_checked_separately_and_signatures_are_preserved(self):
        github = GitHub(); github.rule['ruleset_source_type'] = 'Organization'; github.admin = False
        plan = rules.inspect(github, github.target, 7)
        self.assertEqual(plan['blockers'][0]['code'], 'organization_admin_required')
        self.assertFalse(plan['steps'])
        github.admin = True; github.value['rules'].append({'type': 'required_signatures'})
        self.assertEqual(rules.inspect(github, github.target, 7)['blockers'][0]['code'], 'signing_bypass_refused')
        self.assertTrue(all(method == 'GET' for method, _, _ in github.calls))

    def test_classic_patch_preserves_users_teams_apps(self):
        github = GitHub()
        github.classic = {'required_approving_review_count': 2, 'bypass_pull_request_allowances':
                          {'users': [{'login': 'existing-user'}], 'teams': [{'slug': 'existing-team'}], 'apps': [{'slug': 'existing-app'}]}}
        step = rules.inspect(github, github.target, 7)['steps'][0]
        rules.apply(github, github.target, step, 7)
        write = next(row for row in github.calls if row[0] == 'PATCH')
        self.assertEqual(write[2], {'bypass_pull_request_allowances': {'users': ['existing-user'], 'teams': ['existing-team'], 'apps': ['existing-app', 'forge-controller']}})


class BaselineRepairTests(unittest.TestCase):
    def github(self):
        github = GitHub()
        github.value['name'] = 'Forge review baseline'
        github.value['bypass_actors'].extend([
            {'actor_id': 6, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'},
            {'actor_id': 5, 'actor_type': 'RepositoryRole', 'bypass_mode': 'always'}])
        github.value['rules'].extend([
            {'type': 'required_status_checks', 'parameters': {
                'strict_required_status_checks_policy': True, 'do_not_enforce_on_create': False,
                'required_status_checks': [{'context': 'forge/merge-policy', 'integration_id': 6},
                                           {'context': 'ci/test', 'integration_id': 101}]}},
            {'type': 'non_fast_forward'}, {'type': 'required_linear_history'}])
        github.value['rules'][0]['parameters'].update({
            'required_approving_review_count': 3, 'dismiss_stale_reviews_on_push': True,
            'require_code_owner_review': True, 'require_last_push_approval': True,
            'required_review_thread_resolution': True,
            'allowed_merge_methods': ['squash'],
            'require_extra_approval_for_unattributed_changes': True,
            'required_reviewers': [],
            'dismissal_restriction': {'enabled': False, 'allowed_actors': []}})
        return github

    def test_reinstall_rebinds_only_forge_check_and_preserves_all_other_protections(self):
        github = self.github()
        before = deepcopy(github.value)
        plan = rules.inspect(github, github.target, 7)
        self.assertFalse(plan['blockers'])
        self.assertEqual(len(plan['steps']), 1)
        self.assertEqual(plan['steps'][0]['kind'], 'github.ruleset.repair_baseline')
        rules.apply(github, github.target, plan['steps'][0], 7)
        expected = deepcopy(before)
        expected['rules'][1]['parameters']['required_status_checks'][0]['integration_id'] = 7
        expected['bypass_actors'].append({'actor_id': 7, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'})
        self.assertEqual(github.value, expected)
        payload = next(body for method, _, body in github.calls if method == 'PUT')
        self.assertNotIn('id', payload)
        self.assertFalse(rules.inspect(github, github.target, 7)['steps'])

    def test_effective_check_is_inspected_without_required_reviews(self):
        github = self.github()
        github.rule = {**github.value['rules'][1], 'ruleset_id': 42, 'ruleset_source_type': 'Repository'}
        github.value['rules'][0]['parameters']['required_approving_review_count'] = 0
        self.assertEqual(rules.inspect(github, github.target, 7)['steps'][0]['kind'], 'github.ruleset.repair_baseline')

    def test_manual_inherited_special_and_unknown_sources_refuse_rebinding(self):
        for change in ('name', 'organization', 'unknown', 'any_app', 'missing_bypass', 'conditions', 'unknown_parameter', 'unknown_rule'):
            with self.subTest(change=change):
                github = self.github()
                if change == 'name': github.value['name'] = 'Manual protections'
                if change == 'organization': github.rule['ruleset_source_type'] = 'Organization'
                if change == 'unknown': github.value['rules'][1]['parameters']['required_status_checks'][0].pop('integration_id')
                if change == 'any_app': github.value['rules'][1]['parameters']['required_status_checks'][0]['integration_id'] = -1
                if change == 'missing_bypass': github.value['bypass_actors'] = []
                if change == 'conditions': github.value['conditions']['ref_name']['include'] = ['refs/heads/*']
                if change == 'unknown_parameter': github.value['rules'][0]['parameters']['future_protection'] = True
                if change == 'unknown_rule': github.value['rules'].append({'type': 'future_rule'})
                plan = rules.inspect(github, github.target, 7)
                self.assertEqual(plan['blockers'][0]['code'], 'merge_check_source_repair_refused')
                self.assertFalse(plan['steps'])
                self.assertTrue(all(method == 'GET' for method, _, _ in github.calls))

    def test_no_signing_bypass_is_added(self):
        github = self.github()
        github.value['rules'].append({'type': 'required_signatures'})
        plan = rules.inspect(github, github.target, 7)
        self.assertEqual(plan['blockers'][0]['code'], 'signing_bypass_refused')
        self.assertFalse(plan['steps'])
        github.value['bypass_actors'].append({'actor_id': 7, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'})
        plan = rules.inspect(github, github.target, 7)
        rules.apply(github, github.target, plan['steps'][0], 7)
        self.assertIn({'type': 'required_signatures'}, github.value['rules'])

    def test_rule_drift_refuses_write_and_readback_covers_untouched_parameters(self):
        github = self.github()
        step = rules.inspect(github, github.target, 7)['steps'][0]
        github.value['rules'][0]['parameters']['required_approving_review_count'] = 4
        with self.assertRaises(SetupError) as caught:
            rules.apply(github, github.target, step, 7)
        self.assertEqual(caught.exception.code, 'plan_stale')
        self.assertFalse(any(method == 'PUT' for method, _, _ in github.calls))
        github = self.github()
        step = rules.inspect(github, github.target, 7)['steps'][0]
        rules.apply(github, github.target, step, 7)
        github.value['rules'][0]['parameters']['require_code_owner_review'] = False
        self.assertFalse(rules.confirmed(github, github.target, step))

    def test_unobserved_write_and_edited_plan_are_refused(self):
        from unittest.mock import patch
        github = self.github()
        step = rules.inspect(github, github.target, 7)['steps'][0]
        request = github._request
        def ignored_write(method, path, payload=None):
            return None if method == 'PUT' else request(method, path, payload)
        with patch.object(github, '_request', side_effect=ignored_write):
            with self.assertRaises(SetupError) as caught:
                rules.apply(github, github.target, step, 7)
        self.assertEqual(caught.exception.code, 'github_write_unconfirmed')
        step['after']['rules'][0]['parameters']['required_approving_review_count'] = 0
        with self.assertRaises(SetupError) as caught:
            rules.apply(github, github.target, step, 7)
        self.assertEqual(caught.exception.code, 'plan_changed')
        self.assertFalse(any(method == 'PUT' for method, _, _ in github.calls))

    def test_uncertain_write_is_confirmed_without_repeating(self):
        github = self.github(); github.lose_response = True
        step = rules.inspect(github, github.target, 7)['steps'][0]
        with self.assertRaises(SetupError): rules.apply(github, github.target, step, 7)
        self.assertTrue(rules.confirmed(github, github.target, step))
        self.assertEqual(sum(method == 'PUT' for method, _, _ in github.calls), 1)

    def test_preparation_policies_and_current_installation_access(self):
        from forge_client.preparation import Preparation
        from forge_client.contracts import connection_identity
        for policy in ('apply-if-admin', 'print-commands', 'skip'):
            with self.subTest(policy=policy):
                github = self.github()
                observed = {'version': 1, 'repository': {'id': 10, 'name': 'owner/project'}, 'revision': 'a'*64,
                    'roles': {role: {'app_id': 7, 'installation_ids': [107], 'access': 'ready'}
                              for role in ('build', 'review', 'audit', 'controller')},
                    'readiness': {'discovered': True, 'enabled': False, 'access_ready': True,
                                  'protection_ready': False, 'protection_status': 'needs_setup'},
                    'baseline': {'branch': 'main', 'required_approvals': 2, 'dismiss_stale_reviews': True}}
                class Forge:
                    def authorize(self, *args): pass
                    def requirements(self, target): return deepcopy(observed)
                    def verify(self, *args): observed['readiness'].update(protection_ready=True, protection_status='ready')
                preparation = Preparation(github, Forge(), instance='instance', generation='generation',
                                          apps=connection_identity(observed), repair=policy, clock=lambda: 100)
                plan = preparation.plan('owner/project')
                if policy == 'apply-if-admin':
                    self.assertEqual(preparation.apply(plan)['status'], 'ready')
                    self.assertEqual(sum(method == 'PUT' for method, _, _ in github.calls), 1)
                else:
                    self.assertFalse(plan['steps'])
                    self.assertEqual(preparation.apply(plan)['status'], 'blocked')
                    self.assertEqual(plan['blockers'][0]['code'], 'rules_repair_not_authorized')
                    self.assertFalse(any(method == 'PUT' for method, _, _ in github.calls))
        github = self.github()
        observed['roles']['controller']['access'] = 'missing_access'
        observed['readiness'].update(access_ready=False, protection_ready=False, protection_status='needs_setup')
        preparation = Preparation(github, Forge(), instance='instance', generation='generation',
                                  apps=connection_identity(observed), clock=lambda: 100)
        self.assertEqual([step['kind'] for step in preparation.plan('owner/project')['steps']],
                         ['github.installation.add_repository'])
