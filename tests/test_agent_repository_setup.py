"""Exercise the local administrator/remote App boundary without network access."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock

from forge.repository_requirements import describe
from forge.repository_setup import RepositorySetup, ruleset
from forge.settings import digest, write_json
from forge_client.contracts import SetupError, baseline, connection_identity, requirements
from forge_client.github import GitHub, run_bounded
from forge_client.preparation import Preparation
from tests import test_settings as settings_tests
from forge.settings import ROLES

SECRET = 'synthetic-operator-credential-not-for-forge'


class BoundaryTests(unittest.TestCase):
    def setUp(self):
        settings_tests.SettingsTests.setUp(self)
        self.repo = {'id': 10, 'full_name': 'example/project', 'owner': {'id': 50, 'type': 'Organization'},
                     'default_branch': 'main', 'archived': False, 'disabled': False, 'permissions': {'admin': True},
                     'unused_credential_field': SECRET}
        self.granted = set()
        self.github_rules = []
        self.writes = []
        self.requests = []
        self.authorized = True
        self.capabilities = {'repositories:read', 'repositories:prepare', 'repositories:activate'}
        def discover(connection):
            role = ROLES[connection['app_id'] - 1]
            return {'repositories': [{'id': 10, 'name': 'example/project', 'archived': False}] if connection['app_id'] in self.granted else [],
                    'permissions': self.config_permissions(role), 'account': 'example', 'app_slug': 'example-' + role,
                    'installation_url': 'https://github.com/organizations/example/settings/installations/101'}
        self.client.discover.side_effect = discover
        self.settings.refresh()
        self.target = {'id': 10, 'name': 'example/project'}
        self.app_github = Mock()
        self.app_github.return_value.target.side_effect = lambda _: self.repo['default_branch']
        self.app_github.return_value.pages.return_value = []
        self.app_github.return_value.protection_report.side_effect = lambda *_: {
            'repo_id': 10, 'branch': self.repo['default_branch'], 'available': True,
            'requirements': [{'id': 'reviews', 'passed': bool(self.github_rules)},
                             {'id': 'stale', 'passed': bool(self.github_rules)}]}
        self.service = RepositorySetup(self.settings, github_class=self.app_github)
        test = self
        class Forge:
            def authorize(self, instance, generation, target, capability):
                test.requests.append(('authorize', instance, generation, target, capability))
                if capability not in test.capabilities:
                    raise SetupError('unauthorized', 'Capability is not granted.')
                if not test.authorized:
                    raise SetupError('unauthorized', 'Client access was revoked.')
                if (instance, generation, target) != ('instance', 'generation', test.target):
                    raise SetupError('unauthorized', 'Target or instance is not authorized.')
            def requirements(self, target):
                test.requests.append(('requirements', target))
                return describe(test.settings, target)
            def verify(self, target, revision):
                test.requests.append(('verify', target, revision))
                if digest(test.settings.load()) != revision:
                    raise SetupError('revision_changed', 'Revision changed.')
                test.settings.refresh()
                test.service.check(target['id'], revision)
            def enable(self, target, revision):
                test.requests.append(('enable', target, revision))
                test.service.enable(target['id'], revision)
        self.remote = Forge()
        self.broker = GitHub(run=self.run_gh, executable='/test/bin/gh')
        self.preparation = Preparation(self.broker, self.remote, instance='instance', generation='generation',
                                       apps=connection_identity(describe(self.settings, self.target)), clock=lambda: self.now)

    @staticmethod
    def config_permissions(role):
        from forge.app_access import ROLE_PERMISSIONS
        return ROLE_PERMISSIONS[role]

    def run_gh(self, argv, *, payload=None):
        self.assertEqual(argv[:4], ['/test/bin/gh', 'api', '--hostname', 'github.com'])
        self.assertNotIn(SECRET, str(argv))
        method = argv[argv.index('--method') + 1]
        path = argv[10]
        if method != 'GET':
            self.writes.append((method, path, payload))
        if method == 'GET' and path == '/repos/example/project':
            return json.dumps(self.repo).encode()
        if method == 'GET' and path.startswith('/repos/example/project/rulesets?'):
            return json.dumps(self.github_rules).encode()
        if method == 'PUT' and path.startswith('/user/installations/'):
            self.granted.add(int(path.split('/')[3]) - 100)
            return b''
        if method == 'POST' and path == '/repos/example/project/rulesets':
            self.github_rules.append({'id': 75, 'name': payload['name'], 'source_type': 'Repository',
                                      'unrelated_secret': SECRET})
            return b'{"id":75}'
        raise AssertionError((method, path))

    def test_one_setup_request_grants_checks_prepares_and_enables(self):
        result = self.preparation.setup('example/project', apply=True, enable=True)
        self.assertEqual(result['status'], 'enabled')
        self.assertEqual(len(self.writes), 5)
        self.assertEqual(len(result['completed']), 5)
        self.assertTrue(self.settings.view()['repositories'][0]['dispatch_allowed'])

    def test_plan_uses_one_clock_sample_for_its_lifetime(self):
        import itertools
        self.preparation.clock = lambda: next(ticks)
        ticks = itertools.count(1000, 0.1)
        plan = self.preparation.plan('example/project')
        self.assertEqual(plan['expires_at'] - plan['created_at'], 600)
        self.assertEqual(self.preparation.apply(plan)['status'], 'verification_pending')

    def test_default_setup_only_plans_even_if_enable_is_requested(self):
        result = self.preparation.setup('example/project', enable=True)
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(self.writes, [])
        self.assertFalse(any(r[0] in ('enable', 'verify') for r in self.requests))

    def test_stronger_existing_rules_are_preserved(self):
        self.granted = {1, 2, 3, 4}
        self.github_rules = [{'id': 65, 'name': 'Organization reviews', 'source_type': 'Organization'}]
        self.settings.refresh(); self.service.check(10)
        before = deepcopy(self.github_rules)
        self.assertEqual(self.preparation.setup('example/project', apply=True)['status'], 'ready')
        self.assertEqual(self.github_rules, before)
        self.assertEqual(self.writes, [])

    def test_contradictory_readiness_is_rejected(self):
        value = describe(self.settings, self.target)
        for field in ('enabled', 'access_ready', 'protection_ready'):
            contradictory = deepcopy(value)
            contradictory['readiness'][field] = True
            with self.assertRaises(SetupError): requirements(contradictory)

    def test_changed_server_schema_cannot_supply_a_privileged_payload(self):
        original = self.remote.requirements
        for field, value in (('command', 'run something'), ('token', SECRET), ('endpoint', 'https://attacker.test')):
            def hostile(target, field=field, value=value):
                return {**original(target), field: value}
            self.remote.requirements = hostile
            with self.assertRaises(SetupError):
                self.preparation.plan('example/project')
        self.remote.requirements = original
        self.assertEqual(self.writes, [])

    def test_read_only_scope_cannot_start_local_writes(self):
        self.capabilities = {'repositories:read'}
        plan = self.preparation.plan('example/project')
        with self.assertRaisesRegex(SetupError, 'Capability'):
            self.preparation.apply(plan)
        self.assertEqual(self.writes, [])

    def test_missing_activation_scope_fails_before_preparation(self):
        self.capabilities.remove('repositories:activate')
        with self.assertRaisesRegex(SetupError, 'Capability'):
            self.preparation.setup('example/project', apply=True, enable=True)
        self.assertEqual(self.writes, [])

    def test_invalid_plan_version_does_not_dispatch(self):
        plan = self.preparation.plan('example/project'); plan['version'] = 2
        from forge_client.contracts import digest as hash_plan
        plan['id'] = hash_plan({k: v for k, v in plan.items() if k != 'id'})
        with self.assertRaises(SetupError): self.preparation.apply(plan)
        self.assertEqual(self.writes, [])

    def test_multiple_installations_require_resolution(self):
        config = self.settings.load()
        config['connections']['build'].pop('installation_id')
        config['connections']['build']['installation_ids'] = [101, 201]
        write_json(self.settings.path, config)
        self.preparation.apps = connection_identity(describe(self.settings, self.target))
        plan = self.preparation.plan('example/project')
        self.assertEqual(plan['blockers'], [{'code': 'app_access_unverified', 'role': 'build'}])
        self.assertEqual(self.preparation.apply(plan)['status'], 'blocked')
        self.assertEqual(self.writes, [])

    def prepare_access(self):
        plan = self.preparation.plan('example/project')
        self.assertEqual(len(plan['steps']), 4)
        result = self.preparation.apply(plan)
        self.assertEqual(result['status'], 'verification_pending')
        self.assertEqual(len(self.writes), 4)

    def test_undiscovered_repository_reaches_enabled_through_independent_verification(self):
        initial = describe(self.settings, self.target)
        self.assertFalse(initial['readiness']['discovered'])
        self.assertNotIn('private_key_env', json.dumps(initial))
        self.prepare_access()
        self.assertEqual(self.settings.load()['repositories'], [])
        plan = self.preparation.plan('example/project')
        self.assertEqual(plan['steps'], [{'kind': 'github.ruleset.ensure_baseline'}])
        result = self.preparation.apply(plan, enable=True)
        self.assertEqual(result['status'], 'enabled')
        self.assertEqual(self.writes[-1][2], ruleset('main', 4))
        self.assertTrue(self.settings.view()['repositories'][0]['dispatch_allowed'])
        # A repeat does no additional GitHub administrative writes.
        repeated = self.preparation.apply(self.preparation.plan('example/project'), enable=True)
        self.assertEqual(repeated['status'], 'enabled')
        self.assertEqual(len(self.writes), 5)
        self.assertNotIn(SECRET, json.dumps(self.requests))
        self.assertNotIn(SECRET, json.dumps([initial, plan, result, repeated]))
        for path in self.root.rglob('*'):
            if path.is_file():
                self.assertNotIn(SECRET, path.read_text())

    def test_preparation_requires_explicit_enable(self):
        self.prepare_access()
        result = self.preparation.apply(self.preparation.plan('example/project'))
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(self.settings.load()['repositories'], [])
        self.assertFalse(any(r[0] == 'enable' for r in self.requests))

    def test_app_token_references_and_provider_credentials_are_not_requirements(self):
        view = describe(self.settings, self.target)
        for word in ('private_key', 'token', SECRET, 'config_file'):
            self.assertNotIn(word, json.dumps(view))
        self.assertEqual(set(view['roles']), set(ROLES))

    def test_unconfigured_apps_cannot_be_planned_as_access_grants(self):
        config = self.settings.load(); config['connections'].pop('build'); write_json(self.settings.path, config)
        value = describe(self.settings, self.target)
        self.assertEqual(value['roles']['build'], {'app_id': None, 'installation_ids': [], 'access': 'not_configured'})

    def test_rejected_grant_returns_browser_action_without_child_error_text(self):
        self.broker.grant_repository = Mock(side_effect=SetupError('github_request_failed', 'Sanitized failure.'))
        result = self.preparation.apply(self.preparation.plan('example/project'))
        self.assertEqual(result['status'], 'needs_user_action')
        self.assertEqual(result['next_actions'][0]['url'], 'https://github.com/organizations/example/settings/installations/101')
        self.assertEqual(self.writes, [])

    def test_changed_app_identity_cannot_receive_admin_grants(self):
        plan = self.preparation.plan('example/project')
        config = self.settings.load(); config['connections']['build']['app_id'] = 99
        write_json(self.settings.path, config)
        with self.assertRaisesRegex(SetupError, 'App identities changed'):
            self.preparation.apply(plan)
        self.assertEqual(self.writes, [])

    def test_revocation_between_local_writes_stops_remaining_steps(self):
        original = self.broker.grant_repository
        def revoke(*args):
            original(*args); self.authorized = False
        self.broker.grant_repository = revoke
        with self.assertRaisesRegex(SetupError, 'revoked'):
            self.preparation.apply(self.preparation.plan('example/project'))
        self.assertEqual(len(self.writes), 1)

    def test_edited_plan_cannot_add_arbitrary_actions_even_with_recomputed_digest(self):
        for attack in ({'kind': 'shell', 'command': 'echo forbidden'},
                       {'kind': 'github.installation.add_repository', 'role': 'build', 'installation_id': 999}):
            plan = self.preparation.plan('example/project'); plan['steps'][0] = attack
            from forge_client.contracts import digest as hash_plan
            plan['id'] = hash_plan({k: v for k, v in plan.items() if k != 'id'})
            with self.assertRaises(SetupError):
                self.preparation.apply(plan)
        self.assertEqual(self.writes, [])

    def test_expired_plan_and_changed_repository_stop_before_writes(self):
        plan = self.preparation.plan('example/project'); self.now += 601
        with self.assertRaisesRegex(SetupError, 'expired'):
            self.preparation.apply(plan)
        self.now -= 601
        for field, value in (('id', 11), ('default_branch', 'next')):
            original = self.repo[field]; self.repo[field] = value
            with self.assertRaises(SetupError):
                self.preparation.apply(plan)
            self.repo[field] = original
        self.assertEqual(self.writes, [])

    def test_preserves_existing_baseline_on_conflict(self):
        self.prepare_access()
        self.github_rules.append({'id': 91, 'name': 'Forge review baseline', 'source_type': 'Organization'})
        plan = self.preparation.plan('example/project')
        self.assertEqual(plan['blockers'], [{'code': 'ruleset_conflict', 'ruleset_ids': [91]}])
        self.assertEqual(self.preparation.apply(plan)['status'], 'blocked')
        self.assertEqual(len(self.writes), 4)
        self.assertEqual(self.github_rules[0]['source_type'], 'Organization')

    def test_uncertain_rule_create_is_not_retried(self):
        self.prepare_access()
        original = self.broker.create_baseline
        def uncertain(*args):
            original(*args)
            raise SetupError('github_timeout', 'Timeout.')
        self.broker.create_baseline = uncertain
        plan = self.preparation.plan('example/project')
        self.assertEqual(self.preparation.apply(plan)['status'], 'write_unconfirmed')
        with self.assertRaises(SetupError):
            self.preparation.apply(plan)
        self.assertEqual(len(self.writes), 5)
        self.remote.verify(self.target, plan['revision'])
        self.assertEqual(self.preparation.apply(self.preparation.plan('example/project'))['status'], 'ready')
        self.assertEqual(len(self.writes), 5)

    def test_enable_remains_subject_to_real_server_checks(self):
        revision = digest(self.settings.load())
        with self.assertRaises(ValueError):
            self.remote.enable(self.target, revision)
        self.assertEqual(self.settings.load()['repositories'], [])

    def test_requirements_do_not_initialize_or_activate_a_repository(self):
        before = self.settings.load()
        describe(self.settings, self.target)
        self.assertEqual(self.settings.load(), before)
        self.assertFalse((self.root / 'state' / 'factory-v2.json').exists())


class ContractTests(unittest.TestCase):
    def test_baseline_matches_existing_server_policy(self):
        for branch in ('main', 'release/v2', 'feature.some-change'):
            self.assertEqual(baseline(branch, 4), ruleset(branch, 4))

    def test_branch_patterns_and_bad_identifiers_are_rejected(self):
        from forge_client.contracts import repository
        for name in ('//github.com/evil', 'owner/repo?x=1', 'owner/repo/../../other', 'o/..'):
            with self.assertRaises(SetupError): repository({'id': 1, 'name': name})
        for identifier in (True, 0, -1, 2**53, '1'):
            with self.assertRaises(SetupError): repository({'id': identifier, 'name': 'o/r'})
        for branch in ('*', 'a[bc]', 'a\nb', '../main', ''):
            with self.assertRaises(SetupError): baseline(branch, 4)


class BrokerTests(unittest.TestCase):
    def test_diagnostics_are_sanitized_and_output_is_bounded(self):
        for script in (f'import sys;sys.stderr.write({SECRET!r});sys.exit(1)',
                       f'import sys;sys.stdout.write({SECRET!r});sys.exit(1)'):
            with self.assertRaises(SetupError) as error:
                run_bounded([sys.executable, '-c', script])
            self.assertNotIn(SECRET, str(error.exception))
        with self.assertRaisesRegex(SetupError, 'size limit'):
            run_bounded([sys.executable, '-c', 'import sys;sys.stdout.write("x"*5000000)'])
        with self.assertRaisesRegex(SetupError, 'timed out'):
            run_bounded([sys.executable, '-c', 'import time;time.sleep(2)'], timeout=0.05)

    def test_environment_is_local_and_debug_proxy_injection_is_removed(self):
        env = {'GH_TOKEN': SECRET, 'GH_DEBUG': 'api', 'GH_HOST': 'attacker.test', 'FORGE_TOKEN': SECRET,
               'HTTPS_PROXY': 'http://attacker.test', 'BASH_ENV': '/bad', 'HOME': '/tmp'}
        value = json.loads(run_bounded([sys.executable, '-c', 'import os,json;print(json.dumps(dict(os.environ)))'], environ=env))
        self.assertEqual(value['GH_TOKEN'], SECRET)
        self.assertEqual(value['GH_HOST'], 'github.com')
        self.assertEqual(value['GH_DEBUG'], '')
        for key in ('FORGE_TOKEN', 'HTTPS_PROXY', 'BASH_ENV'):
            self.assertNotIn(key, value)

    def test_bad_json_does_not_echo_response(self):
        broker = GitHub(run=lambda *a, **k: SECRET.encode(), executable='gh')
        with self.assertRaises(SetupError) as error:
            broker.repository('example/project')
        self.assertNotIn(SECRET, str(error.exception))

    def test_local_client_imports_no_server_modules(self):
        result = subprocess.run([sys.executable, '-c',
            'import forge_client.preparation,forge_client.github,sys; assert not any(x == "forge" or x.startswith("forge.") for x in sys.modules)'], capture_output=True)
        self.assertEqual(result.returncode, 0)


if __name__ == '__main__': unittest.main()

class ControllerBaselineTests(unittest.TestCase):
    def test_invalid_controller_identity_cannot_produce_a_ruleset(self):
        for app_id in (None, True, 0, -1, '4959034', 2**53):
            with self.subTest(app_id=app_id), self.assertRaises(ValueError):
                ruleset('main', app_id)

    def test_only_pinned_controller_gets_pull_request_bypass(self):
        for app_id in (4, 4959034):
            value = baseline('main', app_id)
            self.assertEqual(value, ruleset('main', app_id))
            self.assertEqual(value['bypass_actors'], [{'actor_id': app_id, 'actor_type': 'Integration', 'bypass_mode': 'pull_request'}])
