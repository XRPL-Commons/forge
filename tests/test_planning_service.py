from contextlib import nullcontext
from concurrent.futures import ThreadPoolExecutor
import threading
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.store.db import Store
from forge.store.models import TransitionError, VersionConflict
from forge.store.repository_planning import RepositoryPlanning
from forge.store.transitions import Lifecycle
from forge.factory.planning_service import PlanningService, client_request
from forge_client.cli import parser, execute


class PlanningServiceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store.create(temporary.name)
        Lifecycle(self.store,clock=lambda:100).admit_issue(request_id='admit',actor_id='operator',unit_id='unit',issue_id='1:1',
            repository_id=1,repository_name='example/project',issue_number=1,title='Work',body='Details',requirements='digest',observed_at=90)
        self.plans=RepositoryPlanning(self.store,clock=lambda:100)
        self.parts={'store':self.store,'github':Mock(),'config':{'hold_labels':['hold'],'ready_label':'ready'}}
        self.service=PlanningService(temporary.name,actor_id='github:7',clock=lambda:100,parts=self.parts)

    def test_repository_boundary_and_named_operator_attribution(self):
        self.service.action(1,{'action':'rules','request_id':'rules','expected_revision':0,'rules':'Keep related issues together','proposals_enabled':False})
        self.assertEqual(self.service.read(1)['planning']['rules'],'Keep related issues together')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT actor_id FROM repository_planning_requests').fetchone()[0],'github:7')
        self.plans.begin(1,proposal_id='proposal',request_id='begin',actor_id='planner',base_branch='main')
        with self.assertRaises(TransitionError):self.service.read(2,proposal_id='proposal')
        with self.assertRaises(TransitionError):self.service.action(1,{'action':'rules','request_id':'unsafe','rules':'extra'})

    def test_client_reads_and_mutations_have_separate_explicit_capabilities(self):
        clients=Mock();clients.authorize.return_value={'id':'agent','operator':{'id':7}}
        clients.effect_guard.side_effect=lambda *args:nullcontext()
        value={'repository':{'id':1,'name':'example/project'},'instance':'instance','generation':1,'operation':'read','payload':{}}
        with patch('forge.factory.planning_service.PlanningService') as service:
            service.return_value.read.return_value={'planning':{}}
            client_request('root',clients,'token',value)
            self.assertEqual(clients.authorize.call_args.args[2],'repositories:read')
            client_request('root',clients,'token',{**value,'operation':'action','payload':{'action':'triage','request_id':'run'}})
            self.assertEqual(clients.authorize.call_args.args[2],'repositories:triage')
            self.assertEqual(service.call_args.kwargs['actor_id'],'client:agent')

    def test_cli_surface_emits_structured_triage_request_without_personal_token(self):
        args=parser().parse_args(['triage','example/project','--server','https://forge.example','--request-id','stable','--json'])
        with patch('forge_client.cli.Client') as client, patch('forge_client.cli.GitHub') as github, patch('forge_client.cli.connection_lock'):
            github.return_value.repository.return_value={'id':1,'name':'example/project','private':True}
            client.return_value.request.return_value={'proposal_id':'proposal','status':'queued'}
            self.assertEqual(execute(args)['status'],'queued')
            client.return_value.request.assert_called_once_with('planning',{'id':1,'name':'example/project'},operation='action',payload={'action':'triage','request_id':'stable'})
        commands=execute(parser().parse_args(['commands']))
        self.assertEqual(commands['planning_commands']['triage']['capability'],'repositories:triage')

    def test_api_accept_reads_real_issue_contract_and_commits_dependency_intent_once(self):
        self.plans.begin(1,proposal_id='proposal',request_id='begin',actor_id='planner',base_branch='main')
        self.plans.publish('proposal',expected_version=1,request_id='publish',actor_id='planner',rationale='Work in order',rows=[{
            'action':'order','members':['1:1'],'reason':'Needs the shared library','rule_quote':None,'parameters':{'prerequisites':['example/library#7']}}])
        row=self.plans.proposal('proposal')['rows'][0]
        def issue(item):
            self.assertEqual(item,{'repo_id':1,'repo':'example/project','number':1})
            return {'number':1,'state':'open','labels':[],'assignees':[]},'digest'
        self.parts['github'].issue.side_effect=issue
        value={'action':'accept','proposal_id':'proposal','expected_version':2,'request_id':'operator-accept','accept_rows':[row['id']],'reject_rows':[]}
        receipt=self.service.action(1,value)
        self.assertEqual(receipt['status'],'accepted')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM prerequisite_orders').fetchone()[0],1)
            gate=db.execute("SELECT details_json FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL").fetchone()
            self.assertIsNotNone(gate)
        self.parts['github'].issue.side_effect=AssertionError('replay must not need a new network read')
        with patch('forge.factory.planning_service.load',return_value=self.parts['config']):
            self.assertEqual(self.service.action(1,value),receipt)

    def test_api_rejects_changed_github_requirements_before_any_acceptance(self):
        self.plans.begin(1,proposal_id='proposal',request_id='begin',actor_id='planner',base_branch='main')
        self.plans.publish('proposal',expected_version=1,request_id='publish',actor_id='planner',rationale='Keep work',rows=[{
            'action':'keep','members':['1:1'],'reason':'Useful','rule_quote':None,'parameters':{}}])
        row=self.plans.proposal('proposal')['rows'][0]
        self.parts['github'].issue.return_value=({'state':'open','labels':[],'assignees':[]},'changed')
        with self.assertRaises(VersionConflict):self.service.action(1,{'action':'accept','proposal_id':'proposal','expected_version':2,
            'request_id':'operator-accept','accept_rows':[row['id']],'reject_rows':[]})
        self.assertEqual(self.plans.proposal('proposal')['rows'][0]['status'],'proposed')


