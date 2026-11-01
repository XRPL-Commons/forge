"""Supervisor-owned policy needs no GitHub protection mode or paid plan."""
import unittest
import json
import sys
from unittest.mock import Mock, patch

from forge_client.contracts import SetupError, digest
from forge_client.merge_policy import execute
from forge_client.preflight import repository
from forge_client.installation import intent
from tests.test_install_coordinator import interview


class SupervisorProtectionContractsTests(unittest.TestCase):
    def test_explicit_retired_policy_is_rejected_without_network_calls(self):
        client = Mock()
        with self.assertRaises(SetupError) as error:
            execute(client, {'id': 1, 'name': 'one/repo'}, github_protection='optional')
        self.assertEqual(error.exception.code, 'github_protection_retired')
        client.request.assert_not_called()
        from forge_client.cli import parser, repository_commands
        import io
        with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            parser().parse_args(['repo', 'setup', 'one/repo', '--server', 'https://forge.example', '--github-protection', 'optional'])
        self.assertNotIn('github_protection', repository_commands()['setup']['input_schema']['properties'])

    def test_new_intent_omits_settings_while_legacy_intent_keeps_its_digest(self):
        github = Mock()
        github.registration_owner.side_effect = lambda login: {'id': 2, 'login': login, 'type': 'User'}
        github.repository.side_effect = lambda name: {'id': 10 if name.endswith('one') else 11, 'name': name, 'owner_id': 2}
        original = intent(interview(), github)
        self.assertNotIn('rules_repair', original)
        self.assertTrue(all('github_protection' not in row for row in original['repositories']))
        chosen = interview(); chosen['repositories'][0]['github_protection'] = 'required'
        chosen['rules_repair'] = 'apply-if-admin'
        legacy = intent(chosen, github)
        self.assertEqual(legacy['rules_repair'], 'apply-if-admin')
        self.assertEqual(legacy['repositories'][0]['github_protection'], 'required')
        self.assertEqual(digest(legacy), digest(intent(chosen, github)))

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

    def test_preflight_accepts_unavailable_plan_without_a_setting_and_preserves_other_errors(self):
        target = {'id': 10, 'name': 'one/repo', 'branch': 'main', 'admin': True}
        github = Mock(); github.repository.return_value = target
        def get(method, path):
            if '/protection' in path: raise SetupError('github_plan_required', 'Plan required')
            return []
        github._request.side_effect = get
        with patch('forge.factory.environments.probe', return_value={'head': 'a'*40, 'services': []}), \
             patch('forge_client.preflight.branch_rules', side_effect=SetupError('github_plan_required', 'Plan required')):
            observed = repository(github, target)
            self.assertFalse(observed['classic_protection'])
        with patch('forge.factory.environments.probe', return_value={'head': 'a'*40, 'services': []}), \
             patch('forge_client.preflight.branch_rules', side_effect=SetupError('github_request_failed', 'Access denied')):
            with self.assertRaises(SetupError): repository(github, target)
