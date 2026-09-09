"""Real snapshot projection and SQLite authority across revision-bound rework.

GitHub responses and worker release proofs are fixture boundaries; no external
process, network request, or live checkout is used.
"""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge import workflow as engine, workflow_control as control
from forge.control import atomic_json, snapshot_path
from tests.host_isolation import deny_host_tmux


class WorkflowFailureCycleTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.state = Path(tmp.name) / 'state'
        self.config = Path(tmp.name) / 'repos.d'; self.config.mkdir()
        (self.config / 'fixture.repo').write_text('REPO=Org/Fixture\nOWNER_LABEL=auto:owned-by-test\n')
        self.repo = 'Org/Fixture'; self.now = 1000; self.sequence = 0
        clock = patch('time.time', side_effect=lambda: self.now)
        clock.start(); self.addCleanup(clock.stop)
        self.head = 'a' * 40; self.base = 'b' * 40
        self.snapshot('queued')

    def event(self):
        self.sequence += 1
        return 'failure-cycle-' + str(self.sequence)

    def snapshot(self, stage):
        self.now += 1
        labels = {'review': ['ready-for-review'], 'audit': ['ready-for-audit'], 'build': ['changes-requested']}
        self.labels = labels.get(stage, [])
        work = {k: [] for k in ('ready', 'wip', 'review', 'audit', 'sentback', 'limbo', 'conflicts', 'held')}
        work['ready' if stage == 'queued' else 'wip'] = [17]
        if stage in ('review', 'audit'): work[stage] = [22]
        if stage == 'build': work['sentback'] = [22]
        details = {'17': {'number': 17, 'kind': 'issue', 'state': 'open', 'title': 'Exact issue',
                         'labels': ['ready'] if stage == 'queued' else ['wip'],
                         'url': f'https://github.com/{self.repo}/issues/17'}}
        if stage != 'queued':
            details['22'] = {'number': 22, 'kind': 'pr', 'state': 'open', 'title': 'Exact PR',
                             'body': 'Closes #17', 'labels': self.labels,
                             'url': f'https://github.com/{self.repo}/pull/22'}
        snap = {'repo': self.repo, 'enabled': True, 'status': 'ok', 'observed_at': self.now,
                'work': work, 'item_details': details,
                'counts': {'builder': int(stage in ('queued', 'build')), 'review': int(stage == 'review'), 'audit': int(stage == 'audit')},
                'schedule': {'eligible': [17] if stage == 'queued' else [], 'active': [] if stage == 'queued' else [17],
                             'handed_off': [17] if stage in ('review', 'audit') else [],
                             'reserving': [17] if stage == 'build' else [], 'ordered': [17], 'blocked': {}}}
        atomic_json(snapshot_path(self.state, self.repo), snap)
        control.reconcile_repository(self.state, self.repo)

    def github(self, argv, **kwargs):
        if argv[:3] == ['gh', 'pr', 'view']:
            value = {'number': 22, 'url': f'https://github.com/{self.repo}/pull/22', 'state': 'OPEN',
                     'isDraft': False, 'headRefOid': self.head, 'baseRefOid': self.base,
                     'labels': [{'name': x} for x in self.labels], 'author': {'login': 'forge-bot'},
                     'closingIssuesReferences': [{'number': 17, 'url': f'https://github.com/{self.repo}/issues/17'}]}
        elif argv[:3] == ['gh', 'issue', 'view']:
            value = {'number': 17, 'url': f'https://github.com/{self.repo}/issues/17', 'state': 'OPEN', 'labels': [{'name': 'wip'}]}
        else: self.fail('Unexpected external command: ' + repr(argv))
        return SimpleNamespace(returncode=0, stdout=json.dumps(value), stderr='')

    def start(self, role):
        self.now += 0.1
        a = control.reserve(self.config, self.state, 'Fixture-' + role, run=self.github)
        generation = f'${self.sequence + 1}|100|%{self.sequence + 1}|10'
        for action in ('bind', 'submitted', 'working'):
            engine.transition(self.state, a['id'], action, source_event_id=self.event(), generation=generation,
                              **({'provider': 'codex'} if action == 'bind' else {}))
        return a, generation

    def release(self, attempt):
        a, generation = attempt
        engine.transition(self.state, a['id'], 'retired', generation=generation,
                          source_event_id=self.event(), proof={'verified': True, 'released': True,
                          'assignment_id': a['id'], 'generation': generation, 'kind': 'verified_handoff'})
        slot = next(s for s in engine.read_state(self.state)['slots'] if s['session'] == a['session'])
        self.assertEqual(slot['state'], 'free')

    def assert_issue(self, stage, disposition='pending'):
        issues = engine.read_state(self.state)['issues']
        self.assertEqual([(i['number'], i['stage'], i['disposition']) for i in issues], [(17, stage, disposition)])

    def test_review_and_audit_failures_return_exact_issue_to_build_pending(self):
        builder = self.start('build'); self.assert_issue('build', 'working')
        self.snapshot('review'); self.release(builder)
        reviewer = self.start('review'); old_review = reviewer[0]
        self.snapshot('build'); self.assert_issue('build')
        self.release(reviewer); self.assert_issue('build')
        rework = self.start('build')
        self.assertEqual(rework[0]['issue_numbers'], [17]); self.assertEqual(rework[0]['pr']['number'], 22)
        self.head = 'c' * 40
        self.snapshot('review'); self.release(rework)
        rereview = self.start('review')
        self.assertEqual(rereview[0]['pr']['head'], self.head)
        self.assertNotEqual(rereview[0]['id'], old_review['id'])
        self.snapshot('audit'); self.release(rereview)
        auditor = self.start('audit')
        self.snapshot('build'); self.assert_issue('build')
        self.release(auditor); self.assert_issue('build')
        second_rework = self.start('build')
        self.assertEqual(second_rework[0]['issue_numbers'], [17])
        history = engine.history(self.state, limit=1000)
        self.assertGreater(len(history), 20)
        assignments = engine.read_state(self.state)['assignments']
        self.assertEqual(len(assignments), 6)
        self.assertTrue(all(a['issue_numbers'] == [17] for a in assignments))
        self.assertEqual(next(a for a in assignments if a['id'] == old_review['id'])['pr']['head'], 'a' * 40)
        self.assertEqual(sum(a['state'] != 'retired' for a in assignments), 1)
