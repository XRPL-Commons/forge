"""Explicit optional protection works through ordinary preparation and saved intent."""
from copy import deepcopy
import unittest
import json
import sys
from unittest.mock import Mock, patch

from forge_client.contracts import SetupError, connection_identity, digest, requirements
from forge_client.preparation import Preparation
from forge_client.merge_policy import execute
from forge_client.preflight import repository
from forge_client.installation import intent
from tests import test_agent_repository_setup as boundary
from tests.test_install_coordinator import interview


class OptionalPreparationTests(unittest.TestCase):
    def setUp(self):
        self.case = boundary.BoundaryTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        b = self.case
        b.capabilities.add('repositories:policy')
        def protection(target, action, **kw):
            self.assertEqual(set(target), {'id', 'name'})
            if action == 'read': return b.service.protection_policy(target)
            return b.service.set_github_protection(target, kw['revision'], kw['mode'])
        b.remote.protection = protection
        self.preparation = Preparation(b.broker, b.remote, instance='instance', generation='generation',
            apps=b.preparation.apps, clock=lambda: b.now, github_protection='optional')

    def test_policy_plan_is_read_only_and_applies_before_access_without_rules_writes(self):
        b = self.case
        plan = self.preparation.plan('example/project')
        self.assertEqual(plan['steps'][0]['kind'], 'forge.github_protection')
        self.assertFalse(b.writes)
        self.assertEqual(b.service.protection_policy(b.target)['mode'], 'required')
        # Optional reports carry the read-only observation requirement, rather
        # than a requirement to install Forge's GitHub baseline.
        b.app_github.return_value.protection_report.side_effect = lambda *_: {
            'repo_id': 10, 'branch': 'main', 'available': True,
            'github_protection': 'optional', 'merge_check_scope': 'all',
            'requirements': [{'id': 'readable', 'passed': True}]}
        result = self.preparation.setup('example/project', apply=True)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(b.service.protection_policy(b.target)['mode'], 'optional')
        self.assertEqual(len(b.writes), 4)
        self.assertTrue(all('/installations/' in path for _, path, _ in b.writes))
        self.assertEqual(self.preparation.setup('example/project', apply=True)['status'], 'ready')
        self.assertEqual(len(b.writes), 4)

    def test_policy_scope_and_stale_revision_prevent_mode_change(self):
        b = self.case
        b.capabilities.remove('repositories:policy')
        with self.assertRaises(SetupError): self.preparation.plan('example/project')
        b.capabilities.add('repositories:policy')
        plan = self.preparation.plan('example/project')
        b.service.set_github_protection(b.target, plan['revision'], 'required')
        with self.assertRaises(SetupError) as error: self.preparation.apply(plan)
        self.assertEqual(error.exception.code, 'plan_stale')
        self.assertEqual(b.service.protection_policy(b.target)['mode'], 'required')
        self.assertFalse(b.writes)


class OptionalContractsTests(unittest.TestCase):
    def test_cli_explicit_mode_pins_policy_revision(self):
        client = Mock(); client.request.side_effect = [{'revision': 'a'*64}, {'mode': 'optional'}]
        self.assertEqual(execute(client, {'id': 1, 'name': 'one/repo'}, github_protection='optional', request_id='b'*32), {'mode': 'optional'})
        self.assertEqual(client.request.call_args.kwargs, {'policy': 'github_protection', 'action': 'set', 'mode': 'optional', 'revision': 'a'*64, 'request_id': 'b'*32})
        with self.assertRaises(SetupError): execute(client, {'id': 1, 'name': 'one/repo'}, 'auto', github_protection='optional')
        from forge_client.cli import parser
        args = parser().parse_args(['repo', 'setup', 'one/repo', '--server', 'https://forge.example', '--github-protection', 'optional'])
        self.assertEqual(args.github_protection, 'optional')

    def test_omitted_install_field_preserves_intent_and_explicit_choice_is_bound(self):
        github = Mock()
        github.registration_owner.side_effect = lambda login: {'id': 2, 'login': login, 'type': 'User'}
        github.repository.side_effect = lambda name: {'id': 10 if name.endswith('one') else 11, 'name': name, 'owner_id': 2}
        original = intent(interview(), github)
        self.assertTrue(all('github_protection' not in row for row in original['repositories']))
        chosen = interview(); chosen['repositories'][0]['github_protection'] = 'optional'
        changed = intent(chosen, github)
        self.assertEqual(changed['repositories'][0]['github_protection'], 'optional')
        self.assertNotEqual(digest(original), digest(changed))
        chosen['repositories'][0]['github_protection'] = 'ignore'
        with self.assertRaises(SetupError): intent(chosen, github)

    def test_local_broker_classifies_only_known_plan_messages(self):
        from forge_client.github import run_bounded
        known = ('Upgrade to GitHub Pro or make this repository public to enable this feature.',
                 'Upgrade to GitHub Pro or make this repository public to enable this feature',
                 'Upgrade to GitHub Team or make this repository public to enable this feature.')
        for message in (*known, 'Resource not accessible by integration', 'API rate limit exceeded'):
            payload = json.dumps({'status': '403', 'message': message})
            with self.subTest(message=message), self.assertRaises(SetupError) as error:
                run_bounded([sys.executable, '-c', 'import sys;print(' + repr(payload) + ');sys.exit(1)'])
            self.assertEqual(error.exception.code, 'github_plan_required' if message in known else 'github_request_failed')

    def test_preflight_only_accepts_exact_unavailable_plan_in_optional_mode(self):
        target = {'id': 10, 'name': 'one/repo', 'branch': 'main', 'admin': True}
        github = Mock(); github.repository.return_value = target
        def get(method, path):
            if '/protection' in path: raise SetupError('github_plan_required', 'Plan required')
            return []
        github._request.side_effect = get
        with patch('forge.factory.environments.probe', return_value={'head': 'a'*40, 'services': []}), \
             patch('forge_client.preflight.branch_rules', side_effect=SetupError('github_plan_required', 'Plan required')):
            observed = repository(github, target, github_protection='optional')
            self.assertFalse(observed['classic_protection'])
            with self.assertRaises(SetupError): repository(github, target)
        with patch('forge.factory.environments.probe', return_value={'head': 'a'*40, 'services': []}), \
             patch('forge_client.preflight.branch_rules', side_effect=SetupError('github_request_failed', 'Access denied')):
            with self.assertRaises(SetupError): repository(github, target, github_protection='optional')
