import json
import unittest
import uuid
from unittest.mock import patch

from forge.factory.steward_service import StewardService
from forge.store.models import IdempotencyConflict, TransitionError, VersionConflict
from forge.store.steward_views import markers_in
from tests import test_steward_core as core
from tests import test_settings_http as http


class StewardServiceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = core.StewardCoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = StewardService(store=self.fixture.store, actor_id='github:1609016', clock=lambda: 100)

    def choice(self, **changes):
        identifier, _, _ = self.fixture.accepted(judgement='judgement')
        decision = self.service.view('i')['decision']
        return dict(key='i', decision_id=identifier, choice_id='choice-1', digest=decision['digest'],
                    request_id=str(uuid.uuid4()), **changes)

    def test_selection_is_attributed_replayable_and_distinct_from_application(self):
        request = self.choice()
        result = self.service.choose(**request)
        self.assertEqual(result['repositories'], [{'id': 1, 'name': 'owner/repo'}])
        decision = result['decision']
        self.assertEqual(decision['status'], 'selected')
        self.assertEqual(decision['application']['status'], 'pending')
        self.assertEqual(decision['application']['request_id'], request['request_id'])
        self.assertEqual(self.service.choose(**request), result)
        with self.fixture.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT actor_id FROM decisions WHERE request_id=?',
                (request['request_id'],)).fetchone()[0], 'github:1609016')
            self.assertEqual(db.execute('SELECT count(*) FROM steward_applications').fetchone()[0], 0)
        self.fixture.life.apply(self.fixture.command(), decision_id=request['decision_id'], apply_move=lambda *args: {})
        self.assertEqual(self.service.view('i')['decision']['application']['status'], 'applied')

    def test_unrelated_issue_and_changed_choice_are_refused(self):
        request = self.choice()
        self.fixture.life.admit_issue(request_id='other', actor_id='operator', unit_id='other', issue_id='other',
            repository_id=1, repository_name='owner/repo', issue_number=2, title='Other', body='Body', requirements='r', observed_at=100)
        for change in ({'key': 'other'}, {'digest': 'changed'}, {'choice_id': 'other'}):
            with self.subTest(change=change), self.assertRaises(TransitionError):
                self.service.choose(**{**request, **change, 'request_id': str(uuid.uuid4())})
        self.assertEqual(self.service.view('i')['decision']['status'], 'proposed')

    def test_stale_authority_is_visible_and_selection_cannot_clear_hold(self):
        request = self.choice()
        self.fixture.open_gate('operator_pause', {'reason': 'Stop'})
        self.assertEqual(self.service.view('i')['decision']['status'], 'stale')
        with self.assertRaises(VersionConflict):
            self.service.choose(**request)
        with self.fixture.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM steward_selections').fetchone()[0], 0)

    def test_request_identity_cannot_change_actor_or_choice(self):
        request = self.choice()
        self.service.choose(**request)
        for change in ({'choice_id': 'other'}, {'digest': 'other'}):
            with self.assertRaises(IdempotencyConflict):
                self.service.choose(**{**request, **change})
        self.service.actor_id = 'github:other'
        with self.assertRaises(IdempotencyConflict):
            self.service.choose(**request)
        self.service.actor_id = None
        with self.assertRaises(TransitionError):
            self.service.choose(**{**request, 'request_id': str(uuid.uuid4())})

    def test_card_markers_do_not_load_artifacts(self):
        request = self.choice()
        with self.fixture.store.read_snapshot() as db, patch('forge.store.steward.read_in', side_effect=AssertionError('artifact read')):
            self.assertEqual(markers_in(db, ['u'])['u'], {'decision_id': request['decision_id'],
                'digest': request['digest'], 'status': 'proposed'})


class StewardHTTPTests(unittest.TestCase):
    def setUp(self):
        self.fixture = http.SettingsHTTPTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_read_and_selection_use_operator_identity_and_fixed_fields(self):
        with patch('forge.factory.steward_service.StewardService') as service:
            service.return_value.view.return_value = {'decision': None, 'repositories': []}
            handler = self.fixture.handler('/api/issues/steward?key=1:2'); handler.do_GET()
            service.return_value.view.assert_called_once_with('1:2')
            self.assertTrue(json.loads(handler._send.call_args.args[0])['ok'])
            service.return_value.choose.return_value = {'decision': None, 'repositories': []}
            body = dict(key='1:2', decision_id='d', choice_id='c', digest='a'*64, request_id=str(uuid.uuid4()))
            handler = self.fixture.handler('/api/issues/steward', body); handler.do_POST()
            service.return_value.choose.assert_called_once_with(**body)
            self.assertTrue(service.call_args.kwargs['actor_id'].startswith('github:'))
            # The HTTP audit claim survives a lost reply; the same immutable
            # SQL command must remain replayable under the same identity.
            handler = self.fixture.handler('/api/issues/steward', body); handler.do_POST()
            self.assertEqual(service.return_value.choose.call_count, 2)
            self.assertTrue(json.loads(handler._send.call_args.args[0])['ok'])
            handler = self.fixture.handler('/api/issues/steward', {**body, 'parameters': {'force': True}}); handler.do_POST()
            self.assertFalse(json.loads(handler._send.call_args.args[0])['ok'])

    def test_cross_origin_refused_and_stale_selection_returns_conflict(self):
        with patch('forge.factory.steward_service.StewardService') as service:
            handler = self.fixture.handler('/api/issues/steward', {}, Origin='http://attacker.example'); handler.do_POST()
            handler.send_error.assert_called_once_with(403)
            service.assert_not_called()
            service.return_value.choose.side_effect = VersionConflict('Inputs changed')
            handler = self.fixture.handler('/api/issues/steward', dict(key='1:2', decision_id='d', choice_id='c',
                digest='a'*64, request_id=str(uuid.uuid4()))); handler.do_POST()
            self.assertEqual(handler._send.call_args.kwargs['status'], 409)
