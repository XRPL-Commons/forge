from copy import deepcopy
from unittest.mock import patch

from forge.repository_steward_policy import StewardPolicy
from forge.settings import Settings, DEFAULT
from forge.store.models import TransitionError
from tests import test_client_connections as connections
from tests import test_settings_http as http
import unittest
import json


class StewardPolicyTests(connections.Fixture):
    def setUp(self):
        super().setUp()
        self.settings=Settings(self.root,clock=lambda:self.now)
        self.settings.save_config({**deepcopy(DEFAULT),'repositories':[{**self.target,'enabled':False}]})
        self.policy=StewardPolicy(self.settings,actor_id='github:123')

    def test_policy_revision_and_history_are_repository_scoped(self):
        before=self.policy.view(self.target['id'])
        self.assertEqual((before['policy'],before['revision']),('reversible',0))
        changed=self.policy.set(repo_id=self.target['id'],revision=0,policy='manual')
        self.assertEqual((changed['policy'],changed['revision']),('manual',1))
        with self.assertRaises(TransitionError):
            self.policy.set(repo_id=self.target['id'],revision=0,policy='reversible')
        self.assertEqual(self.policy.set(repo_id=self.target['id'],revision=1,policy='manual'),changed)
        self.policy.set(repo_id=self.target['id'],revision=1,policy='reversible')
        with self.database.read_snapshot() as db:
            rows=db.execute("SELECT revision,active,settings_json FROM configuration WHERE domain='steward' ORDER BY revision").fetchall()
        self.assertEqual([(r[0],r[1]) for r in rows],[(1,0),(2,1)])
        self.assertEqual(json.loads(rows[0][2]),{'autonomy':'manual'})

    def test_unknown_repository_invalid_mode_and_unverified_actor_cannot_write(self):
        for value in ({'repo_id':999,'revision':0,'policy':'manual'},
                      {'repo_id':self.target['id'],'revision':0,'policy':'all'},
                      {'repo_id':True,'revision':0,'policy':'manual'}):
            with self.assertRaises(TransitionError):self.policy.set(**value)
        self.policy.actor_id=None
        with self.assertRaises(TransitionError):self.policy.set(repo_id=self.target['id'],revision=0,policy='manual')
        self.assertEqual(self.policy.view(self.target['id'])['revision'],0)


class StewardPolicyHTTPTests(unittest.TestCase):
    def setUp(self):
        self.f=http.SettingsHTTPTests();self.f.setUp();self.addCleanup(self.f.doCleanups)

    def test_operator_route_checks_fields_and_forwards_actor(self):
        with patch('forge.repository_steward_policy.StewardPolicy') as service:
            service.return_value.view.return_value={'policy':'manual','revision':1}
            handler=self.f.handler('/api/repositories/steward?repo_id=1');handler.do_GET()
            service.return_value.view.assert_called_once_with(1)
            service.return_value.set.return_value={'policy':'manual','revision':1}
            body={'repo_id':1,'revision':0,'policy':'manual'}
            handler=self.f.handler('/api/repositories/steward',body);handler.do_POST()
            service.return_value.set.assert_called_once_with(**body)
            self.assertTrue(service.call_args.kwargs['actor_id'].startswith('github:'))
            handler=self.f.handler('/api/repositories/steward',{**body,'force':True});handler.do_POST()
            self.assertEqual(handler._send.call_args.kwargs['status'],409)

    def test_cross_origin_policy_write_is_refused(self):
        with patch('forge.repository_steward_policy.StewardPolicy') as service:
            handler=self.f.handler('/api/repositories/steward',{},Origin='http://attacker.example');handler.do_POST()
            handler.send_error.assert_called_once_with(403);service.assert_not_called()
