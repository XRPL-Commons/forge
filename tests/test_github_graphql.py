import unittest
from unittest.mock import Mock

from forge.app_access import AccessError
from forge.factory.github import GitHub, GitHubUnavailable, GitHubBudgetExhausted


class GraphQLPublicationTests(unittest.TestCase):
    def setUp(self):
        self.client = Mock()
        self.github = GitHub(Mock(), {}, client=self.client)
        self.github.token = Mock(return_value='synthetic-test-token')
        self.repo = {'repo_id': 12, 'repo': 'example/project'}

    def test_uses_repo_scoped_host_token_and_returns_data(self):
        self.client.request.return_value = {'data': {'createCommitOnBranch': {'commit': {'oid': 'a' * 40}}}}
        data = self.github.graphql('controller', self.repo, 'mutation Example {}', {'input': {'expectedHeadOid': 'b' * 40}})
        self.assertEqual(data, self.client.request.return_value['data'])
        self.github.token.assert_called_once_with('controller', 12, extra=None)
        self.assertEqual(self.client.request.call_args.args[:2], ('POST', '/graphql'))

    def test_partial_data_and_http_success_do_not_confirm_publication(self):
        for value in ({'data': {'commit': {}}, 'errors': [{'type': 'FORBIDDEN', 'message': 'private details'}]},
                      {'errors': [{'type': 'FORBIDDEN private details'}]}, {}, None):
            self.client.request.return_value = value
            with self.subTest(value=value), self.assertRaises(GitHubUnavailable) as caught:
                self.github.graphql('controller', self.repo, 'mutation Example {}', {})
            self.assertNotIn('private details', str(caught.exception))
            self.assertNotIn('private details', repr(caught.exception.error_types))

    def test_http_failure_preserves_status_without_diagnostic_body(self):
        self.client.request.side_effect = AccessError('private response details', status=503)
        with self.assertRaises(GitHubUnavailable) as caught:
            self.github.graphql('controller', self.repo, 'mutation Example {}', {})
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn('private', str(caught.exception))

    def test_deadline_prevents_token_and_network_calls(self):
        self.github.deadline = 0
        with self.assertRaises(GitHubBudgetExhausted):
            self.github.graphql('controller', self.repo, 'mutation Example {}', {})
        self.github.token.assert_not_called()
        self.client.request.assert_not_called()


class IssueInputTests(unittest.TestCase):
    def test_notes_and_requirements_use_same_comment_observation(self):
        github = GitHub(Mock(), {}, client=Mock())
        github.call = Mock(return_value={'number': 7, 'title': 'A', 'body': 'B'})
        comments = [{'id': 1, 'body': 'human decision', 'user': {'type': 'User'}}]
        github.pages = Mock(return_value=comments)
        value, digest, observed = github.issue_inputs({'repo_id': 12, 'repo': 'example/project', 'number': 7})
        github.pages.assert_called_once()
        self.assertIs(observed, comments)
        self.assertEqual(value['number'], 7)
        self.assertEqual(len(digest), 64)
