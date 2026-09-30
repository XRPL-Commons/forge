"""Ownership waits survive controller restarts without spending attempts."""
from unittest.mock import Mock

from forge.factory.issues import facts, presentation
from forge.factory.ownership import ownership
from forge.factory.supervisor import Supervisor
from forge.factory.github import GitHubUnavailable
from tests.test_factory_cycle import CycleTest, Settings


class OwnershipSchedulingTests(CycleTest):
    def owned(self, *, assignees=(), labels=('ready',), body='', prs=()):
        issue = {'number': 1, 'title': 'Shared issue', 'state': 'open', 'body': body,
                 'labels': [{'name': n} for n in labels], 'assignees': list(assignees)}
        self.github.issue = Mock(return_value=(issue, 'requirements'))
        self.github.pulls = Mock(return_value=list(prs))
        return issue

    def item(self):
        return self.ledger.read()['issues'][self.key]

    def assert_waiting(self, issue):
        self.assertFalse(self.cycle()['errors'])
        item = self.item()
        self.assertFalse(self.engine.jobs)
        self.assertEqual(item['failures'], [])
        self.assertEqual(item['state'], 'ready')
        row = presentation(facts(issue), item, {'name': item['repo'], 'enabled': True, 'access_ready': True},
                           self.config, self.github.pulls.return_value)
        self.assertEqual(row['status'], 'waiting')
        self.assertEqual(row['summary'], item['ownership_wait'])
        self.assertEqual(row['actions'], [])

    def test_assignee_takes_precedence_over_dependency_then_both_resume(self):
        issue = self.owned(assignees=[{'login': 'lucbocahut', 'type': 'User'}], body='Depends on #2')
        self.github.call = Mock(return_value={'number': 2, 'state': 'open'})
        self.assert_waiting(issue)
        self.github.call.assert_not_called()
        issue['assignees'] = []
        self.cycle()
        self.assertIsNone(self.item()['ownership_wait'])
        self.assertEqual(self.item()['blocked_dependencies'], [2])
        self.assertFalse(self.engine.jobs)
        self.github.call.return_value['state'] = 'closed'
        self.cycle()
        self.assertEqual(self.active()['role'], 'build')

    def test_wip_resumes_when_removed(self):
        issue = self.owned(labels=['ready', 'wip'])
        self.assert_waiting(issue)
        issue['labels'] = [{'name': 'ready'}]
        self.cycle()
        self.assertEqual(self.active()['role'], 'build')

    def test_claiming_pr_resumes_when_closed(self):
        issue = self.owned(prs=[{'number': 8, 'body': 'Fixes #1', 'user': {'login': 'maintainer'}}])
        self.assert_waiting(issue)
        self.assertIn('@maintainer', self.item()['ownership_wait']['headline'])
        self.github.pulls.return_value[0]['state'] = 'closed'
        self.cycle()
        self.assertEqual(self.active()['role'], 'build')

    def test_bot_assignees_do_not_block_and_cache_preserves_their_type(self):
        issue = self.owned(assignees=[{'login': 'automation', 'type': 'Bot'}, {'login': 'forge-build[bot]'}])
        self.assertIsNone(ownership(facts(issue), self.item(), 'example/project'))
        self.cycle()
        self.assertEqual(self.active()['role'], 'build')

    def test_a_mention_is_not_a_claim(self):
        self.owned(prs=[{'number': 8, 'body': 'See #1 for context'}])
        self.cycle()
        self.assertEqual(self.active()['role'], 'build')

    def test_dispatch_checks_ownership_without_observation(self):
        self.owned(assignees=[{'login': 'maintainer'}])
        self.github.deadline = float('inf')
        Supervisor(self.ledger, self.engine, self.github, Settings(), self.config).dispatch(discover_missing=False)
        self.assertFalse(self.engine.jobs)
        self.assertIsNotNone(self.item()['ownership_wait'])

    def test_assignment_after_build_preserves_candidate_and_blocks_review(self):
        self.cycle()
        self.github.publish(self.active())
        self.finish('build')
        issue = self.owned(assignees=[{'login': 'maintainer'}])
        self.cycle()
        candidate = self.item()['candidate']
        self.assertIsNotNone(candidate)
        self.assertEqual(self.item()['state'], 'built')
        self.assertFalse([j for j in self.ledger.read()['jobs'].values() if j['role'] == 'review'])
        issue['assignees'] = []
        self.cycle()
        self.assertEqual(self.active()['role'], 'review')
        self.assertEqual(self.item()['candidate'], candidate)

    def test_assignment_after_audit_blocks_merge(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle(); self.finish('review'); self.cycle()
        self.finish('audit')
        issue = self.owned(assignees=[{'login': 'maintainer'}])
        self.cycle()
        self.assertFalse(self.github.pull['merged'])
        self.assertEqual(self.item()['state'], 'audited')
        issue['assignees'] = []
        self.cycle()
        self.assertEqual(self.item()['state'], 'merged')

    def test_unreadable_prs_cannot_dispatch(self):
        self.owned()
        self.github.pulls.side_effect = GitHubUnavailable('Cannot inspect existing work')
        self.cycle()
        self.assertFalse(self.engine.jobs)
        self.assertEqual(self.item()['failures'], [])

    def test_new_discovery_waits_without_permanent_hold(self):
        issue = self.owned(prs=[{'number': 8, 'body': 'Closes #1'}])
        settings = Settings()
        settings.view = lambda: {'repositories': [{'id': 1, 'name': 'example/project', 'dispatch_allowed': True}]}
        with self.ledger.transaction() as data:
            data['issues'].clear()
        self.github.issues = Mock(return_value=[issue])
        self.github.deadline = float('inf')
        Supervisor(self.ledger, self.engine, self.github, settings, self.config).discover()
        self.assertEqual(self.item()['state'], 'ready')
        self.assertIsNotNone(self.item()['ownership_wait'])
        self.github.pulls.return_value = []
        self.cycle()
        self.assertEqual(self.active()['role'], 'build')

    def test_a_forge_branch_name_does_not_hide_someone_elses_claim(self):
        issue = self.owned(prs=[{'number': 8, 'body': 'Closes #1', 'branch': 'forge/issue-1-pretender',
                                'author': 'maintainer'}])
        self.assert_waiting(issue)

    def test_verified_forge_draft_can_resume(self):
        self.owned(prs=[{'number': 8, 'body': 'Closes #1', 'branch': 'forge/issue-1-old', 'forge_owned': True}])
        self.cycle()
        self.assertEqual(self.active()['role'], 'build')

    def test_requested_reviewer_is_not_an_implementation_owner(self):
        item = {'candidate': {'pr': 8}, 'state': 'approval_needed', 'reviewers_requested': {'logins': ['maintainer']}}
        issue = self.owned(assignees=[{'login': 'maintainer'}])
        self.assertIsNone(ownership(issue, item, 'example/project'))
        issue['assignees'].append({'login': 'developer'})
        self.assertEqual(ownership(issue, item, 'example/project')['headline'], 'Assigned to @developer')
        issue['assignees'].pop()
        item['state'] = 'ready'
        self.assertIsNotNone(ownership(issue, item, 'example/project'))

    def test_pr_inventory_verifies_forge_author_and_repository(self):
        from copy import deepcopy
        from forge.factory.github import GitHub
        github = object.__new__(GitHub)
        github.publisher = Mock(return_value='build[bot]')
        own = {'number': 8, 'state': 'open', 'body': 'Closes #1',
               'head': {'ref': 'forge/issue-1-old', 'repo': {'id': 1}},
               'base': {'repo': {'id': 1}}, 'user': {'login': 'build[bot]', 'type': 'Bot'}}
        other = deepcopy(own); other['number'] = 9; other['user'] = {'login': 'developer', 'type': 'User'}
        fork = deepcopy(own); fork['number'] = 10; fork['head']['repo']['id'] = 2
        github.pages = Mock(return_value=[own, other, fork])
        rows = github.pulls({'repo_id': 1})
        self.assertEqual([p['forge_owned'] for p in rows], [True, False, False])
        github.publisher.assert_called_once_with('build')

    def test_explicit_hold_still_fences_an_active_owned_worker(self):
        self.cycle()
        self.owned(assignees=[{'login': 'maintainer'}], labels=['ready', 'hold'])
        self.cycle()
        self.assertEqual(self.item()['state'], 'hold')
        self.assertIsNone(self.item()['active'])
        self.assertEqual(self.item()['failures'], [])
