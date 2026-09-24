"""Prerequisites consume observation time, never workers or code retry budget."""
from unittest.mock import Mock

from forge.factory.contracts import dependencies
from forge.factory.github import GitHubBudgetExhausted, GitHubUnavailable
from forge.factory.issues import presentation
from tests.test_factory_cycle import CycleTest


class DependencySchedulingTests(CycleTest):
    def dependency(self, state='open'):
        self.github.issue = Mock(return_value=({'number': 1, 'state': 'open', 'labels': [],
            'body': 'Effort: S. Depends on: #2 (foundation). Blocks: #3 (page).'}, 'requirements'))
        self.github.call = Mock(return_value={'number': 2, 'state': state})

    def test_only_explicit_upstream_references_are_prerequisites(self):
        self.assertEqual(dependencies('See #1. Depends on: #2, #4. Blocks: #3.'), [2, 4])
        self.assertEqual(dependencies('Depends on #2. References: #7\nRelated: #8'), [2])
        self.assertEqual(dependencies('Depends on other/project#9 and https://github.com/x/y/issues/10'), [])
        self.assertEqual(dependencies('```\nDepends on #2\n```\nBlocked by #4'), [4])

    def test_open_dependency_waits_and_closure_starts_without_retry(self):
        self.dependency()
        self.assertFalse(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual(item['state'], 'ready')
        self.assertEqual(item['blocked_dependencies'], [2])
        self.assertFalse(self.engine.jobs)
        self.assertEqual(item['failures'], [])
        self.github.call.return_value['state'] = 'closed'
        self.assertFalse(self.cycle()['errors'])
        self.assertEqual(self.active()['phase'], 'running')
        self.assertFalse(self.ledger.read()['issues'][self.key]['blocked_dependencies'])

    def test_newly_discovered_job_is_checked_at_dispatch_without_observation(self):
        from forge.factory.supervisor import Supervisor
        from tests.test_factory_cycle import Settings
        self.dependency()
        self.github.deadline = float('inf')
        self.github.pulls = Mock(return_value=[])
        self.github.issues = Mock(return_value=[])
        controller = Supervisor(self.ledger, self.engine, self.github, Settings(), self.config)
        controller.dispatch()
        self.assertFalse(self.engine.jobs)
        self.assertEqual(self.ledger.read()['issues'][self.key]['blocked_dependencies'], [2])

    def test_unreadable_dependency_never_launches_or_charges_failure(self):
        for error in (GitHubUnavailable('Forbidden'), GitHubBudgetExhausted('Later')):
            self.dependency()
            self.github.call.side_effect = error
            self.cycle()
            self.assertFalse(self.engine.jobs)
            self.assertEqual(self.ledger.read()['issues'][self.key]['failures'], [])

    def test_wait_is_visible_without_operator_action(self):
        self.dependency(); self.cycle()
        issue, _ = self.github.issue({})
        item = self.ledger.read()['issues'][self.key]
        row = presentation(issue, item, {'name': 'example/project', 'enabled': True, 'access_ready': True}, self.config)
        self.assertEqual(row['status'], 'waiting')
        self.assertIn('#2', row['summary']['plain'])
        self.assertFalse(row['actions'])
