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
        self.assertEqual(dependencies('Deployed verification depends on #23. Coordinate dependency changes with #22.'), [23])
        self.assertEqual(dependencies('Depends on other/project#9 and https://github.com/x/y/issues/10'), [])
        self.assertEqual(dependencies('```\nDepends on #2\n```\nBlocked by #4'), [4])

    def test_prerequisites_written_in_plain_words_are_recognised(self):
        body = ('- Design source: #90. Do not start until #90 is closed.\n'
                '- Previous phase: #101 must be merged first (it reads its consequences).\n'
                '- `ready` marks this as eligible once its dependencies are closed.')
        self.assertEqual(dependencies(body), [90, 101])
        self.assertEqual(dependencies('Waiting for #12 and #13. Start after #4 is merged.'), [4, 12, 13])
        for text in ('This blocks #5.', 'See #3 for context.', 'Unblocks #7', 'Found in the #8 first pass'):
            self.assertEqual(dependencies(text), [], text)

    def test_a_worker_stopping_for_prerequisites_becomes_a_dependency_wait(self):
        self.cycle()
        job = self.active()
        self.engine.finish(job, {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'blocked_environment',
                                 'blocker': 'dependency_access', 'summary': 'Issue says do not start until #2 is closed',
                                 'headline': 'Stopped: prerequisites are open', 'plain': 'The issue says to wait for #2.'})
        self.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['environment_hold']['blocker'], 'dependency_access')
        self.github.issue = Mock(return_value=({'number': 1, 'state': 'open', 'labels': [],
            'body': 'Do not start until #2 is closed.'}, 'requirements'))
        self.github.call = Mock(return_value={'number': 2, 'state': 'open'})
        self.assertFalse(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertNotIn('environment_hold', item)
        self.assertEqual(item['blocked_dependencies'], [2])
        self.assertIsNone(item['active'])

    def test_a_question_about_named_prerequisites_is_answered_by_the_wait(self):
        self.cycle()
        job = self.active()
        decision = {'question': 'Should issue #1 wait until #2 is closed?', 'options': ['Wait', 'Proceed'],
                    'recommendation': 'Defer until #2 is closed.', 'evidence': 'The issue says to wait for #2.'}
        self.engine.finish(job, {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'needs_decision',
                                 'summary': 'Prerequisite open', 'decision': decision,
                                 'headline': 'Stopped: prerequisite open', 'plain': 'The issue says to wait for #2.'})
        self.cycle()
        self.assertTrue(self.ledger.read()['issues'][self.key].get('decision_request'))
        self.github.issue = Mock(return_value=({'number': 1, 'state': 'open', 'labels': [],
            'body': 'Prerequisites: #2.'}, 'requirements'))
        self.github.call = Mock(return_value={'number': 2, 'state': 'open'})
        self.assertFalse(self.cycle()['errors'])
        item = self.ledger.read()['issues'][self.key]
        self.assertNotIn('decision_request', item)
        self.assertEqual((item['state'], item['blocked_dependencies'], item['active']), ('ready', [2], None))

    def test_failures_from_open_prerequisites_become_a_wait_with_a_fresh_budget(self):
        from forge.factory.ledger import APPROACH_HOLD
        self.cycle()
        job = self.active()
        self.engine.finish(job, {'version': 1, 'attempt': job['id'], 'role': 'build', 'outcome': 'blocked_environment',
                                 'blocker': 'toolchain', 'summary': 'stop', 'headline': 'Stopped', 'plain': 'Stopped.'})
        self.cycle()
        with self.ledger.transaction() as data:
            item = data['issues'][self.key]
            item.update(state='hold', reason=APPROACH_HOLD, active=None)
            item.pop('environment_hold', None)
            data['jobs'][job['id']].update(role='recovery', cleanup=True, outcome='investigated',
                capture={'result': {'headline': 'Issue waits for its prerequisites',
                                    'summary': 'The issue forbids starting until #2 is closed.'}})
        self.github.issue = Mock(return_value=({'number': 1, 'state': 'open', 'labels': [],
            'body': 'Do not start until #2 is closed.'}, 'requirements'))
        self.github.call = Mock(return_value={'number': 2, 'state': 'open'})
        self.cycle()
        item = self.ledger.read()['issues'][self.key]
        self.assertEqual((item['state'], item['blocked_dependencies']), ('ready', [2]))
        self.assertEqual(self.ledger.failure_count(item), 0)

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

    def test_unreadable_dependency_backs_off_without_starving_another_issue(self):
        self.dependency()
        first = self.github.issue.return_value
        self.github.issue.side_effect = lambda item: first if item['number'] == 1 else (
            {'number': item['number'], 'state': 'open', 'body': '', 'labels': []}, 'requirements')
        other = self.ledger.discover({'id': 1, 'name': 'example/project'}, 3, 'requirements')
        self.github.call.side_effect = GitHubUnavailable('Prerequisite unavailable')
        self.cycle()
        self.assertEqual(self.active()['issue_key'], other)
        held = self.ledger.read()['issues'][self.key]
        self.assertGreater(held['retry_after'], 0)
        self.assertEqual(held['failures'], [])

    def test_wait_is_visible_without_operator_action(self):
        self.dependency(); self.cycle()
        issue, _ = self.github.issue({})
        item = self.ledger.read()['issues'][self.key]
        row = presentation(issue, item, {'name': 'example/project', 'enabled': True, 'access_ready': True}, self.config)
        self.assertEqual(row['status'], 'blocked')
        self.assertIn('#2', row['summary']['plain'])
        self.assertFalse(row['actions'])
