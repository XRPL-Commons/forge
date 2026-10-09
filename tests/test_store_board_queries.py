"""Board selectors retain lifecycle and completion-event semantics."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.store_inventory import StoreInventory
from forge.factory.store_views import PipelineViews
from forge.store.migration.importer import Importer
from tests.test_sqlite_runtime_support import RuntimeFixture
from tests import test_store_import as imports


class BoardCompletionTests(TestCase):
    setUp = imports.ImportTests.setUp
    write = imports.ImportTests.write
    snapshot = imports.ImportTests.snapshot

    def test_completion_windows_use_last_event_sequence_and_count_issue_outcomes(self):
        now = 1_000_000
        states = [('merged', 'completed'), ('merged', 'completed'),
                  ('cancelled', 'completed'), ('cancelled', 'not_planned'),
                  ('ready', None)]
        self.ledger['issues'] = {}
        self.ledger['events'] = []
        for number, (state, reason) in enumerate(states, 1):
            item = deepcopy(self.issue)
            item.update(key=f'1:{number}', number=number, state=state)
            item['github_observation'] = {'at': now, 'issue': {
                'number': number, 'title': 'Change', 'body': '',
                'state': 'closed' if reason else 'open', 'state_reason': reason}}
            if state == 'merged':
                job = f'build-{number}'
                item['candidate'] = dict(pr=10+number, head='head', base='base', policy='policy',
                    requirements='requirements-hash', key=f'candidate-{number}',
                    build_attempt=job, accepted_at=12, branch=f'work-{number}')
                item['evidence'] = {'merge': {'pr': 10+number, 'sha': f'merge-{number}'}}
                self.ledger['jobs'][job] = dict(id=job, issue_key=item['key'], role='build',
                    cleanup=True, fenced=True, slot='slot', created_at=11, phase='cleaned',
                    outcome='accepted', branch=f'work-{number}', target_branch='main')
            self.ledger['issues'][item['key']] = item
        # Imported event time can go backwards. The last committed sequence,
        # not MAX(at), determines each unit's completion timestamp.
        for sequence, key, kind, at in (
            (1, '1:1', 'merged', now-100),
            (2, '1:1', 'merged', now-2*86400),
            (3, '1:2', 'merged', now-200),
            (4, '1:3', 'cancelled', now-300),
            (5, '1:4', 'cancelled', now-400),
        ):
            self.ledger['events'].append(dict(sequence=sequence, issue=key, kind=kind, at=at))
        importer = Importer(self.snapshot(), self.base/'staging')
        importer.run()
        views = PipelineViews(importer.store, clock=lambda: now)
        board = views.factory(section='board')
        self.assertEqual({row['key'] for row in board['issues']}, {'1:5'})
        self.assertEqual({key: board['summary'][key] for key in
                          ('merged_total', 'merged_day', 'merged_week', 'completed_count')},
                         dict(merged_total=2, merged_day=1, merged_week=2, completed_count=4))
        completed = views.factory(section='completed', limit=2)
        self.assertEqual([row['key'] for row in completed['issues']], ['1:2', '1:3'])
        following = views.factory(section='completed', limit=2, before=completed['next'])
        self.assertEqual([row['key'] for row in following['issues']], ['1:4', '1:1'])
        self.assertEqual(following['issues'][-1]['completed_at'], now-2*86400)
        self.assertIsNone(following['next'])


class BoardDiscoveryTests(RuntimeFixture):
    def test_only_current_unadmitted_failures_join_the_board(self):
        self.prepare()
        inventory = StoreInventory(self.database, self.settings, Mock(),
                                   config=validate({}), clock=lambda: self.now)
        repository = dict(id=10, name='example/project', enabled=True)
        remote = dict(number=1, title='Issue', body='Original', state='open',
                      updated_at='2026-10-08T10:00:00Z', labels=[], assignees=[])
        inventory.record(repository, remote)
        views = PipelineViews(self.database, clock=lambda: self.now)
        self.assertEqual(views.factory(section='board')['issues'], [])
        inventory.failure('10:1', 'requirements_stale')
        cards = views.factory(section='board')['issues']
        self.assertEqual([row['key'] for row in cards], ['10:1'])
        self.assertEqual(cards[0]['reason_code'], 'requirements_stale')
        self.assertIsNone(cards[0]['lifecycle']['unit_id'])
        inventory.record(repository, {**remote, 'body': 'Revised',
                                      'updated_at': '2026-10-08T10:01:00Z'})
        self.assertEqual(views.factory(section='board')['issues'], [])
