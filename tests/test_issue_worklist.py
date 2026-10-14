"""Issue inventory is observation; durable decisions alone change admission."""
from contextlib import nullcontext
from copy import deepcopy
from urllib.parse import unquote
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from forge.factory.issues import (IssueInventory, SUMMARIES, facts, revision, presentation,
                                  dependencies, LEGACY_SETUP_REASON)
from forge.factory.contracts import HEADLINE_LIMIT, PLAIN_LIMIT, PLAIN_SENTENCES
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
        self.people, self.people_calls, self.fail_people = [{'login': 'maintainer', 'name': 'A Maintainer'}, {'login': 'second-pair', 'name': ''}], 0, False
        self.rules, self.available, self.lose_response = True, True, False
    def issue(self, item):
        if not self.available: raise GitHubUnavailable('Unavailable')
        return deepcopy(self.value), 'requirements'
    def target(self, item): return 'main'
    def publisher(self, role): return role + '[bot]'
    def protections(self, *args): return self.rules
    def pulls(self, item): return deepcopy(self.prs)
    def labels(self, item): pass
    def recent_authors(self, item): return list(getattr(self, 'authors', []))
    def label_events(self, item, number): return list(getattr(self, 'events', {}).get(number, []))
    def collaborators(self, item):
        self.people_calls += 1
        if not self.available or self.fail_people: raise GitHubUnavailable('Unavailable')
        return deepcopy(self.people)
    def call(self, role, item, method, path, body=None):
        if not self.available: raise GitHubUnavailable('Unavailable')
        self.calls.append((method, path, body))
        if '?' in path: return deepcopy(self.pages.get(path, []))
        if method == 'GET': return {'number': int(path.split('/')[-1]), 'state': 'open'}
        if method == 'POST':
            if 'assignees' in body:
                self.value['assignees'] = [{'login': n} for n in body['assignees']]
            elif 'labels' in body:
                self.value['labels'] += [{'name': n} for n in body['labels']]
            if self.lose_response:
                self.lose_response = False
                raise GitHubUnavailable('Connection lost after commit')
        if method == 'PATCH' and path == '/issues/1': self.value['state'] = 'closed'
        if method == 'DELETE': self.value['labels'] = [l for l in self.value['labels'] if l['name'] != unquote(path.split('/')[-1])]
        return {}


