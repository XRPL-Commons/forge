import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.factory.contracts import (HEADLINE_LIMIT, PLAIN_LIMIT, candidate, digest, result,
                                     submission, summary, references_issue)
from forge.factory.config import validate, policy
from forge.factory.ledger import Ledger
from forge.factory.supervisor import Supervisor
from forge.factory.runner import RunnerError
from forge.factory.github import GitHubUnavailable, GitHubBudgetExhausted
from forge.state import StateError


class FactoryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.now = 1000
        self.ledger = Ledger(self.directory.name, clock=lambda: self.now)
        self.config = validate({'enabled': True, 'merge_enabled': True, 'image': 'sha256:' + 'a'*64, 'harness_argv': ['/adapter']})
        self.settings = Mock()
        self.settings.admission.return_value = {'allowed': True}
        self.settings.view.return_value = {'repositories': []}
        self.github = Mock()
        self.github.own_draft.return_value = None  # No open Forge draft unless a test says so.
        self.github.ci_evidence.return_value = None
        self.github.publisher.side_effect = lambda role: role + '[bot]'
        self.github.issue.return_value = ({'state': 'open', 'labels': []}, 'requirements')
        self.github.publication.return_value = None
        self.github.pr.return_value = {'number': 2, 'head': {'sha': 'a'*40}, 'base': {'sha': 'b'*40},
                                      'draft': False, 'state': 'open', 'merged': False, 'mergeable': True}
        self.github.human.return_value = (True, False)
        self.github.gates.return_value = True
        self.github.protections.return_value = True
        self.runner = Mock()
        self.runner.capacity.return_value = {'allowed': True, 'reason': 'capacity_available'}
        self.runner.observe.return_value = {'logs': 'Running output says success', 'at': 1000}
        self.runner.inspect.return_value = {'status': 'running', 'running': True}
        self.runner.create.return_value = 'container'
        self.runner.capture.return_value = {'result': None, 'logs': 'execution log', 'missing': []}
        self.runner.reset.return_value = {'container_absent': True, 'capacity_ready': True}
        self.supervisor = Supervisor(self.ledger, self.runner, self.github, self.settings, self.config, clock=lambda: self.now)
        self.key = self.ledger.discover({'id': 1, 'name': 'example/project'}, 1, 'requirements')

    def test_assignment_leaves_result_file_writes_to_the_harness(self):
        from forge.factory.contracts import assignment_note
        job = self.job()
        note = assignment_note(job)
        self.assertIn('final structured response', note['instructions'])
        self.assertIn('do not write that file yourself', note['instructions'])
        self.assertNotIn('Write the result contract atomically', note['instructions'])
        self.assertIn('same tool invocation', note['instructions'])

    def test_existing_pr_association_uses_exact_issue_identity(self):
        job = self.job()
        body = '<!-- forge-submission ' + json.dumps({'version': 1, 'issue': 1, 'attempt': job['id'], 'head': None}) + ' -->'
        self.assertTrue(references_issue(body, 'example/project', 1))
        self.assertTrue(references_issue('Fixes #1.', 'example/project', 1))
        self.assertTrue(references_issue('https://github.com/example/project/issues/1', 'example/project', 1))
        self.assertFalse(references_issue('Fixes #11', 'example/project', 1))
        self.assertFalse(references_issue('https://github.com/example/project/issues/11', 'example/project', 1))
        self.assertFalse(references_issue('https://github.com/other/project/issues/1', 'example/project', 1))

    def test_explicit_hold_fences_work_without_cancelling_issue(self):
        job = self.job()
        self.github.issue.return_value = ({'state': 'open', 'labels': [{'name': 'hold'}]}, 'requirements')
        self.supervisor.reconcile(self.item())
        self.assertEqual(self.item()['state'], 'hold')
        self.assertIsNone(self.item()['active'])
        self.assertTrue(self.ledger.read()['jobs'][job['id']]['fenced'])
        self.supervisor.execution(self.ledger.read()['jobs'][job['id']])
        self.assertFalse(self.ledger.read()['slots'])
        self.assertEqual(self.ledger.failure_count(self.item()), 0)

    def test_external_cancellation_is_idempotent(self):
        self.ledger.cancel(self.key, 'Closed')
        events = len(self.ledger.read()['events'])
        self.ledger.cancel(self.key, 'Closed')
        self.assertEqual(len(self.ledger.read()['events']), events)

    def test_live_output_does_not_establish_completion(self):
        job = self.job()
        self.ledger.job_update(job['id'], phase='running')
        self.supervisor.execution(self.ledger.read()['jobs'][job['id']])
        current = self.ledger.read()['jobs'][job['id']]
        self.assertIn('success', current['observation']['logs'])
        self.assertIsNone(current['outcome'])
        self.assertNotIn('capture', current)
        self.assertEqual(self.item()['active'], job['id'])
        self.assertEqual(self.item()['state'], 'ready')

    def test_settled_issues_are_observed_hourly_and_open_ones_every_rotation(self):
        done = self.ledger.discover({'id': 1, 'name': 'example/project'}, 2, 'requirements')
        with self.ledger.transaction() as data:
            data['issues'][done].update(state='merged', last_observed=self.now - 60, settled_observed=True)
            data['issues'][self.key]['last_observed'] = self.now - 60
        with patch.object(self.supervisor, 'reconcile') as reconcile:
            self.supervisor.cycle()
        self.assertEqual([c.args[0]['key'] for c in reconcile.call_args_list], [self.key])
        with self.ledger.transaction() as data:
            data['issues'][done]['last_observed'] = self.now - 3601
        with patch.object(self.supervisor, 'reconcile') as reconcile:
            self.supervisor.cycle()
        self.assertIn(done, [c.args[0]['key'] for c in reconcile.call_args_list])

    def test_an_issue_cut_short_by_the_budget_is_observed_first_next_cycle(self):
        from forge.factory.github import GitHubBudgetExhausted
        other = self.ledger.discover({'id': 1, 'name': 'example/project'}, 2, 'requirements')
        with self.ledger.transaction() as data:
            data['issues'][self.key]['last_observed'] = self.now - 100
            data['issues'][other]['last_observed'] = self.now - 50
        with patch.object(self.supervisor, 'reconcile', side_effect=GitHubBudgetExhausted('Cycle observation budget reached')):
            self.supervisor.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['last_observed'], self.now - 100)
        with patch.object(self.supervisor, 'reconcile') as reconcile:
            self.supervisor.cycle()
        self.assertEqual(reconcile.call_args_list[0].args[0]['key'], self.key)
        # An issue that never fits rotates after three interruptions in a row.
        with self.ledger.transaction() as data:
            data['issues'][self.key]['last_observed'] = self.now - 100
        with patch.object(self.supervisor, 'reconcile', side_effect=GitHubBudgetExhausted('Cycle observation budget reached')):
            for _ in range(3):
                self.supervisor.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['last_observed'], self.now)

    def test_dispatch_keeps_a_share_of_the_cycle_budget(self):
        # Two slow observations would eat the whole 60 s budget; the second is
        # skipped so a builder is still reserved this cycle.
        self.ledger.discover({'id': 1, 'name': 'example/project'}, 2, 'requirements')
        self.github.target.return_value = 'main'
        clock = {'now': 0.0}
        fake = Mock()
        fake.monotonic.side_effect = lambda: clock['now']
        def slow(item):
            clock['now'] += 50
        with patch('forge.factory.supervisor.time', fake), patch.object(self.supervisor, 'reconcile', side_effect=slow) as reconcile:
            self.supervisor.cycle()
        self.assertEqual(reconcile.call_count, 1)
        self.assertTrue(self.ledger.read()['slots'], 'dispatch must run after observation stops')

    def test_slow_api_observation_cannot_spend_dispatch_deadline(self):
        self.github.target.return_value = 'main'
        now = [0.0]
        def observe(item):
            now[0] = self.github.deadline
            raise GitHubBudgetExhausted('Observation allowance used')
        with patch('forge.factory.supervisor.time.monotonic', side_effect=lambda: now[0]), \
                patch.object(self.supervisor, 'reconcile', side_effect=observe):
            self.supervisor.cycle()
        self.assertLessEqual(now[0], self.config['cycle_budget_seconds'] / 2)
        self.assertEqual(self.github.deadline, self.config['cycle_budget_seconds'])
        self.assertTrue(self.ledger.read()['slots'])

    def test_discovery_exhaustion_preserves_dispatch_budget(self):
        self.github.target.return_value = 'main'
        now = [0.0]
        def discovery():
            now[0] = self.github.deadline
        with patch('forge.factory.supervisor.time.monotonic', side_effect=lambda: now[0]), \
                patch.object(self.supervisor, 'discovery_due', return_value=True), \
                patch.object(self.supervisor, 'discover', side_effect=discovery):
            self.supervisor.cycle()
        self.assertLess(now[0], self.github.deadline)
        self.assertTrue(self.ledger.read()['slots'])

    def test_busy_factory_still_discovers_new_issues_on_a_cadence(self):
        # A ready issue is always queued, so dispatch never discovers on its own.
        self.settings.view.return_value = {'repositories': [{'id': 1, 'name': 'example/project', 'dispatch_allowed': True}]}
        self.github.pulls.return_value = []
        self.github.issues.return_value = []
        self.github.target.return_value = 'main'
        self.runner.capacity.return_value = {'allowed': False, 'reason': 'memory_budget'}
        with patch('forge.factory.supervisor.time') as fake:
            fake.monotonic.return_value = 0.0
            self.supervisor.cycle()
            self.assertEqual(self.github.issues.call_count, 1, 'first cycle reads the repository')
            self.supervisor.cycle()
            self.assertEqual(self.github.issues.call_count, 1, 'read again only after the interval')
            self.now += Supervisor.DISCOVERY_INTERVAL
            self.supervisor.cycle()
            self.assertEqual(self.github.issues.call_count, 2)

    def test_interrupted_discovery_resumes_with_the_same_repository_next_cycle(self):
        self.settings.view.return_value = {'repositories': [
            {'id': 2, 'name': 'example/first', 'dispatch_allowed': True},
            {'id': 3, 'name': 'example/second', 'dispatch_allowed': True}]}
        self.github.deadline = float('inf')
        self.github.pulls.return_value = []
        calls = []
        def issues(item):
            calls.append(item['repo_id'])
            if len(calls) == 1:
                raise GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle')
            return []
        self.github.issues.side_effect = issues
        self.supervisor.discover()
        self.assertNotIn('2', self.ledger.read().get('discovery_observed', {}), 'an interrupted repository is not stamped')
        self.assertEqual(calls, [2], 'the cycle stops at the budget instead of moving to the next repository')
        self.supervisor.discover()
        self.assertEqual(calls, [2, 2, 3], 'the next cycle resumes with the interrupted repository first')

    def test_budget_exhaustion_is_not_reported_as_an_error(self):
        self.settings.view.return_value = {'repositories': [{'id': 2, 'name': 'example/first', 'dispatch_allowed': True}]}
        self.github.deadline = float('inf')
        self.github.pulls.side_effect = GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle')
        self.supervisor.discover()
        self.github.issue.side_effect = GitHubBudgetExhausted('Cycle observation budget reached; resume next cycle')
        self.github.target.return_value = 'main'
        self.supervisor.cycle()
        self.assertEqual([e for e in self.supervisor.errors if e['phase'] in ('github', 'discovery', 'reconciliation')], [])

    def test_discovery_rotates_repositories_after_exhausted_api_budget(self):
        self.settings.view.return_value = {'repositories': [
            {'id': 2, 'name': 'example/first', 'dispatch_allowed': True},
            {'id': 3, 'name': 'example/second', 'dispatch_allowed': True}]}
        def unavailable(item):
            self.github.deadline = 0
            raise GitHubUnavailable('Budget exhausted')
        self.github.deadline = float('inf')
        self.github.pulls.side_effect = unavailable
        self.supervisor.discover()
        self.assertEqual(self.github.pulls.call_count, 1)
        self.github.deadline = float('inf')
        self.supervisor.discover()
        self.assertEqual(self.github.pulls.call_args.args[0]['repo_id'], 3)

    def item(self):
        return self.ledger.read()['issues'][self.key]

    def job(self, role='build', failure=None):
        return self.ledger.reserve(self.key, role, role+'-1', publisher=role+'[bot]',
                                   policy=digest(policy(self.config)), timeout=120, failure=failure)

    def clean(self, job):
        self.ledger.job_update(job['id'], fenced=True, phase='stopping')
        self.ledger.release(job['id'])

    def candidate(self, job):
        return {'repo_id': 1, 'issue': 1, 'pr': 2, 'head': 'a'*40, 'base': 'b'*40, 'base_ref': 'main',
                'build_attempt': job['id'], 'key': 'candidate', 'requirements': 'requirements',
                'policy': job['policy'], 'branch': job['branch']}

    def report(self, job, outcome='pass'):
        value = {'version': 1, 'role': job['role'], 'attempt': job['id'], 'outcome': outcome, 'summary': 'Specific findings',
                 'headline': 'The step finished', 'plain': 'The worker finished this step. Forge moves the issue on.'}
        if job['role'] in ('review', 'audit'):
            value['candidate'] = job['candidate']['key']
        if job['role'] == 'audit':
            value.update(human_required='required', human_reason='Sensitive change')
        return value

    def investigate(self, pattern='tests', alternative=None, incomplete=False):
        failure = self.item()['failures'][-1]
        job = self.job('recovery', failure)
        self.ledger.findings(job['id'], {'summary': 'Diagnosis', 'pattern': pattern, 'evidence': ['test output'],
                                      'uncertainty': '', 'alternative': alternative, 'incomplete': incomplete})
        self.clean(job)
        return job

    def test_plain_language_is_shortened_rather_than_refused(self):
        job = self.job()
        report = self.report(job)
        for blank in ('', '   '):
            with self.assertRaises(ValueError):
                result({**report, 'headline': blank}, job)
        long = result({**report, 'headline': 'w' * 400,
                       'plain': 'One. Two. Three. Four that must not survive. ' + 'word ' * 200}, job)
        self.assertEqual(len(long['headline']), HEADLINE_LIMIT)
        self.assertTrue(long['headline'].endswith('…'))
        self.assertLessEqual(len(long['plain']), PLAIN_LIMIT)
        self.assertEqual(long['plain'], 'One. Two. Three.')
        self.assertNotIn('Four', long['plain'])

    def test_a_worker_from_the_previous_image_still_reports_in_plain_language(self):
        # During a rolling upgrade the old image writes neither field. Deriving
        # them from summary is better than failing an attempt that did the work.
        job = self.job()
        report = self.report(job)
        older = {k: v for k, v in report.items() if k not in ('headline', 'plain')}
        older['summary'] = ('Rebuilt the navigation module and opened the pull request. '
                            'Ran the unit tests in packages/web and they pass. '
                            'Left the public component API untouched. '
                            'This fourth sentence must not reach the page.')
        derived = result(dict(older), job)
        self.assertEqual(derived['headline'], 'Rebuilt the navigation module and opened the pull request.')
        self.assertEqual(derived['plain'], 'Rebuilt the navigation module and opened the pull request. '
                                           'Ran the unit tests in packages/web and they pass. '
                                           'Left the public component API untouched.')
        self.assertNotIn('fourth sentence', derived['plain'])
        long = result({**older, 'summary': 'w' * 400}, job)
        self.assertEqual(len(long['headline']), HEADLINE_LIMIT)
        self.assertLessEqual(len(long['plain']), PLAIN_LIMIT)
        # Only absence is forgiven: a field of the wrong type is still malformed.
        for broken in ({**report, 'headline': 42}, {**report, 'plain': None}, {**older, 'summary': '  '}):
            with self.assertRaises(ValueError):
                result(broken, job)

    def test_every_outcome_keeps_the_plain_language_card_with_the_issue(self):
        card = {'headline': 'Built and waiting for review',
                'plain': 'The change is written and its pull request is open. Review starts next.'}
        build = self.job()
        self.ledger.accept(build['id'], candidate=self.candidate(build), summary=summary(card, 'build', self.now))
        self.assertEqual(self.item()['last_summary']['headline'], card['headline'])
        self.assertEqual(self.item()['candidate']['summary']['role'], 'build')
        self.clean(build)
        review = self.job('review')
        failed = summary({'headline': 'Stopped: the tests fail on the login page',
                          'plain': 'Signing in no longer works. The attempt was stopped.'}, 'review', self.now)
        self.ledger.fail(review['id'], 'Rejected', rejection=True, summary=failed)
        self.assertEqual(self.item()['failures'][-1]['summary'], failed)
        self.assertEqual(self.item()['last_summary'], failed)
        self.clean(review)
        investigation = self.job('recovery', self.item()['failures'][-1])
        found = summary({'headline': 'The login test needs a fresh database',
                         'plain': 'The failure comes from stale test data. Forge will try again.'}, 'recovery', self.now)
        self.ledger.findings(investigation['id'], {'summary': 'Diagnosis', 'pattern': 'data', 'evidence': ['log'],
                                                   'uncertainty': '', 'alternative': None}, summary=found)
        self.assertEqual(self.item()['last_summary'], found)
        self.clean(investigation)
        blocked = self.job()
        stopped = summary({'headline': 'Stopped: the worker ran out of memory',
                           'plain': 'The worker used more memory than it is allowed.'}, 'build', self.now)
        self.ledger.block_environment(blocked['id'], 'resources', 'Out of memory', summary=stopped)
        self.assertEqual(self.item()['environment_hold']['summary'], stopped)
        self.assertEqual(self.item()['last_summary'], stopped)

    def test_a_result_without_a_card_leaves_the_previous_one_untouched(self):
        build = self.job()
        first = summary({'headline': 'Built and waiting for review', 'plain': 'Review starts next.'}, 'build', self.now)
        self.ledger.accept(build['id'], candidate=self.candidate(build), summary=first)
        self.clean(build)
        review = self.job('review')
        self.assertIsNone(summary({'summary': 'no plain language here'}, 'review', self.now))
        self.ledger.fail(review['id'], 'Rejected', rejection=True, summary=None)
        self.assertEqual(self.item()['last_summary'], first)
        self.assertNotIn('summary', self.item()['failures'][-1])


    def test_durable_reservation_prevents_duplicate_issue_and_slot(self):
        job = self.job()
        other = Ledger(self.directory.name)
        self.assertEqual(other.read()['slots']['build-1'], job['id'])
        with self.assertRaises(StateError):
            self.job()
        self.ledger.accept(job['id'], candidate=self.candidate(job))
        with self.assertRaises(StateError):
            self.job('review')
        self.clean(job)
        self.assertEqual(self.job('review')['role'], 'review')

    def test_success_does_not_reset_issue_wide_budget(self):
        first = self.job()
        self.ledger.fail(first['id'], 'Build failed')
        self.clean(first); self.investigate()
        build = self.job()
        self.ledger.accept(build['id'], candidate=self.candidate(build)); self.clean(build)
        review = self.job('review')
        self.ledger.fail(review['id'], 'Rejected', rejection=True); self.clean(review)
        self.investigate('logic')
        build = self.job()
        self.ledger.fail(build['id'], 'Different failure'); self.clean(build)
        self.assertEqual(self.item()['state'], 'failed')
        self.assertEqual(self.ledger.failure_count(self.item()), 3)
        self.investigate('environment')
        self.assertEqual(self.item()['state'], 'failed')
        with self.assertRaises(StateError):
            self.job()

    def test_repeated_failure_requires_changed_third_attempt(self):
        for _ in range(2):
            job = self.job(); self.ledger.fail(job['id'], 'tests failed'); self.clean(job); self.investigate()
        self.assertEqual(self.item()['state'], 'hold')
        self.assertEqual(self.ledger.failure_count(self.item()), 2)

    def test_supported_alternative_is_passed_to_next_assignment(self):
        first = self.job(); self.ledger.fail(first['id'], 'tests failed'); self.clean(first); self.investigate()
        second = self.job(); self.ledger.fail(second['id'], 'tests failed'); self.clean(second)
        alternative = {'change': 'Replace network fixture with local fake', 'reason': 'Offline dependency', 'evidence': 'Connection failure in test output'}
        self.investigate(alternative=alternative)
        self.assertEqual(self.job()['history']['retry_plan'], alternative)

    def test_failed_investigation_never_recurses_or_charges_again(self):
        job = self.job(); self.ledger.fail(job['id'], 'Failure'); self.clean(job)
        recovery = self.job('recovery', self.item()['failures'][0])
        self.ledger.job_update(recovery['id'], capture={'result': None})
        self.supervisor.outcome(self.ledger.read()['jobs'][recovery['id']])
        self.assertEqual(self.ledger.failure_count(self.item()), 1)
        self.assertEqual(self.item()['failures'][0]['investigation'], 'complete')
        self.assertTrue(self.item()['failures'][0]['findings']['incomplete'])

    def test_failure_observed_twice_is_charged_once_after_restart(self):
        job = self.job()
        self.ledger.fail(job['id'], 'failure')
        Ledger(self.directory.name).fail(job['id'], 'duplicate')
        self.assertEqual(self.ledger.failure_count(self.item()), 1)

    def test_crash_after_build_publication_recovers_without_failure(self):
        job = self.job()
        self.github.publication.return_value = self.candidate(job)
        self.ledger.job_update(job['id'], capture={'result': None, 'missing': ['worker output']})
        self.supervisor.outcome(self.ledger.read()['jobs'][job['id']])
        self.assertEqual(self.item()['state'], 'built')
        self.assertEqual(self.ledger.failure_count(self.item()), 0)

    def test_success_prose_does_not_advance_without_publication(self):
        job = self.job()
        self.ledger.job_update(job['id'], capture={'result': self.report(job)})
        self.supervisor.outcome(self.ledger.read()['jobs'][job['id']])
        self.assertEqual(self.item()['state'], 'investigating')

    def test_missing_container_resets_before_network_reconciliation(self):
        job = self.job(); self.ledger.job_update(job['id'], phase='running')
        self.runner.inspect.return_value = None
        self.github.issue.side_effect = RuntimeError('Should not be called by reset')
        self.supervisor.execution(self.ledger.read()['jobs'][job['id']])
        self.assertFalse(self.ledger.read()['slots'])
        self.assertEqual(self.item()['active'], job['id'])
        self.runner.reset.assert_called_once()

    def test_failed_teardown_preserves_slot_and_verified_milestone(self):
        job = self.job()
        self.ledger.accept(job['id'], candidate=self.candidate(job))
        self.ledger.job_update(job['id'], fenced=True, phase='stopping')
        self.runner.reset.side_effect = RunnerError('Engine unavailable')
        with self.assertRaises(RunnerError):
            self.supervisor.execution(self.ledger.read()['jobs'][job['id']])
        self.assertEqual(self.item()['state'], 'built')
        self.assertEqual(self.ledger.read()['slots']['build-1'], job['id'])

    def test_stale_release_cannot_free_replacement(self):
        first = self.job(); self.ledger.wait(first['id'], 'Quota', 0); self.clean(first)
        replacement = self.job()
        with self.assertRaises(StateError):
            self.ledger.release(first['id'])
        self.assertEqual(self.ledger.read()['slots']['build-1'], replacement['id'])

    def test_cleanup_runs_when_factory_and_repository_disabled(self):
        job = self.job(); self.ledger.job_update(job['id'], phase='running')
        self.ledger.cancel(self.key, 'Operator stop')
        self.config['enabled'] = False
        self.settings.admission.return_value = {'allowed': False}
        self.supervisor.execution(self.ledger.read()['jobs'][job['id']])
        self.assertFalse(self.ledger.read()['slots'])
        self.assertEqual(self.ledger.failure_count(self.item()), 0)

    def test_human_wait_releases_audit_and_does_not_repeat_it(self):
        build = self.job(); self.ledger.accept(build['id'], candidate=self.candidate(build)); self.clean(build)
        review = self.job('review'); self.ledger.accept(review['id'], evidence={'check_id': 1}); self.clean(review)
        audit = self.job('audit'); self.ledger.accept(audit['id'], evidence={**self.report(audit), 'check_id': 2}); self.clean(audit)
        self.github.human.return_value = (False, False)
        self.supervisor.merge(self.item(), self.github.pr.return_value)
        self.assertEqual(self.item()['state'], 'approval_needed')
        self.assertFalse(self.ledger.read()['slots'])
        self.assertEqual(self.supervisor.choose('audit', self.ledger.read()), (None, None))
        self.github.merge.assert_not_called()

    def test_human_approval_cannot_waive_technical_gate(self):
        build = self.job(); self.ledger.accept(build['id'], candidate=self.candidate(build)); self.clean(build)
        with self.ledger.transaction() as data:
            data['issues'][self.key].update(state='audited', evidence={'audit': {'human_required': 'required'}})
        self.github.gates.return_value = False
        self.supervisor.merge(self.item(), self.github.pr.return_value)
        self.github.merge.assert_not_called()

    def test_explicit_human_retry_retains_previous_failures(self):
        for n in range(3):
            job = self.job(); self.ledger.fail(job['id'], 'failure'); self.clean(job); self.investigate(str(n))
        self.ledger.retry(self.key, 'Operator corrected the infrastructure')
        self.assertEqual(self.ledger.failure_count(self.item()), 0)
        self.assertEqual(len(self.item()['failures']), 3)
        self.assertEqual(self.item()['state'], 'ready')

    def test_missing_provider_credentials_wait_without_charging_or_leaking_a_slot(self):
        from forge.factory.runner import RunnerUnavailable
        job = self.job()
        self.runner.inspect.return_value = {'status': 'created', 'running': False}
        self.runner.prepare.side_effect = RunnerUnavailable('Provider credentials unavailable')
        self.supervisor.execution(job)
        self.assertEqual(self.ledger.failure_count(self.item()), 0)
        self.assertFalse(self.ledger.read()['slots'])
        self.assertGreater(self.ledger.read()['provider_retry_after'], self.now)

    def test_memory_wait_resumes_same_job_after_restart_without_timeout_or_failure(self):
        job = self.job()
        self.runner.inspect.return_value = None
        self.runner.capacity.return_value = {'allowed': False, 'reason': 'memory_headroom'}
        self.supervisor.execution(job)
        waiting = self.ledger.read()['jobs'][job['id']]
        self.assertEqual(waiting['capacity_wait_since'], self.now)
        self.assertFalse(waiting['fenced'])
        self.runner.create.assert_not_called()
        self.assertEqual(self.ledger.failure_count(self.item()), 0)
        self.now += 600
        resumed = Supervisor(self.ledger, self.runner, self.github, self.settings, self.config, clock=lambda: self.now)
        self.runner.capacity.return_value = {'allowed': True}
        self.runner.inspect.side_effect = [None, {'status': 'created', 'running': False}, {'status': 'running', 'running': True}]
        resumed.execution(waiting)
        current = self.ledger.read()['jobs'][job['id']]
        self.assertEqual(current['phase'], 'running')
        self.assertIsNone(current['capacity_wait_since'])
        self.assertEqual(current['deadline'], job['deadline'] + 600)
        self.runner.start.assert_called_once()
        self.assertEqual(self.ledger.failure_count(self.item()), 0)

    def test_capacity_race_at_start_waits_without_provider_cooldown(self):
        from forge.factory.runner import RunnerCapacity
        job = self.job()
        self.runner.inspect.return_value = {'status': 'created', 'running': False}
        self.runner.start.side_effect = RunnerCapacity({'allowed': False, 'reason': 'memory_pressure'})
        self.supervisor.execution(job)
        current = self.ledger.read()['jobs'][job['id']]
        self.assertEqual(current['phase'], 'launching')
        self.assertIsNotNone(current['capacity_wait_since'])
        self.assertEqual(self.ledger.failure_count(self.item()), 0)
        self.assertFalse(self.ledger.read().get('provider_retry_after'))
        self.runner.reset.assert_not_called()

    def test_low_memory_leaves_new_work_queued_without_allocating_an_attempt(self):
        self.github.deadline = float('inf')
        self.runner.capacity.return_value = {'allowed': False, 'reason': 'memory_budget'}
        self.supervisor.dispatch()
        self.assertEqual(self.item()['state'], 'ready')
        self.assertFalse(self.ledger.read()['jobs'])
        self.assertFalse(self.ledger.read()['slots'])
        self.assertFalse(self.supervisor.errors)

    def test_initialized_but_incomplete_json_never_resets_ownership(self):
        self.job()
        Path(self.directory.name, 'factory-ledger.json').write_text('{"version":1}')
        with self.assertRaises(StateError):
            Ledger(self.directory.name).read()

    def test_recovery_corrects_a_failure_once_and_cannot_undo_cancellation(self):
        job = self.job(); self.ledger.fail(job['id'], 'Publication initially absent'); self.clean(job)
        self.assertTrue(self.ledger.recover_publication(job['id'], self.candidate(job)))
        self.assertFalse(self.ledger.recover_publication(job['id'], self.candidate(job)))
        self.assertEqual(self.ledger.failure_count(self.item()), 0)
        self.assertEqual(self.item()['state'], 'built')
        self.ledger.cancel(self.key, 'Operator decision')
        self.ledger.hold(self.key, 'Old observation failed')
        self.assertEqual(self.item()['state'], 'cancelled')

    def test_unknown_investigation_does_not_replace_failed_milestone_with_hold(self):
        for number in range(3):
            job = self.job(); self.ledger.fail(job['id'], 'Failure'); self.clean(job); self.investigate(str(number))
        self.ledger.hold(self.key, 'Old observation unavailable')
        self.assertEqual(self.item()['state'], 'failed')

    def test_contract_binds_author_attempt_issue_branch_head_and_repository(self):
        job = self.job()
        block = {'version': 1, 'issue': 1, 'attempt': job['id'], 'head': 'a'*40}
        pr = {'number': 2, 'body': '<!-- forge-submission '+json.dumps(block)+' -->',
              'head': {'sha': 'a'*40, 'ref': job['branch'], 'repo': {'id': 1}},
              'base': {'sha': 'b'*40, 'ref': 'main', 'repo': {'id': 1}},
              'user': {'login': 'build[bot]', 'type': 'Bot'}, 'draft': False, 'state': 'open'}
        self.assertEqual(candidate(pr, job, 'r', 'p')['head'], 'a'*40)
        for field, value in [('draft', True), ('state', 'closed'), ('user', {'login': 'other', 'type': 'Bot'})]:
            changed = {**pr, field: value}
            with self.assertRaises(ValueError):
                candidate(changed, job, 'r', 'p')
        with self.assertRaises(ValueError):
            submission(pr['body'] + pr['body'])

    def test_stale_result_cannot_advance_new_candidate(self):
        build = self.job(); self.ledger.accept(build['id'], candidate=self.candidate(build)); self.clean(build)
        review = self.job('review')
        self.ledger.invalidate(self.key, 'new head')
        self.assertFalse(self.ledger.accept(review['id'], evidence={'check_id': 1}))
        self.assertEqual(self.item()['state'], 'ready')


if __name__ == '__main__':
    unittest.main()
