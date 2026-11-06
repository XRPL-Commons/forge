"""Human choices preserve the same explicit intent and never apply installation."""
from unittest import TestCase
from unittest.mock import Mock

from forge_client.contracts import SetupError
from forge_client.github import GitHub
from forge_client.install_interview import interview


class InterviewTests(TestCase):
    def run_interview(self, answers, *, repositories=None, ssh='anvil'):
        github = Mock()
        github.identity.return_value = {'id': 1, 'login': 'owner'}
        github.installation_repositories.return_value = repositories or ['owner/one', 'owner/two']
        prompts, output = [], []
        iterator = iter(answers)
        def ask(prompt):
            prompts.append(prompt)
            return next(iterator)
        result = interview(ssh, github, ask=ask, tell=output.append)
        self.assertEqual(next(iterator, None), None, 'Unused answers')
        return result, prompts, '\n'.join(output), github

    def test_default_path_is_six_questions_and_does_not_grant_workflow_access(self):
        result, prompts, output, github = self.run_interview(['', '', '1,2', '', '', ''])
        self.assertEqual(len(prompts), 6)
        self.assertEqual(result['providers'], [{'id': 'codex', 'kind': 'browser'}])
        self.assertEqual(result['operators'], ['owner'])
        self.assertEqual(result['smoke_target'], 'scratch')
        self.assertIsNone(result['host_key'])
        self.assertEqual(result['hostname'], 'anvil')
        self.assertEqual(result['repositories'], [
            {'name': 'owner/one', 'merge_policy': 'human', 'allow_workflow_edits': False},
            {'name': 'owner/two', 'merge_policy': 'human', 'allow_workflow_edits': False}])
        self.assertNotIn('rules_repair', result)
        self.assertIn('two private test repositories', output)
        self.assertIn('approval before merge', output)
        self.assertIn('existing GitHub rules', output)
        self.assertIn('existing known_hosts', output)
        self.assertEqual([call[0] for call in github.method_calls], ['identity', 'installation_repositories'])

    def test_invalid_choices_retry_without_broadening_selection(self):
        result, _, output, _ = self.run_interview([
            '', '', '100', 'other/repo', '1 1', 'one owner/two', '9', '2', 'bad-key', '', ''])
        self.assertEqual(result['providers'], [{'id': 'claude', 'kind': 'browser'}])
        self.assertEqual([row['name'] for row in result['repositories']], ['owner/one', 'owner/two'])
        self.assertIn('CLI before the browser handoff', output)
        self.assertIn('full SHA256 fingerprint', output)

    def test_summary_edits_and_explicit_workflow_grant(self):
        result, _, output, _ = self.run_interview([
            '', '', '1', '', '', '1', 'New Factory',
            '5', '3', '2', '2', '6', ''])
        self.assertEqual(result['name'], 'New Factory')
        self.assertEqual(result['hostname'], 'new-factory')
        self.assertEqual(result['repositories'][0]['merge_policy'], 'auto')
        self.assertTrue(result['repositories'][0]['allow_workflow_edits'])
        self.assertIn('automatic merge; workflow edits allowed', output)
        self.assertIn('grant workflow-write permission', output)

    def test_editing_owner_reselects_repositories_and_smoke_target(self):
        result, _, _, _ = self.run_interview([
            '', '', '1', '', '', '5', '5', '2', '6',
            '2', 'elsewhere', 'new', ''])
        self.assertEqual(result['owner'], 'elsewhere')
        self.assertEqual(result['repositories'][0]['name'], 'elsewhere/new')
        self.assertEqual(result['smoke_target'], 'scratch')

    def test_discovery_failure_still_allows_explicit_names(self):
        github = Mock()
        github.identity.return_value = {'login': 'owner'}
        github.installation_repositories.side_effect = SetupError('github_unavailable', 'unavailable')
        answers = iter(['', '', 'project', '', '', ''])
        output = []
        result = interview('anvil', github, ask=lambda _: next(answers), tell=output.append)
        self.assertEqual(result['repositories'][0]['name'], 'owner/project')
        self.assertIn('discovery is unavailable', '\n'.join(output))

    def test_advanced_login_methods_operators_and_pinned_host_key(self):
        fingerprint = 'SHA256:' + 'a' * 43
        result, _, _, _ = self.run_interview([
            '', '', '1', '', fingerprint, '5', '2', 'owner colleague',
            '4', '2', '3', '1', 'custom-network', '6', '1', 'Renamed', ''])
        self.assertEqual(result['host_key'], fingerprint)
        self.assertEqual(result['operators'], ['owner', 'colleague'])
        self.assertEqual(result['hostname'], 'custom-network')
        self.assertEqual(result['providers'], [{'id': 'codex', 'kind': 'browser'}, {'id': 'claude', 'kind': 'token'}])


class RepositoryChoicesTests(TestCase):
    def test_discovery_is_read_only_and_excludes_other_owners_archived_and_disabled(self):
        github = GitHub(executable='gh')
        owner = {'id': 2, 'login': 'owner', 'type': 'User'}
        row = {'owner': {'id': 2}, 'full_name': 'owner/one', 'archived': False, 'disabled': False}
        github._request = Mock(side_effect=[owner, [row, {**row, 'owner': {'id': 3}},
            {**row, 'full_name': 'owner/archived', 'archived': True},
            {**row, 'full_name': 'owner/disabled', 'disabled': True}]])
        self.assertEqual(github.installation_repositories('owner'), ['owner/one'])
        self.assertEqual([call.args[0] for call in github._request.call_args_list], ['GET', 'GET'])
        self.assertEqual(github._request.call_args.args[1], '/user/repos?sort=pushed&per_page=100')

    def test_org_repositories_use_authenticated_org_inventory(self):
        github = GitHub(executable='gh')
        github._request = Mock(side_effect=[{'id': 2, 'login': 'org', 'type': 'Organization'}, []])
        self.assertEqual(github.installation_repositories('org'), [])
        self.assertEqual(github._request.call_args.args[1], '/orgs/org/repos?sort=pushed&per_page=100')