class PlainSummaryTests(unittest.TestCase):
    """One headline sentence and at most three sentences, whatever the state."""

    runtime = {'hold_labels': {'hold', 'blocked'}, 'ready_label': 'ready', 'enabled': True, 'human_reviewers': []}
    repo = {'name': 'example/project', 'enabled': True, 'access_ready': True, 'protection_ready': True}

    def card(self, item=None, issue=None, runtime=None):
        base = {'number': 1, 'title': 'An issue', 'state': 'open', 'labels': [], 'assignees': [], 'body': ''}
        return presentation({**base, **(issue or {})}, item or {}, self.repo, runtime or self.runtime)['summary']

    def test_forge_writes_the_summary_for_every_state_a_worker_has_not_spoken_for(self):
        cases = [
            ({}, {'labels': [{'name': 'ready'}]}, 'Waiting for a builder'),
            ({}, {}, 'Not queued for Forge'),
            ({'active': 'job', 'active_role': 'build'}, {}, 'Writing the change'),
            ({'state': 'built'}, {}, 'Built and waiting for review'),
            ({'state': 'merged'}, {}, 'The work is merged'),
            ({'operator_request': 'request'}, {}, 'Applying your decision'),
            ({'operator_hold': {'kind': 'pause'}, 'reason': 'Waiting for the design review'}, {}, 'Paused by an operator'),
            ({'prerequisite_hold': {'state': 'ready'}}, {}, 'Ready to start once you recheck'),
            ({}, {'labels': [{'name': 'blocked'}]}, 'Paused by a label on GitHub'),
        ]
        for item, issue, headline in cases:
            with self.subTest(headline=headline):
                summary = self.card(item, issue)
                self.assertEqual(summary['headline'], headline)
                self.assertLessEqual(len(summary['headline']), HEADLINE_LIMIT)
                self.assertLessEqual(len(summary['plain']), PLAIN_LIMIT)
                self.assertLessEqual(len([s for s in summary['plain'].split('. ') if s]), PLAIN_SENTENCES)
        self.assertEqual(self.card({}, {'labels': [{'name': 'ready'}]}, {**self.runtime, 'enabled': False})['headline'],
                         'Forge is paused')

    def test_every_copied_summary_is_one_headline_and_three_short_sentences(self):
        for case, (headline, plain) in SUMMARIES.items():
            with self.subTest(case=case):
                self.assertLessEqual(len(headline), HEADLINE_LIMIT)
                self.assertNotIn('.', headline)
                self.assertLessEqual(len([s for s in plain.split('. ') if s]), PLAIN_SENTENCES)

    def test_approval_guidance_requires_current_human_gate(self):
        issue = {'number': 1, 'state': 'open', 'labels': [], 'assignees': []}
        item = {'state': 'approval_needed', 'candidate': {'pr': 24, 'key': 'current'},
                'merge_gate': {'candidate': 'current', 'kind': 'approval', 'reason': 'Check the rotation.'}}
        def project(**kwargs):
            return presentation(issue, item, self.repo, self.runtime, **kwargs)
        self.assertEqual(project()['approval'], {'pr': 24, 'reason': 'Check the rotation.'})
        # Following a GitHub review link is read-only; stale inventory still
        # suppresses decisions, but must not hide the way to inspect the PR.
        self.assertIsNotNone(project(fresh=False)['approval'])
        self.assertEqual(project(fresh=False)['actions'], [])
        item.update(active='merge-job', active_role='merge')
        self.assertIsNotNone(project()['approval'])
        item.update(active=None)
        item['merge_gate']['kind'] = 'blocked'
        self.assertIsNone(project()['approval'])
        self.assertEqual(project()['actions'], [])
        self.assertIsNotNone(project()['merge_blocker'])
        item.update(active='merge-job', active_role='merge')
        item['merge_gate']['kind'] = 'refused'
        self.assertEqual(project()['status'], 'attention')
        self.assertIsNone(project()['approval'])
        item['candidate']['key'] = 'new'
        self.assertIsNone(project()['merge_blocker'])
        self.assertIsNone(project()['approval'])
        item.update(state='merged', active=None)
        self.assertIsNone(project()['merge_blocker'])

    def test_existing_approval_requests_keep_the_github_link_without_new_metadata(self):
        issue = {'number': 1, 'state': 'open', 'labels': [], 'assignees': []}
        item = {'state': 'approval_needed', 'candidate': {'pr': 24, 'key': 'current'},
                'reason': 'Check the key rotation.',
                'evidence': {'audit': {'human_required': 'required', 'human_reason': 'Check the key rotation.'}}}
        def project():
            return presentation(issue, item, self.repo, self.runtime, fresh=False)
        self.assertEqual(project()['approval']['pr'], 24)
        for reason in ('A current GitHub review requests changes',
                       'Current review and audit checks must match the accepted evidence',
                       'Configure native human CODEOWNERS protection before automatically merging escalated changes'):
            item['reason'] = reason
            self.assertIsNone(project()['approval'])
        item['reason'] = 'Check the key rotation.'
        item['merge_gate'] = {'candidate': 'current', 'kind': 'ready', 'reason': None}
        self.assertIsNone(project()['approval'])
        item['merge_gate']['kind'] = 'refused'
        self.assertIsNone(project()['approval'])
        self.assertIsNotNone(project()['merge_blocker'])

    def test_a_reader_never_has_to_open_details_to_learn_why(self):
        reason = ('The documentation artifact itself is accurate and low-risk to merge, but its claims '
                  'about the deployment were not verified end to end.')
        waiting = self.card({'state': 'approval_needed', 'reason': reason, 'candidate': {'pr': 24}})
        self.assertEqual(waiting['headline'], 'Waiting for a person to approve the pull request')
        self.assertIn('accurate and low-risk to merge', waiting['plain'])
        audited = self.card({'state': 'approval_needed', 'candidate': {'pr': 24},
                             'evidence': {'audit': {'human_reason': 'A person must sign off on the key rotation.'}}})
        self.assertIn('sign off on the key rotation', audited['plain'])
        held = self.card({'state': 'hold', 'reason': 'Waiting for the migration to run in production'})
        self.assertIn('Waiting for the migration to run in production.', held['plain'])

    def test_the_worker_leads_where_the_worker_is_the_one_who_knows(self):
        written = {'headline': 'Stopped: the tests fail on the login page', 'role': 'build', 'at': 10,
                   'plain': 'Signing in no longer works after the change. Forge stopped the attempt.'}
        failed = self.card({'state': 'failed', 'reason': 'Tests failed', 'last_summary': written})
        self.assertEqual(failed['headline'], written['headline'])
        self.assertEqual(failed['plain'], written['plain'])
        blocked = self.card({'state': 'hold', 'last_summary': written,
                             'environment_hold': {'state': 'built', 'blocker': 'resources', 'detail': 'Out of memory',
                                                  'summary': {**written, 'headline': 'Stopped: the worker ran out of memory'}}})
        self.assertEqual(blocked['headline'], 'Stopped: the worker ran out of memory')
        # Without worker words Forge still says what happened and why.
        plain = self.card({'state': 'failed', 'reason': 'No verified publication or usable result'})
        self.assertEqual(plain['headline'], 'Stopped: the last attempt failed')
        self.assertIn('No verified publication or usable result.', plain['plain'])

    def test_a_long_worker_summary_is_shortened_not_shown_whole(self):
        summary = self.card({'state': 'failed', 'last_summary': {
            'headline': 'w' * 200,
            'plain': 'One. Two. Three. Four must not reach the page. ' + 'path/to/file.ts ' * 100}})
        self.assertEqual(len(summary['headline']), HEADLINE_LIMIT)
        self.assertEqual(summary['plain'], 'One. Two. Three.')


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

    def decision(self, action, reason='Prerequisite checked by operator'):
        self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            identifier = request(self.root, key='1:1', action=action, revision=row['revision'], reason=reason)
        with self.actions.store.transaction() as data: value = deepcopy(data[identifier])
        return identifier, value

    def tracked(self, hold=None):
        self.ledger.discover(self.settings.repos[0], 1, 'requirements', hold=hold)

    def test_issue_environment_override_is_durable_and_does_not_queue_backlog(self):
        from forge.factory.config import validate
        self.actions.config = validate({'environment_tiers': {'full': {'image': 'sha256:' + 'a' * 64, 'memory': '4g', 'cpus': 2}}})
        self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            identifier = request(self.root, key='1:1', action='environment', revision=row['revision'],
                                 reason='Use MongoDB for this issue', environment={'mode': 'full', 'services': ['mongodb']})
        with self.actions.store.transaction() as data: decision = deepcopy(data[identifier])
        self.actions.apply(identifier, decision)
        item = self.ledger.read()['issues']['1:1']
        self.assertEqual(item['environment_override'], {'mode': 'full', 'services': ['mongodb']})
        self.assertEqual(item['state'], 'hold')
        self.assertEqual(item['operator_hold']['kind'], 'backlog')
        self.actions.apply(identifier, decision)
        events = [e for e in self.ledger.read()['events'] if e.get('request') == identifier]
        self.assertEqual(len(events), 1)
        self.assertFalse(self.ledger.read()['jobs'])

    def test_return_to_automatic_clears_issue_override_and_labels_without_releasing_hold(self):
        self.tracked(hold='Operator review needed')
        self.github.value['labels'].append({'name': 'env:full'})
        with self.ledger.transaction() as data:
            item = data['issues']['1:1']
            item['environment_override'] = {'mode': 'full', 'services': ['mongodb']}
            item['environment_choice'] = {'tier': 'full', 'services': ['mongodb'], 'automatic': False}
        self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            identifier = request(self.root, key='1:1', action='environment', revision=row['revision'],
                                 reason='Use automatic selection', environment={'mode': 'automatic', 'services': []})
        with self.actions.store.transaction() as data: decision = deepcopy(data[identifier])
        self.actions.apply(identifier, decision)
        item = self.ledger.read()['issues']['1:1']
        self.assertNotIn('environment_override', item)
        self.assertNotIn('environment_choice', item)
        self.assertEqual(item['state'], 'hold')
        self.assertEqual(item['reason'], 'Operator review needed')
        self.assertNotIn('env:full', [label['name'] for label in self.github.value['labels']])

    def test_stale_environment_override_is_rejected(self):
        self.tracked(); self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            identifier = request(self.root, key='1:1', action='environment', revision=row['revision'],
                                 reason='Use standard', environment={'mode': 'standard', 'services': []})
        with self.actions.store.transaction() as data: decision = deepcopy(data[identifier])
        with self.ledger.transaction() as data:
            data['issues']['1:1']['environment_choice'] = {'tier': 'full', 'services': ['mongodb']}
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.actions.apply(identifier, decision)
        self.assertNotIn('environment_override', self.ledger.read()['issues']['1:1'])

    def test_environment_override_cannot_admit_untracked_work_with_an_assignee(self):
        self.github.value['labels'].append({'name': self.actions.config['ready_label']})
        self.github.value['assignees'] = [{'login': 'maintainer'}]
        self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        with patch('forge.factory.issue_actions.IssueInventory', return_value=self.inventory):
            identifier = request(self.root, key='1:1', action='environment', revision=row['revision'],
                                 reason='Use standard', environment={'mode': 'standard', 'services': []})
        with self.actions.store.transaction() as data: decision = deepcopy(data[identifier])
        with self.assertRaisesRegex(ValueError, 'assignee'):
            self.actions.apply(identifier, decision)
        self.assertNotIn('1:1', self.ledger.read()['issues'])

    def awaiting_approval(self, pr=30):
        self.tracked()
        with self.ledger.transaction() as data:
            data['issues']['1:1'].update(state='approval_needed', reason='The pull request needs a human review',
                                         candidate={'pr': pr, 'head': 'abc123', 'base': 'def456', 'base_ref': 'main'})
        self.github.prs = [{'number': pr, 'title': 'Forge pull request', 'body': 'Closes #1'}]

    def test_the_audit_badge_reads_the_human_decision_word_not_its_truthiness(self):
        self.awaiting_approval()
        for word, expected in (('not_required', False), ('required', True)):
            with self.ledger.transaction() as data:
                data['issues']['1:1']['evidence'] = {'audit': {'outcome': 'pass', 'summary': 'PASS.', 'human_required': word,
                                                               'human_reason': 'reason'}}
            reviews = self.inventory.view(key='1:1')['issue']['reviews']
            self.assertEqual([r['human_required'] for r in reviews if r['role'] == 'audit'], [expected], word)

    def test_environment_recheck_preserves_stage_and_failure_budget(self):
        self.tracked()
        with self.ledger.transaction() as state:
            state['issues']['1:1'].update(state='hold', reason='Forge environment needs attention',
                environment_hold={'state': 'built', 'blocker': 'toolchain', 'detail': 'Missing compiler'})
        identifier, _ = self.decision('recheck')
        self.actions.process()
        with self.actions.store.transaction() as requests:
            self.assertEqual(requests[identifier]['status'], 'applied')
        item = self.ledger.read()['issues']['1:1']
        self.assertEqual(item['state'], 'built')
        self.assertEqual(item['budget_start'], 0)
        self.assertNotIn('environment_hold', item)

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
        # A routine rescan in progress: the issue is still known from the last
        # complete listing, so its actions stay (they are re-checked against
        # GitHub before anything is applied). Stale or failed data still offers none.
        entry.update(error=None, at=time.time(), complete=False, scan={'issues': {}})
        write_json(self.inventory.path, data)
        self.assertTrue(self.inventory.view()['issues'][0]['fresh'])

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
        held = self.ledger.read()['issues']['1:1']
        self.assertEqual(held['ownership_wait']['headline'], 'Marked work in progress')
        self.assertTrue(held['prerequisite_hold'])
        self.github.value['labels'] = []
        Supervisor(self.ledger, Engine(), self.github, self.settings, self.actions.config).reconcile(held)
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'ready')

    def test_retry_releases_only_the_approach_hold_with_a_recorded_decision(self):
        from forge.factory.ledger import APPROACH_HOLD
        self.tracked(hold=APPROACH_HOLD); self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        self.assertEqual(row['actions'], ['retry'])
        self.assertIn('new approach', row['explanation'])
        identifier, decision = self.decision('retry')
        self.actions.apply(identifier, decision)
        issue = self.ledger.read()['issues']['1:1']
        self.assertEqual(issue['state'], 'ready')
        self.assertEqual(issue['retry_plan'], decision['reason'])

    def test_retry_never_releases_a_hold(self):
        self.tracked(hold='Wait for a decision'); self.refresh()
        with self.assertRaises(ValueError): self.decision('retry')
        with self.assertRaises(ValueError): self.actions.validate_action('retry', facts(self.github.value), self.ledger.read()['issues']['1:1'])

    def test_retry_allows_verified_own_drafts_but_never_a_foreign_author(self):
        self.tracked()
        with self.ledger.transaction() as data:
            data['issues']['1:1']['state'] = 'failed'
        self.github.prs = [{'number': 22, 'body': 'Fixes #1', 'state': 'open', 'draft': True,
                            'head': {'ref': 'forge/issue-1-previous', 'repo': {'id': 1}},
                            'base': {'repo': {'id': 1}}, 'user': {'login': 'build[bot]', 'type': 'Bot'}}]
        identifier, decision = self.decision('retry', 'Continue the existing draft')
        item = self.ledger.read()['issues']['1:1']
        self.github.prs[0]['user']['login'] = 'another[bot]'
        with self.assertRaisesRegex(ValueError, 'Pull request #22 says it resolves this issue'):
            self.actions.prerequisites(decision, self.github.value, item)
        self.github.prs[0]['body'] = 'Phase 1 no longer waits on #1; only phase 3 does.'
        self.actions.prerequisites(decision, self.github.value, item)  # a mention is not a claim
        self.github.prs[0].update(body='Fixes #1')
        self.github.prs[0]['user']['login'] = 'build[bot]'
        self.actions.apply(identifier, decision)
        self.assertEqual(self.ledger.read()['issues']['1:1']['state'], 'ready')

    def test_builder_decision_is_visible_and_existing_retry_records_the_answer(self):
        self.tracked(hold='Choose a supported approach')
        question = {'question': 'Wait for upstream?', 'options': ['Wait', 'Remove the dependency'],
                    'recommendation': 'Wait', 'evidence': 'No compatible release exists.', 'attempt': 'previous'}
        with self.ledger.transaction() as data:
            data['issues']['1:1']['decision_request'] = question
        self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        self.assertEqual(row['decision_request'], question)
        self.assertEqual(row['actions'], ['retry'])
        identifier, decision = self.decision('retry', 'Remove the dependency and replace its usage')
        self.actions.apply(identifier, decision)
        issue = self.ledger.read()['issues']['1:1']
        self.assertEqual(issue['state'], 'ready')
        self.assertEqual(issue['retry_plan'], decision['reason'])
        self.assertNotIn('decision_request', issue)

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

    def test_decision_feedback_is_chronological_and_keeps_older_pending_requests(self):
        with self.actions.store.transaction() as data:
            for index in range(25):
                data[str(100-index)] = {'key':'1:1','created_at':index,'status':'pending' if index == 0 else 'applied','message':str(index)}
        statuses=self.inventory.action_statuses()
        self.assertEqual(len(statuses),21)
        self.assertEqual(statuses[0]['status'],'pending')
        self.assertEqual(statuses[-1]['message'],'24')

    def test_running_investigation_is_work_not_cleanup(self):
        self.tracked(); self.refresh()
        with self.ledger.transaction() as data:
            data['issues']['1:1']['state']='investigating'
            data['jobs']['recovery']={'id':'recovery','issue_key':'1:1','role':'recovery','phase':'running','cleanup':False,'slot':'recovery-1'}
            data['slots']['recovery-1']='recovery'
        row=self.inventory.view(key='1:1')['issue']
        self.assertEqual(row['status'],'working')
        self.assertEqual(row['active'],'recovery')
        self.assertEqual(row['explanation'],'Investigating a failed attempt')
        self.assertEqual(row['actions'],[])

    def test_recent_failed_refresh_retries_on_next_poll(self):
        self.refresh()
        self.github.available=False; self.inventory.refresh(force=True)
        self.assertFalse(self.inventory.view()['complete'])
        self.github.available=True; self.inventory.refresh()
        self.assertTrue(self.inventory.view()['complete'])
        self.assertIsNone(self.inventory.view()['repositories'][0]['error'])

    def test_close_drops_the_issue_and_its_pull_request(self):
        self.awaiting_approval(); self.refresh()
        self.assertTrue(self.inventory.view(key='1:1')['issue']['closable'])
        identifier, decision = self.decision('close', reason='Superseded by #9')
        self.actions.apply(identifier, decision)
        self.assertEqual([c for c in self.github.calls if c[0] in ('POST', 'PATCH')], [
            ('PATCH', '/pulls/30', {'state': 'closed'}),
            ('POST', '/issues/1/comments', {'body': 'Closed from Forge as not planned: Superseded by #9'}),
            ('PATCH', '/issues/1', {'state': 'closed', 'state_reason': 'not_planned'})])
        data = self.ledger.read()
        self.assertEqual(data['issues']['1:1']['state'], 'cancelled')
        self.assertEqual(self.inventory.view(key='1:1')['issue']['status'], 'dropped')
        # A restarted controller replays the decision without commenting twice.
        self.actions.apply(identifier, decision)
        self.assertEqual(len([c for c in self.github.calls if c[0] in ('POST', 'PATCH')]), 3)
        self.refresh()
        self.assertEqual(self.inventory.view(key='1:1')['issue']['status'], 'dropped')

    def test_issues_closed_as_not_planned_are_dropped_not_completed(self):
        self.tracked()
        for reason, status in (('not_planned', 'dropped'), ('duplicate', 'dropped'), ('completed', 'completed'), (None, 'completed')):
            with self.subTest(reason=reason):
                self.github.value.update(state='closed', state_reason=reason)
                self.refresh()
                row = self.inventory.view(key='1:1')['issue']
                self.assertEqual(row['status'], status)
                self.assertFalse(row['closable'])
        self.github.value.update(state='closed', state_reason='not_planned'); self.refresh()
        self.assertEqual([r['key'] for r in self.inventory.view(state='dropped')['issues']], ['1:1'])
        self.assertEqual(self.inventory.view(state='completed')['issues'], [])
        self.assertEqual(self.inventory.view(state='open')['issues'], [])

    def test_closing_from_forge_records_the_reason(self):
        self.awaiting_approval(); self.refresh()
        identifier, decision = self.decision('close', reason='Superseded by #9')
        self.actions.apply(identifier, decision)
        self.assertEqual(self.ledger.read()['issues']['1:1']['closed_as'], 'not_planned')

    def test_a_hold_label_is_on_hold_not_attention(self):
        self.tracked(hold='Waiting for dry-run evidence')
        self.github.value['labels'].append({'name': 'hold'}); self.refresh()
        row = self.inventory.view(key='1:1')['issue']
        self.assertEqual((row['status'], row['actions']), ('waiting', ['resume']))

    def test_a_close_the_ledger_saw_wins_over_a_stale_open_copy(self):
        self.tracked(hold='Configure two approving reviews and stale-approval dismissal before dispatch'); self.refresh()
        for reason, status in (('completed', 'completed'), ('not_planned', 'dropped'), ('duplicate', 'dropped'), (None, 'completed')):
            with self.subTest(reason=reason):
                self.ledger.cancel('1:1', 'Issue closed externally',
                    github_issue={**self.github.value, 'state': 'closed', 'state_reason': reason})
                row = self.inventory.view(key='1:1')['issue']
                self.assertEqual(row['state'], 'closed')
                self.assertEqual(row['status'], status)
                self.assertFalse(row['closable'])
                self.assertEqual(row['actions'], [])

    def test_operator_stopped_open_issue_can_resume(self):
        self.tracked(); self.refresh()
        from forge.factory.control import queue, requests
        job = self.ledger.reserve('1:1', 'build', 'worker-1', publisher='builder[bot]', policy='policy', timeout=600)
        queue(self.root, 'stop', job['id'], 'Operator stopped this attempt')
        requests(self.root, self.ledger)
        self.ledger.release(job['id'])
        row = self.inventory.view(key='1:1')['issue']
        self.assertEqual((row['state'], row['status'], row['actions']), ('open', 'attention', ['resume']))
        self.assertEqual(row['summary']['headline'], 'The previous attempt was stopped')

    def test_newer_inventory_reopen_wins_over_ledger_closure(self):
        self.tracked(); self.refresh()
        self.ledger.cancel('1:1', 'Issue closed externally',
            github_issue={**self.github.value, 'state': 'closed', 'state_reason': 'not_planned'})
        self.assertEqual(self.inventory.view(key='1:1')['issue']['status'], 'dropped')
        self.refresh()  # GitHub now returns an open issue after the recorded closure.
        row = self.inventory.view(key='1:1')['issue']
        self.assertEqual((row['state'], row['status'], row['actions']), ('open', 'attention', ['resume']))

    def test_newer_direct_reopen_wins_over_ledger_closure(self):
        self.tracked(); self.refresh()
        self.ledger.cancel('1:1', 'Issue closed externally',
            github_issue={**self.github.value, 'state': 'closed', 'state_reason': 'completed'})
        with self.ledger.transaction() as data:
            data['issues']['1:1']['github_observation'] = {'at': time.time(), 'issue': facts(self.github.value)}
        row = self.inventory.view(key='1:1')['issue']
        self.assertEqual((row['state'], row['status'], row['actions']), ('open', 'attention', ['resume']))

    def test_close_works_for_an_issue_forge_never_tracked(self):
        identifier, decision = self.decision('close', reason='Not needed')
        self.actions.apply(identifier, decision)
        self.assertEqual(self.github.value['state'], 'closed')
        self.assertNotIn('1:1', self.ledger.read()['issues'])

    def test_assign_reviewer_requests_the_review_and_assigns_the_issue(self):
        self.awaiting_approval()
        identifier, decision = self.decision('assign_reviewer', reason='maintainer')
        row = self.inventory.view(key='1:1')['issue']
        self.assertEqual(row['actions'], ['assign_reviewer'])
        self.assertEqual(row['reviewers'], [{'login': 'maintainer', 'name': 'A Maintainer', 'source': 'collaborator'},
                                            {'login': 'second-pair', 'name': '', 'source': 'collaborator'}])
        self.actions.process()
        self.assertEqual([c for c in self.github.calls if c[0] == 'POST'],
                         [('POST', '/pulls/30/requested_reviewers', {'reviewers': ['maintainer']}),
                          ('POST', '/issues/1/assignees', {'assignees': ['maintainer']})])
        item = self.ledger.read()['issues']['1:1']
        self.assertEqual(item['state'], 'approval_needed')
        self.assertEqual(item['reviewers_requested']['logins'], ['maintainer'])
        self.assertEqual(item['reviewers_requested']['request'], identifier)
        self.assertNotIn('operator_request', item)
        self.assertEqual({l['name'] for l in self.github.value['labels']}, {'customer'})
        self.assertEqual([e['kind'] for e in self.ledger.read()['events']][-1], 'reviewer_requested')
        after = self.inventory.view(key='1:1')['issue']
        self.assertEqual(after['status'], 'attention')
        self.assertEqual(after['reviewers_requested']['logins'], ['maintainer'])

    def test_assign_reviewer_applies_once_per_decision_and_can_be_repeated(self):
        self.awaiting_approval()
        identifier, decision = self.decision('assign_reviewer', reason='@maintainer, @second-pair')
        self.actions.apply(identifier, decision)
        self.actions.apply(identifier, decision)
        posts = [c for c in self.github.calls if c[0] == 'POST']
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0][2], {'reviewers': ['maintainer', 'second-pair']})
        self.assertEqual(posts[1][2], {'assignees': ['maintainer', 'second-pair']})
        with self.actions.store.transaction() as data: data[identifier]['status'] = 'applied'
        again, repeated = self.decision('assign_reviewer', reason='maintainer, second-pair')
        self.assertNotEqual(again, identifier)
        self.actions.apply(again, repeated)
        self.assertEqual(len([c for c in self.github.calls if c[0] == 'POST']), 4)
        item = self.ledger.read()['issues']['1:1']
        self.assertEqual(item['reviewers_requested']['request'], again)
        self.assertEqual(item['state'], 'approval_needed')

    def test_assign_reviewer_needs_a_pull_request_waiting_for_approval(self):
        self.tracked(); self.refresh()
        self.assertEqual(self.inventory.view(key='1:1')['issue']['actions'], ['queue'])
        with self.assertRaises(ValueError): self.decision('assign_reviewer', reason='maintainer')
        with self.assertRaisesRegex(ValueError, 'waits for approval'):
            self.actions.validate_action('assign_reviewer', facts(self.github.value), self.ledger.read()['issues']['1:1'])
        with self.ledger.transaction() as data: data['issues']['1:1'].update(state='approval_needed')
        with self.assertRaisesRegex(ValueError, 'waits for approval'):
            self.actions.validate_action('assign_reviewer', facts(self.github.value), self.ledger.read()['issues']['1:1'])
        self.assertFalse(any(c[0] != 'GET' for c in self.github.calls))

    def test_reviewer_decision_records_github_logins_only(self):
        from forge.factory.store_operator import reviewer_logins
        self.assertEqual(reviewer_logins(' @Maintainer, second-pair  maintainer '), ['Maintainer', 'second-pair'])
        for value in ('', '   ', '@@', 'not a login!', ',', 'a' * 40, ' '.join(str(n) for n in range(11))):
            with self.subTest(value=value), self.assertRaises(ValueError): reviewer_logins(value)

    def test_reviewer_choices_are_cached_and_a_failed_listing_leaves_none(self):
        self.refresh(); self.refresh()
        self.assertEqual(self.github.people_calls, 1)
        self.assertEqual([r['login'] for r in self.inventory.view(key='1:1')['issue']['reviewers']],
                         ['maintainer', 'second-pair'])
        data = self.inventory.read(); data['repositories']['1']['collaborators']['at'] = 0
        write_json(self.inventory.path, data)
        self.github.fail_people = True
        self.refresh()
        self.assertEqual(self.github.people_calls, 2)
        self.assertEqual(self.inventory.view(key='1:1')['issue']['reviewers'], [])