from tests import test_client_connections as connections
from tests import test_sqlite_identity as identity
from forge.client_connections import ConnectionError


class PlanningClientAuthorityTests(connections.Fixture):
    def setUp(self):
        identity.SqliteConnectionTests.setUp(self)
        Lifecycle(self.database,clock=lambda:self.now).admit_issue(request_id='admit',actor_id='operator',unit_id='unit',issue_id='321:1',
            repository_id=321,repository_name='example/project',issue_number=1,title='Work',body='Details',requirements='digest',observed_at=self.now)
        self.credentials=self.token()
        self.parts={'store':self.database,'github':Mock(),'config':{'hold_labels':['hold'],'ready_label':'ready'}}
        self.planning=PlanningService(self.root,actor_id='client:'+self.credentials['connection']['id'],clock=lambda:self.now,parts=self.parts)
        self.planning.plans.begin(321,proposal_id='proposal',request_id='begin',actor_id='planner',base_branch='main')
        self.planning.plans.publish('proposal',expected_version=1,request_id='publish',actor_id='planner',rationale='Work in order',rows=[{
            'action':'order','members':['321:1'],'reason':'Needs library','rule_quote':None,'parameters':{'prerequisites':['example/library#7']}}])
        row=self.planning.plans.proposal('proposal')['rows'][0]
        self.value={'repository':self.target,'instance':self.credentials['connection']['instance'],'generation':self.generation,
            'operation':'action','payload':{'action':'accept','proposal_id':'proposal','expected_version':2,'request_id':'approve',
                                          'accept_rows':[row['id']],'reject_rows':[]}}

    def test_revocation_serializes_with_observation_and_acceptance(self):
        observing,release,revoking,revoked=threading.Event(),threading.Event(),threading.Event(),threading.Event()
        def issue(item):
            observing.set()
            if not release.wait(3):raise AssertionError('Observation was not released')
            return {'number':1,'state':'open','labels':[],'assignees':[]},'digest'
        self.parts['github'].issue.side_effect=issue
        def revoke():
            revoking.set()
            self.service.revoke(self.session,self.credentials['connection']['id'])
            revoked.set()
        with patch('forge.factory.planning_service.PlanningService',return_value=self.planning),ThreadPoolExecutor(max_workers=2) as pool:
            accepted=pool.submit(client_request,self.root,self.service,self.credentials['access_token'],self.value)
            try:
                self.assertTrue(observing.wait(3))
                revocation=pool.submit(revoke)
                self.assertTrue(revoking.wait(3))
                self.assertFalse(revoked.wait(.05),'revocation must not complete while an authorized acceptance is in progress')
            finally:release.set()
            self.assertEqual(accepted.result(3)['status'],'accepted')
            revocation.result(3)
            with self.assertRaises(ConnectionError):
                client_request(self.root,self.service,self.credentials['access_token'],self.value)
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM prerequisite_orders').fetchone()[0],1)

    def test_revoked_before_effect_guard_cannot_accept_after_initial_authorization(self):
        authorize=self.service.authorize
        def revoke_after_authorize(*args):
            grant=authorize(*args)
            self.service.revoke(self.session,self.credentials['connection']['id'])
            return grant
        with patch.object(self.service,'authorize',side_effect=revoke_after_authorize),patch('forge.factory.planning_service.PlanningService',return_value=self.planning):
            with self.assertRaises(ConnectionError):
                client_request(self.root,self.service,self.credentials['access_token'],self.value)
        self.parts['github'].issue.assert_not_called()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM prerequisite_orders').fetchone()[0],0)
