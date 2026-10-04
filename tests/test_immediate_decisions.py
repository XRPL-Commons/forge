"""Local decisions are immediate; only verified GitHub observations admit work."""
import time
import unittest
from unittest.mock import patch

from forge.factory.issue_actions import decide
from forge.factory.ledger import DecisionChanged
from forge.factory.supervisor import Supervisor
from forge.state import StateError
from tests import test_issue_worklist as worklist
from tests.test_factory_cycle import Engine


class ImmediateDecisionTests(unittest.TestCase):
    setUp = worklist.WorklistTests.setUp
    refresh = worklist.WorklistTests.refresh
    tracked = worklist.WorklistTests.tracked

    def decide(self, action, **overrides):
        self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        args = dict(key='1:1', action=action, revision=row['revision'], reason='Operator decision')
        self.github.calls.clear()
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            return decide(self.root, **{**args, **overrides})

    def item(self):
        return self.ledger.read()['issues']['1:1']

    def sync(self):
        with self.ledger.observing(self.item()):
            self.actions.sync(self.item())

    def reserve(self):
        return self.ledger.reserve('1:1', 'build', 'build-1', publisher='build[bot]', policy='policy', timeout=60)

    def worker_dependency(self):
        from forge.factory.contracts import digest
        self.tracked()
        self.github.value['body'] = 'Use the foundation from #90.'
        with self.ledger.transaction() as data:
            data['issues']['1:1'].update(declared_dependencies=[90], blocked_dependencies=[90],
                dependency_body=digest(self.github.value['body']), dependency_evidence=self.github.value['body'],
                dependency_reason='Waiting for #90.', failures=[{'attempt': 'old', 'role': 'build', 'cause': 'old'}], budget_start=0)

    def test_start_anyway_records_reason_and_clears_only_worker_dependencies(self):
        self.worker_dependency()
        self.refresh(); row = self.inventory.view(key='1:1')['issue']
        identifier = self.decide('start_anyway', reason='Foundation API is already available')
        self.assertNotIn('declared_dependencies', self.item())
        self.assertNotIn('blocked_dependencies', self.item())
        self.assertEqual(self.item()['budget_start'], 0)
        self.assertEqual(self.item()['failures'], [{'attempt': 'old', 'role': 'build', 'cause': 'old'}])
        self.assertEqual(self.item()['retry_plan'], 'Foundation API is already available')
        self.assertFalse(self.github.calls)
        events = [e for e in self.ledger.read()['events'] if e.get('request') == identifier]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['kind'], 'operator_start_anyway')
        self.assertEqual(events[0]['issues'], [90])
        self.decide('start_anyway', request_id=identifier, revision=row['revision'], reason='Foundation API is already available')
        with self.assertRaises(StateError): self.reserve()
        self.sync()
        self.reserve()

    def test_start_anyway_cannot_bypass_new_explicit_dependency(self):
        self.worker_dependency()
        self.decide('start_anyway')
        self.github.value['body'] += '\nDepends on #8.'
        self.sync()
        self.assertEqual(self.item()['state'], 'hold')
        with self.assertRaises(StateError): self.reserve()

    def test_start_anyway_rejects_stale_declarations_and_empty_reason(self):
        self.worker_dependency()
        with self.assertRaises(ValueError): self.decide('start_anyway', reason=' ')
        self.refresh(); row = self.inventory.view(key='1:1')['issue']
        with self.ledger.transaction() as data:
            data['issues']['1:1']['declared_dependencies'] = [91]
        with self.assertRaises(ValueError): self.decide('start_anyway', revision=row['revision'])

    def test_queue_updates_inventory_without_github_or_pending_receipt(self):
        identifier = self.decide('queue')
        self.assertEqual(self.item()['state'], 'ready')
        self.assertEqual(self.inventory.view(key='1:1')['issue']['status'], 'queued')
        self.assertFalse(self.github.calls)
        self.assertEqual(self.inventory.action_statuses(), [])
        self.assertNotIn('operator_request', self.item())
        self.assertEqual(len([e for e in self.ledger.read()['events'] if e.get('request') == identifier]), 1)
        with self.assertRaisesRegex(StateError, 'Confirm'):
            self.reserve()
        self.sync()
        self.assertEqual({l['name'] for l in self.github.value['labels']}, {'customer', 'ready'})
        self.assertNotIn('operator_sync', self.item())
        self.assertEqual(self.item()['requirements'], 'requirements')
        self.reserve()

    def test_pause_resume_and_backlog_compose_before_github_sync(self):
        self.decide('queue')
        self.decide('pause')
        self.assertEqual(self.item()['state'], 'hold')
        self.assertEqual(self.inventory.view(key='1:1')['issue']['actions'], ['resume'])
        self.assertEqual(self.inventory.view(key='1:1')['issue']['summary']['headline'], 'Paused by an operator')
        self.decide('resume')
        self.assertEqual(self.item()['state'], 'ready')
        self.assertNotIn('hold', self.inventory.view(key='1:1')['issue']['labels'])
        self.decide('backlog')
        self.assertEqual(self.inventory.view(key='1:1')['issue']['status'], 'backlog')
        self.sync()
        self.assertEqual({l['name'] for l in self.github.value['labels']}, {'customer'})
        self.assertEqual(self.item()['operator_hold']['kind'], 'backlog')

    def test_recheck_preserves_stage_candidate_and_failure_budget(self):
        self.tracked()
        with self.ledger.transaction() as data:
            data['issues']['1:1'].update(state='hold', environment_hold={'state': 'built'},
                failures=[{'cause': 'old'}], budget_start=0, candidate={'pr': 7})
        self.decide('recheck')
        self.assertEqual(self.item()['state'], 'built')
        self.assertEqual(self.item()['budget_start'], 0)
        self.assertEqual(self.item()['candidate'], {'pr': 7})
        self.assertNotIn('environment_hold', self.item())
        self.sync()

    def test_retry_starts_new_failure_budget_and_records_the_answer(self):
        self.tracked()
        with self.ledger.transaction() as data:
            data['issues']['1:1'].update(state='failed', failures=[{'cause': 'old'}],
                decision_request={'question': 'What next?'}, evidence={'review': {'outcome': 'fail'}})
        self.decide('retry')
        self.assertEqual(self.item()['state'], 'ready')
        self.assertEqual(self.item()['budget_start'], 1)
        self.assertFalse(self.item()['evidence'])
        self.assertNotIn('decision_request', self.item())
        self.assertIn('decision_answered', [e['kind'] for e in self.ledger.read()['events']])

    def test_stale_revision_and_active_or_cleaning_worker_are_refused(self):
        self.tracked(); self.github.value['labels'].append({'name': 'ready'})
        self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        self.decide('pause')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.decide('resume', revision=row['revision'])
        self.decide('resume'); self.sync()
        job = self.reserve()
        with self.assertRaisesRegex(ValueError, 'cleanup'):
            self.decide('pause')
        with self.ledger.transaction() as data:
            data['issues']['1:1']['active'] = None
            data['jobs'][job['id']]['outcome'] = 'waiting'
        with self.assertRaisesRegex(ValueError, 'cleanup'):
            self.decide('pause')

    def test_worker_reservation_between_inventory_read_and_commit_is_refused(self):
        self.tracked(); self.github.value['labels'].append({'name': 'ready'}); self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        view = self.inventory.view
        def racing_view(**kwargs):
            result = view(**kwargs)
            self.reserve()
            return result
        with patch.object(self.inventory, 'view', side_effect=racing_view), patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            with self.assertRaisesRegex(ValueError, 'cleanup'):
                decide(self.root, key='1:1', action='pause', revision=row['revision'], reason='Pause')
        self.assertIsNotNone(self.item()['active'])
        self.assertNotIn('operator_hold', self.item())

    def test_unreconciled_recovery_result_still_counts_as_an_active_attempt(self):
        self.tracked(); self.github.value['labels'].append({'name': 'ready'})
        with self.ledger.transaction() as data:
            data['jobs']['recovery'] = {'id': 'recovery', 'issue_key': '1:1', 'role': 'recovery',
                                        'phase': 'exited', 'outcome': None, 'cleanup': True}
        with self.assertRaisesRegex(ValueError, 'cleanup'):
            self.decide('pause')
        self.assertNotIn('operator_hold', self.item())

    def test_github_outage_and_lost_write_response_keep_dispatch_blocked(self):
        from forge.factory.github import GitHubUnavailable
        self.decide('queue')
        self.github.lose_response = True
        with self.assertRaises(GitHubUnavailable):
            self.sync()
        self.assertEqual(self.item()['state'], 'ready')
        with self.assertRaises(StateError): self.reserve()
        self.sync()
        self.assertNotIn('operator_sync', self.item())
        self.assertEqual(len([c for c in self.github.calls if c[0] == 'POST']), 1)

    def test_new_live_dependency_or_external_work_prevents_launch(self):
        for change in ('dependency', 'assignee', 'pull', 'rules', 'access'):
            with self.subTest(change=change):
                # Each case gets an independent durable state.
                self.setUp()
                self.decide('queue')
                if change == 'dependency': self.github.value['body'] = 'Depends on #8'
                if change == 'assignee': self.github.value['assignees'] = [{'login': 'human'}]
                if change == 'pull': self.github.prs = [{'number': 4, 'body': 'Closes #1'}]
                if change == 'rules': self.github.rules = False
                if change == 'access': self.settings.allowed = False
                self.sync()
                self.assertEqual(self.item()['state'], 'hold')
                self.assertEqual(self.item()['operator_hold']['kind'], 'conflict')
                with self.assertRaises(StateError): self.reserve()
                self.assertFalse(self.github.calls and any(c[0] == 'POST' for c in self.github.calls))

    def test_supervisor_cannot_overwrite_an_immediate_decision(self):
        self.tracked(); self.github.value['labels'].append({'name': 'ready'})
        snapshot = self.item()
        supervisor = Supervisor(self.ledger, Engine(), self.github, self.settings, self.actions.config)
        original = self.github.issue
        def during_read(item):
            old = original(item)
            self.decide('pause')
            return old
        with patch.object(self.github, 'issue', side_effect=during_read):
            with self.assertRaises(DecisionChanged): supervisor.reconcile(snapshot)
        self.assertEqual(self.item()['operator_hold']['kind'], 'pause')
        with self.assertRaises(DecisionChanged): supervisor.reconcile(snapshot)

    def test_stable_request_identity_is_idempotent(self):
        identifier = '22222222-2222-4222-8222-222222222222'
        self.refresh(); row = self.inventory.view(key='1:1')['issue']
        self.decide('queue', request_id=identifier)
        self.decide('queue', request_id=identifier, revision=row['revision'])
        self.assertEqual(len([e for e in self.ledger.read()['events'] if e.get('request') == identifier]), 1)
        with self.assertRaisesRegex(ValueError, 'identity'):
            self.decide('pause', request_id=identifier)

    def test_pause_during_dispatch_prevents_reservation(self):
        self.tracked(); self.github.value['labels'].append({'name': 'ready'})
        self.github.deadline = time.monotonic() + 60
        runner = Engine()
        supervisor = Supervisor(self.ledger, runner, self.github, self.settings, self.actions.config)
        def before_reserve(*args):
            self.decide('pause')
            return None
        with patch.object(self.github, 'own_draft', side_effect=before_reserve, create=True):
            supervisor.dispatch(discover_missing=False)
        self.assertEqual(self.item()['operator_hold']['kind'], 'pause')
        self.assertFalse(self.ledger.read()['jobs'])
        self.assertFalse(runner.jobs)

    def test_new_decision_survives_an_inflight_label_write(self):
        self.decide('queue')
        call = self.github.call
        def racing_call(role, item, method, path, body=None):
            value = call(role, item, method, path, body)
            if method == 'POST':
                self.decide('pause')
            return value
        with patch.object(self.github, 'call', side_effect=racing_call):
            with self.assertRaises(DecisionChanged): self.sync()
        self.assertEqual(self.item()['operator_sync']['action'], 'pause')
        self.sync()
        self.assertIn('hold', {l['name'] for l in self.github.value['labels']})
        self.assertEqual(self.item()['operator_hold']['kind'], 'pause')
