"""Explicit transport approval and delegation scope for membership decisions."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock,patch
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from forge.delivery_actions import ClientDelivery
from forge.client_connections import ConnectionError
from forge_client.delivery import execute
from forge_client.cli import parser
from tests import test_member_operator as members


class DeliveryClientTests(unittest.TestCase):
    def test_cli_sends_exact_saved_decision_without_refreshing_it(self):
        preview={'revision':'original','delivery':{'decision':{'member_id':'member','membership_generation':3,'dependent_decisions':{'other':'wait_external'}}}}
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'decision.json';source.write_text(json.dumps(preview))
            args=parser().parse_args(['delivery','drop','example/repo','issue','--server','https://forge.example','--decision',str(source),'--reason','Split work','--apply','--json'])
            args.request_id=str(uuid.uuid4())
            client=Mock();execute(client,{'id':1,'name':'example/repo'},args)
        client.request.assert_called_once_with('delivery',{'id':1,'name':'example/repo'},action='drop',key='issue',revision='original',membership=preview['delivery']['decision'],reason='Split work',request_id=args.request_id)

    def test_cli_inspect_is_read_only_and_apply_is_required(self):
        client=Mock();args=SimpleNamespace(delivery_command='inspect',key='issue')
        execute(client,{'id':1,'name':'example/repo'},args)
        client.request.assert_called_once_with('delivery',{'id':1,'name':'example/repo'},action='inspect',key='issue')
        with self.assertRaises(SystemExit):
            parser().parse_args(['delivery','dissolve','example/repo','issue','--server','https://forge.example','--decision','decision.json','--reason','Split'])


class DeliveryScopeTests(unittest.TestCase):
    setUp=members.MemberOperatorTests.setUp
    command=members.MemberOperatorTests.command
    compose=members.MemberOperatorTests.compose
    manifest=members.MemberOperatorTests.manifest
    clean=members.MemberOperatorTests.clean
    plan=members.MemberOperatorTests.plan
    setup_actions=members.MemberOperatorTests.setup_actions

    def test_cli_checks_repository_identity_and_named_capability_before_queueing(self):
        self.setup_actions()
        clients=Mock();clients.authorize.return_value={'id':'client','operator':{'id':42}}
        clients.effect_guard.side_effect=lambda *args:nullcontext()
        service=ClientDelivery('/unused',clients)
        base={'repository':{'id':1,'name':'example/repo'},'instance':'instance','generation':1,'key':'issue-0'}
        with patch('forge.delivery_actions.selected_service',return_value=self.actions):
            preview=service.request('token',{**base,'action':'inspect'})
            self.assertEqual(clients.authorize.call_args.args[2],'repositories:read')
            value={**base,'action':'drop','revision':preview['revision'],'membership':preview['delivery']['decision'],'reason':'Split','request_id':str(uuid.uuid4())}
            service.request('token',value)
            self.assertEqual(clients.authorize.call_args.args[2],'repositories:triage')
            with self.assertRaises(ConnectionError):
                service.request('token',{**base,'repository':{'id':2,'name':'example/other'},'action':'inspect'})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM issue_action_requests').fetchone()[0],1)

    def test_revoked_grant_cannot_replay_a_pending_membership_decision(self):
        self.setup_actions()
        clients=Mock();clients.authorize.side_effect=ConnectionError('revoked','Connection revoked',403)
        with patch('forge.delivery_actions.selected_service') as selected,self.assertRaises(ConnectionError):
            ClientDelivery('/unused',clients).request('token',{'repository':{'id':1,'name':'example/repo'},'instance':'instance','generation':1,'key':'issue-0','action':'drop','revision':'old','membership':{},'reason':'Split','request_id':str(uuid.uuid4())})
        selected.assert_not_called()
