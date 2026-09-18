"""Issue inventory is observation; durable decisions alone change admission."""
from contextlib import nullcontext
from copy import deepcopy
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from forge.factory.issues import IssueInventory, facts, revision, presentation, dependencies, LEGACY_SETUP_REASON
from forge.factory.issue_actions import IssueActions, request
from forge.factory.github import GitHubUnavailable
from forge.factory.ledger import Ledger
from forge.factory.supervisor import Supervisor
from forge.settings import write_json
from tests.test_factory_cycle import Engine


class Settings:
    def __init__(self):
        self.allowed = True
        self.repos = [{'id': 1, 'name': 'example/project', 'enabled': True, 'access_ready': True,
                       'protection_ready': True, 'access': {'controller': 'ready'}, 'setup_url': '/settings/repository?repo_id=1'}]
    def view(self): return {'repositories': deepcopy(self.repos)}
    def load(self): return {'connections': {}}
    def locked(self, *args, **kwargs): return nullcontext()
    def paused(self): return False
    def admission(self, repo): return {'allowed': self.allowed}


class GitHub:
    def __init__(self):
        self.value = {'number': 1, 'title': 'An unqueued issue', 'body': 'Useful requirements', 'state': 'open',
                      'labels': [{'name': 'customer'}], 'assignees': []}
        self.prs, self.calls, self.pages = [], [], {}
        self.rules, self.available, self.lose_response = True, True, False
    def issue(self, item):
        if not self.available: raise GitHubUnavailable('Unavailable')
        return deepcopy(self.value), 'requirements'
    def target(self, item): return 'main'
    def protections(self, *args): return self.rules
    def pulls(self, item): return deepcopy(self.prs)
    def labels(self, item): pass
    def call(self, role, item, method, path, body=None):
        if not self.available: raise GitHubUnavailable('Unavailable')
        self.calls.append((method, path, body))
        if '?' in path: return deepcopy(self.pages.get(path, []))
        if method == 'GET': return {'number': int(path.split('/')[-1]), 'state': 'open'}
        if method == 'POST':
            self.value['labels'] += [{'name': n} for n in body['labels']]
            if self.lose_response:
                self.lose_response = False
                raise GitHubUnavailable('Connection lost after commit')
        if method == 'DELETE': self.value['labels'] = [l for l in self.value['labels'] if l['name'] != path.split('/')[-1]]
        return {}


class WorklistTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings, self.github = Settings(), GitHub()
        self.ledger = Ledger(self.root / 'state/factory')
        self.inventory = IssueInventory(self.root, settings=self.settings, github=self.github)
        self.actions = IssueActions(self.root, self.ledger, settings=self.settings, github=self.github)
        patcher = patch.object(IssueInventory, 'start_refresh'); patcher.start(); self.addCleanup(patcher.stop)

    def refresh(self):
        self.github.pages['/issues?state=open&sort=created&direction=asc&per_page=100&page=1'] = [self.github.value]
        self.inventory.refresh(force=True)

    def decision(self, action):
        self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            identifier = request(self.root, key='1:1', action=action, revision=row['revision'], reason='Prerequisite checked by operator')
        with self.actions.store.transaction() as data: value = deepcopy(data[identifier])
        return identifier, value

    def tracked(self, hold=None):
        self.ledger.discover(self.settings.repos[0], 1, 'requirements', hold=hold)

    def test_inventory_includes_backlog_and_closed_without_admitting_work(self):
        self.github.pages['/issues?state=closed&sort=created&direction=asc&per_page=100&page=1'] = [
            {**self.github.value, 'number': 2, 'state': 'closed'}, {**self.github.value, 'number': 3, 'pull_request': {}}]
        self.refresh()
        self.assertEqual(self.inventory.view()['issues'][0]['status'], 'backlog')
        self.assertEqual(self.inventory.view(state='completed')['issues'][0]['number'], 2)
        self.assertEqual(self.inventory.view(state='all')['total'], 2)
        self.assertFalse(self.ledger.read()['issues'])
        self.assertTrue(all(c[0] == 'GET' for c in self.github.calls))

    def test_pagination_and_filter_keep_all_repository_choices(self):
        self.github.pages['/issues?state=open&sort=created&direction=asc&per_page=100&page=1'] = [
            {**self.github.value, 'number': n} for n in range(1, 101)]
        self.github.pages['/issues?state=open&sort=created&direction=asc&per_page=100&page=2'] = [{**self.github.value, 'number': 101}]
        self.inventory.refresh(True)
        self.assertEqual(self.inventory.view(page=3)['issues'][0]['number'], 101)
        self.settings.repos.append({**self.settings.repos[0], 'id': 2, 'name': 'example/second'})
        self.assertEqual(len(self.inventory.view(repo_id=1)['repositories']), 2)
        self.assertTrue(self.inventory.view(repo_id=1)['complete'])

    def test_stale_and_partial_data_cannot_offer_mutations(self):
        self.refresh(); self.github.available = False; self.inventory.refresh(True)
        row = self.inventory.view()['issues'][0]
        self.assertFalse(row['fresh']); self.assertEqual(row['actions'], [])
        data = self.inventory.read(); entry = data['repositories']['1']
        entry.update(error=None, at=time.time(), complete=False, scan={'issues': {}})
        write_json(self.inventory.path, data)
        self.assertFalse(self.inventory.view()['issues'][0]['fresh'])

    def test_queue_is_explicit_preserves_labels_and_is_replayable(self):
        identifier, decision = self.decision('queue')
        self.assertFalse(self.ledger.read()['issues'])
        self.github.lose_response = True
        self.actions.process()
        item = self.ledger.read()['issues']['1:1']
        self.assertEqual(item['operator_request'], identifier)
        self.assertEqual(item['state'], 'hold')
        self.actions.process(); self.actions.process()
        item = self.ledger.read()['issues']['1:1']
        self.assertEqual(item['state'], 'ready'); self.assertNotIn('operator_request', item)
        self.assertEqual({l['name'] for l in self.github.value['labels']}, {'customer', 'ready'})
        self.assertEqual(len([e for e in self.ledger.read()['events'] if e.get('request') == identifier]), 1)
        self.assertEqual(len([c for c in self.github.calls if c[0] == 'POST']), 1)
        self.assertEqual(self.inventory.view()['issues'][0]['status'], 'queued')

    def test_changed_description_rejects_before_label_write(self):
        identifier, decision = self.decision('queue')
        self.github.value['body'] = 'Different requirements'
        self.actions.process()
        self.assertFalse(self.ledger.read()['issues'])
        self.assertFalse(any(c[0] != 'GET' for c in self.github.calls))
        with self.actions.store.transaction() as data: self.assertEqual(data[identifier]['status'], 'rejected')

    def test_queue_rechecks_live_access_rules_dependencies_and_existing_work(self):
        for change in ('access', 'rules', 'dependency', 'pull', 'assignee', 'wip'):
            with self.subTest(change=change):
                identifier, decision = self.decision('queue')
                if change == 'access': self.settings.allowed = False
                if change == 'rules': self.github.rules = False
                if change == 'dependency': self.github.value['body'] = 'Depends on #8'
                if change == 'pull': self.github.prs = [{'number': 4, 'body': 'Closes #1'}]
                if change == 'assignee': self.github.value['assignees'] = [{'login': 'human'}]
                if change == 'wip': self.github.value['labels'].append({'name': 'wip'})
                self.actions.process()
                self.assertFalse(self.ledger.read()['issues'])
                self.assertFalse(any(c[0] != 'GET' for c in self.github.calls))
                self.settings.allowed = self.github.rules = True
                self.github.prs = []; self.github.value.update(body='Useful requirements', assignees=[], labels=[{'name': 'customer'}])

    def test_deliberate_release_preserves_budget_and_removes_only_hold(self):
        self.tracked(hold='Manual decision')
        with self.ledger.transaction() as data: data['issues']['1:1'].update(budget_start=1, failures=[{'cause': 'Retained history'}])
        self.github.value['labels'] += [{'name': 'hold'}, {'name': 'ready'}]
        identifier, decision = self.decision('resume'); self.actions.process()
        item = self.ledger.read()['issues']['1:1']
        self.assertEqual(item['budget_start'], 1); self.assertEqual(item['state'], 'ready')
        self.assertEqual({l['name'] for l in self.github.value['labels']}, {'ready', 'customer'})
        self.assertFalse(any(e['kind'] == 'human_retry' for e in self.ledger.read()['events']))

    def test_open_dependency_prevents_explicit_release(self):
        self.tracked(hold='Manual decision'); self.github.value.update(body='Dependency: #8', labels=[{'name': 'hold'}])
        self.decision('resume'); self.actions.process()
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'hold')
        self.assertFalse(any(c[0] != 'GET' for c in self.github.calls))
        self.assertEqual(dependencies('Mention #4\nDependency: #8\nBlocked by #9'), [8, 9])

    def test_return_to_backlog_survives_reconcile(self):
        self.tracked(); self.github.value['labels'].append({'name': 'ready'})
        self.decision('backlog'); self.actions.process()
        supervisor = Supervisor(self.ledger, Engine(), self.github, self.settings, self.actions.config)
        supervisor.reconcile(self.ledger.read()['issues']['1:1'])
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'hold')
        self.assertEqual(self.inventory.view()['issues'][0]['status'], 'backlog')
        self.assertNotIn('ready', {l['name'] for l in self.github.value['labels']})

    def test_legacy_setup_migration_requires_exact_history_and_no_work(self):
        self.tracked(); self.ledger.hold('1:1', LEGACY_SETUP_REASON)
        self.assertTrue(self.ledger.migrate_protection_hold('1:1'))
        self.assertFalse(self.ledger.migrate_protection_hold('1:1'))
        self.assertEqual(self.ledger.read()['issues']['1:1']['prerequisite_hold']['state'], 'ready')

    def test_legacy_migration_does_not_erase_human_decision(self):
        self.tracked(); self.ledger.hold('1:1', 'Wait for operator'); self.ledger.hold('1:1', LEGACY_SETUP_REASON)
        self.assertFalse(self.ledger.migrate_protection_hold('1:1'))

    def test_legacy_recovery_requires_live_rules_and_respects_explicit_hold(self):
        self.tracked(); self.ledger.hold('1:1', LEGACY_SETUP_REASON)
        supervisor = Supervisor(self.ledger, Engine(), self.github, self.settings, self.actions.config)
        self.github.rules = False
        supervisor.reconcile(self.ledger.read()['issues']['1:1'])
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'hold')
        self.github.rules = True; self.github.value['labels'].append({'name': 'hold'})
        supervisor.reconcile(self.ledger.read()['issues']['1:1'])
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'hold')
        self.assertNotIn('prerequisite_hold', self.ledger.read()['issues']['1:1'])

    def test_legacy_recovery_pauses_existing_work(self):
        self.tracked(); self.ledger.hold('1:1', LEGACY_SETUP_REASON)
        self.github.value['labels'].append({'name': 'wip'})
        Supervisor(self.ledger, Engine(), self.github, self.settings, self.actions.config).reconcile(self.ledger.read()['issues']['1:1'])
        self.assertIn('Existing work', self.ledger.read()['issues']['1:1']['reason'])

    def test_retry_never_releases_a_hold(self):
        self.tracked(hold='Wait for a decision'); self.refresh()
        with self.assertRaises(ValueError): self.decision('retry')
        with self.assertRaises(ValueError): self.actions.validate_action('retry', facts(self.github.value), self.ledger.read()['issues']['1:1'])

    def test_active_attempt_and_cleanup_prevent_queue_mutations(self):
        self.tracked(); identifier, decision = self.decision('queue')
        with self.ledger.transaction() as data:
            data['issues']['1:1']['active'] = 'running'
            data['jobs']['running'] = {'id': 'running', 'issue_key': '1:1', 'role': 'build', 'cleanup': False, 'slot': 'build-1'}
            data['slots']['build-1'] = 'running'
        with self.assertRaisesRegex(ValueError, 'active attempt'): self.actions.apply(identifier, decision)

    def test_applied_event_recovers_pending_flag_after_restart(self):
        self.tracked(); identifier, decision = self.decision('queue')
        with self.ledger.transaction() as data:
            item = data['issues']['1:1']; item['operator_request'] = identifier
            self.ledger.event(data, item, 'operator_queue', request=identifier)
        self.actions.apply(identifier, decision)
        self.assertNotIn('operator_request', self.ledger.read()['issues']['1:1'])

    def test_pending_pause_blocks_dispatch_even_before_github_can_be_read(self):
        self.tracked(); self.github.value['labels'].append({'name': 'ready'})
        identifier, decision = self.decision('pause')
        self.github.available = False; self.actions.process()
        item = self.ledger.read()['issues']['1:1']
        self.assertEqual(item['operator_request'], identifier)
        supervisor = Supervisor(self.ledger, Engine(), self.github, self.settings, self.actions.config)
        self.assertEqual(supervisor.choose('build', self.ledger.read()), (None, None))
        self.github.available = True; self.actions.process()
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'hold')

    def test_legacy_setup_recovers_once_without_retry(self):
        self.tracked(); self.ledger.hold('1:1', LEGACY_SETUP_REASON)
        supervisor = Supervisor(self.ledger, Engine(), self.github, self.settings, self.actions.config)
        supervisor.reconcile(self.ledger.read()['issues']['1:1'])
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'ready')
        self.assertEqual([e['kind'] for e in self.ledger.read()['events']][-2:], ['protection_hold_migrated', 'protection_restored'])

    def test_same_reason_after_human_history_cannot_use_setup_recheck(self):
        self.tracked(); self.ledger.hold('1:1', 'Deliberate hold'); self.ledger.hold('1:1', LEGACY_SETUP_REASON)
        self.decision('recheck'); self.actions.process()
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'hold')
        self.assertNotIn('prerequisite_hold', self.ledger.read()['issues']['1:1'])

    def test_old_retry_endpoint_cannot_bypass_issue_decisions(self):
        from forge.factory.control import queue
        self.tracked(hold='Deliberate hold')
        with self.assertRaisesRegex(ValueError, 'Open the issue'):
            queue(self.root, 'retry', '1:1', 'Wrong action for this hold')
