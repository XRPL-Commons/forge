"""External rules stay read-only and retired setup paths cannot mutate them."""
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


class ObservationTests(unittest.TestCase):
    def test_existing_reviews_never_plan_a_bypass_or_admin_requirement(self):
        github = GitHub(); github.admin = False; github.target['admin'] = False
        github.rule['ruleset_source_type'] = 'Organization'
        plan = rules.inspect(github, github.target, 7)
        self.assertEqual(plan['steps'], [])
        self.assertEqual(plan['blockers'], [])
        self.assertEqual(plan['sources'], [{'id': 42, 'type': 'Organization'}])
        self.assertTrue(all(method == 'GET' for method, _, _ in github.calls))
        self.assertFalse(any('/memberships/' in path for _, path, _ in github.calls))

    def test_retired_repair_and_baseline_entrypoints_never_contact_github(self):
        from unittest.mock import Mock
        from forge_client.github import GitHub as Broker
        github = Mock()
        for kind in ('github.ruleset.ensure_baseline', 'github.ruleset.repair_bypass',
                     'github.ruleset.repair_baseline', 'github.classic.repair_bypass'):
            with self.subTest(kind=kind), self.assertRaises(SetupError) as error:
                rules.apply(github, {}, {'kind': kind}, 7)
            self.assertEqual(error.exception.code, 'github_protection_retired')
        github._request.assert_not_called()
        with self.assertRaises(SetupError): Broker.create_baseline(github, 'owner/project', 'main', 7)
        github._request.assert_not_called()

    def test_preparation_verifies_without_rule_writes_or_admin_access(self):
        from forge_client.preparation import Preparation
        from forge_client.contracts import connection_identity
        github = GitHub(); github.target['admin'] = False
        observed = {'version': 1, 'repository': {'id': 10, 'name': 'owner/project'}, 'revision': 'a'*64,
                    'roles': {role: {'app_id': number, 'installation_ids': [number + 100], 'access': 'ready'}
                              for role, number in zip(('build', 'review', 'audit', 'controller'), (4, 5, 6, 7))},
                    'readiness': {'discovered': True, 'enabled': False, 'access_ready': True,
                                  'protection_ready': False, 'protection_status': 'unchecked'},
                    'merge_requirements': {'branch': 'main', 'merge_check_scope': 'all'}}
        class Forge:
            def authorize(self, *args): pass
            def requirements(self, target): return deepcopy(observed)
            def verify(self, *args): observed['readiness'].update(protection_ready=True, protection_status='ready')
        preparation = Preparation(github, Forge(), instance='instance', generation='generation',
            apps=connection_identity(observed), clock=lambda: 100, repair='apply-if-admin')
        plan = preparation.plan('owner/project')
        self.assertEqual(plan['steps'], [])
        self.assertEqual(plan['blockers'], [])
        self.assertEqual(preparation.apply(plan)['status'], 'ready')
        self.assertEqual(github.calls, [])
