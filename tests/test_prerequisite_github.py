"""Prerequisite reads bind installed repository identity and actual PR completion."""
import unittest
from unittest.mock import Mock

from forge.factory.github import GitHub, GitHubUnavailable
from forge.factory.prerequisites import blocker


class PrerequisiteGitHubTests(unittest.TestCase):
    def setUp(self):
        self.settings=Mock()
        self.settings.observation.return_value={'roles':{'controller':{'repositories':[{'id':22,'name':'other/repo'}]}}}
        self.github=GitHub(self.settings,{})
        self.responses={'':{'id':22},'/issues/7':{'id':100,'number':7,'state':'closed','state_reason':'completed','updated_at':'2026-10-10T00:00:00Z'}}
        self.github.call=Mock(side_effect=lambda role,item,method,suffix:self.responses[suffix])

    def test_pr_merge_uses_pull_endpoint_not_issue_closed_state(self):
        self.responses['/issues/7']['pull_request']={}
        self.github.pr=Mock(return_value={'id':200,'number':7,'merged':False,'base':{'repo':{'id':22}}})
        fact=self.github.prerequisite('other/repo#7')
        self.assertEqual(fact['github_id'],100)  # REST Issue ID differs from Pull Request ID.
        self.assertEqual(blocker(fact),'prerequisite_closed_unmerged')
        self.github.pr.return_value['merged']=True
        self.assertIsNone(blocker(self.github.prerequisite('other/repo#7')))

    def test_renamed_repository_uses_pinned_id(self):
        self.settings.observation.return_value['roles']['controller']['repositories'][0]['name']='new/name'
        binding={'repository_id':22,'github_id':100,'number':7,'kind':'issue'}
        self.assertEqual(self.github.prerequisite('old/name#7',binding)['repository_id'],22)
        self.assertEqual(self.github.call.call_args.args[1]['repo'],'new/name')
        self.assertEqual(self.github.prerequisite('old/name#7')['error'],'prerequisite_inaccessible')

    def test_inaccessible_target_never_becomes_completion(self):
        self.github.call.side_effect=GitHubUnavailable('not found',status=404)
        self.assertEqual(blocker(self.github.prerequisite('other/repo#7')),'prerequisite_inaccessible')


if __name__=='__main__':unittest.main()
