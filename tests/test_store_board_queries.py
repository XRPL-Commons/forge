"""Board selectors retain lifecycle and completion-event semantics."""
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.store_inventory import StoreInventory
from forge.factory.store_views import PipelineViews
from forge.store.configuration import Configuration
from forge.store.db import Store
from tests.historical_store_fixtures import member
from tests.test_sqlite_runtime_support import RuntimeFixture


class BoardCompletionTests(TestCase):

    def test_completion_windows_use_last_event_sequence_and_count_issue_outcomes(self):
        now = 1_000_000
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        store = Store.create(Path(directory.name))
        Configuration(store).replace('factory.json', {'repositories': [{'id': 1, 'enabled': True}]}, expected_revision=0)
        states = [('merged', 'completed'), ('merged', 'completed'),
                  ('github_completed', 'completed'), ('github_not_planned', 'not_planned'), (None, None)]
        with store.write() as db:
            for number, (end, reason) in enumerate(states, 1):
                unit, member_id = f'u{number}', f'member-{number}'
                member(db, unit=unit, issue=f'1:{number}', number=number, identifier=member_id)
                if reason:
                    db.execute("UPDATE issues SET github_state='closed',state_reason=? WHERE id=?", (reason, f'1:{number}'))
                if end == 'merged':
                    db.execute("INSERT INTO candidates VALUES(?,?,?,?,'head','base','policy','requirements',1,'inputs',12)",
                               (f'candidate-{number}', unit, 10+number, f'work-{number}'))
                    db.execute('UPDATE units SET current_candidate_id=? WHERE id=?', (f'candidate-{number}', unit))
                    db.execute("UPDATE unit_issues SET state='done',completion_issue_version=1,left_at=100,exit_reason='merged' WHERE id=?", (member_id,))
                    db.execute("UPDATE units SET state='reviewing' WHERE id=?", (unit,))
                    db.execute("UPDATE units SET state='merged',merge_sha=? WHERE id=?", (f'sha-{number}', unit))
                elif end:
                    db.execute("UPDATE unit_issues SET state='removed',left_at=100,exit_reason='closed' WHERE id=?", (member_id,))
                    db.execute("UPDATE units SET state='closed',close_code=? WHERE id=?", (end, unit))
            # Event time can go backwards. The last committed sequence, not
            # MAX(at), determines each unit's completion timestamp.
            for sequence, unit, kind, at in (
                (1, 'u1', 'merged', now-100),
                (2, 'u1', 'merged', now-2*86400),
                (3, 'u2', 'merged', now-200),
                (4, 'u3', 'closed', now-300),
                (5, 'u4', 'closed', now-400),
            ):
                db.execute("INSERT INTO events(sequence,transaction_id,unit_id,kind,at,payload_json) VALUES(?,?,?,?,?,'{}')",
                           (sequence, f'event-{sequence}', unit, kind, at))
        views = PipelineViews(store, clock=lambda: now)
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
    def test_heartbeat_keeps_individual_error_codes_for_operator_messages(self):
        self.prepare()
        with self.database.write() as db:
            db.execute('INSERT INTO store_metadata VALUES(?,?)', ('supervisor:heartbeat', json.dumps({
                'finished_at': self.now,
                'errors': {'planning': {'count': 2, 'codes': ['triage_result_missing', 'triage_result_invalid']}},
            })))
        observation = PipelineViews(self.database, clock=lambda: self.now).factory(section='board')['observation']
        self.assertEqual(observation, {'at': self.now, 'errors': [
            {'phase': 'planning', 'reason': 'triage_result_missing'},
            {'phase': 'planning', 'reason': 'triage_result_invalid'},
        ]})

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
