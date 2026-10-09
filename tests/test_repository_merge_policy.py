from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from forge.factory.config import DEFAULT,validate,policy,merge_mode,legacy_merge_policies,load
from forge.repository_merge_policy import ClientMergePolicy
from forge.settings import Settings,DEFAULT as SETTINGS_DEFAULT,digest
from forge.store.configuration import Configuration
from forge.store import db as storage,upgrade
from forge_client.merge_policy import execute
from tests import test_client_connections as connections
from tests import test_sqlite_identity as identity_tests


class PolicyModelTests(unittest.TestCase):
    def test_default_human_and_digest_scoped_to_immutable_repository_identity(self):
        config=validate({'repository_merge_policies':{'1':'auto'},'repository_verification':{
            'one/repo':[{'name':'CI','app_id':7}],'other/repo':[{'name':'Other','app_id':8}]}})
        before=policy(config,1,repository_name='one/repo')
        self.assertFalse(before['human_required']); self.assertEqual(merge_mode(config,2),'human')
        self.assertNotEqual(digest(before),digest(policy(config,3,repository_name='one/repo')))
        config['repository_merge_policies']['2']='auto'; config['repository_verification']['other/repo'][0]['name']='Changed'
        self.assertEqual(before,policy(config,1,repository_name='one/repo'))
        config['repository_merge_policies']['1']='human'
        self.assertNotEqual(digest(before),digest(policy(config,1,repository_name='one/repo')))
        with self.assertRaises(ValueError):merge_mode(config,'one/repo')

    def test_invalid_policy_does_not_become_an_automatic_mode(self):
        for policies in ({'one/repo':'auto'},{'01':'auto'},{'0':'auto'},{'1':False},{'1':[]},{'1':'automatic'}):
            with self.subTest(policies=policies),self.assertRaises(ValueError):validate({'repository_merge_policies':policies})

    def test_legacy_conversion_preserves_only_existing_explicit_auto_authority(self):
        repos=[{'id':1},{'id':2}]
        config=legacy_merge_policies({'merge_enabled':True,'human_required':False,'repository_merge_policies':{'2':'human'}},repos)
        self.assertEqual(config['repository_merge_policies'],{'1':'auto','2':'human'})
        self.assertEqual(merge_mode(config,3),'human')
        for old in ({},{'merge_enabled':False,'human_required':False},{'merge_enabled':True,'human_required':True}):
            self.assertEqual(legacy_merge_policies(old,repos)['repository_merge_policies'],{'1':'human','2':'human'})

    def test_cli_write_pins_read_revision_and_explicit_request_identity(self):
        client=Mock();client.request.side_effect=[{'revision':'a'*64},{'mode':'auto'}]
        target={'id':1,'name':'one/repo'}
        self.assertEqual(execute(client,target,'auto',request_id='b'*32),{'mode':'auto'})
        self.assertEqual(client.request.call_args.kwargs,{'action':'set','mode':'auto','revision':'a'*64,'request_id':'b'*32})
        from forge_client.cli import parser,repository_commands
        args=parser().parse_args(['repo','policy','one/repo','human','--server','https://forge.example','--json'])
        self.assertEqual(args.mode,'human')
        self.assertIn('policy',repository_commands())

    def test_cli_default_connection_does_not_request_policy_capability(self):
        from forge_client.cli import parser,execute as cli_execute
        from contextlib import nullcontext
        with patch('forge_client.cli.Client') as constructor,patch('forge_client.cli.GitHub') as github,patch('forge_client.cli.connection_lock',return_value=nullcontext()):
            constructor.return_value.begin.return_value={}
            github.return_value.repository.return_value={'id':1,'name':'one/repo'}
            args=parser().parse_args(['--json','connect','https://forge.example','--repo','one/repo'])
            cli_execute(args)
            self.assertEqual(constructor.return_value.begin.call_args.args[1],['repositories:read','repositories:prepare','repositories:activate'])
            args.policy=True; cli_execute(args)
            self.assertIn('repositories:policy',constructor.return_value.begin.call_args.args[1])


class PolicyAuthorityTests(connections.Fixture):
    def setUp(self):
        identity_tests.SqliteConnectionTests.setUp(self)
        self.settings=Settings(self.root,clock=lambda:self.now)
        self.settings.save_config({**deepcopy(SETTINGS_DEFAULT),'repositories':[{**self.target,'enabled':False}]})
        self.policy=ClientMergePolicy(self.settings,self.service)
        self.credentials=self.token()
        self.base={'repository':self.target,'instance':self.credentials['connection']['instance'],'generation':self.generation}
        self.bearer=self.credentials['access_token']

    def read(self):
        return self.policy.request(self.bearer,{**self.base,'action':'read'})

    def decision(self,mode='auto',request_id='a'*32):
        return {**self.base,'action':'set','mode':mode,'revision':self.read()['revision'],'request_id':request_id}

    def test_atomic_policy_receipt_replays_and_audits_the_verified_actor(self):
        request=self.decision(); answer=self.policy.request(self.bearer,request)
        self.assertEqual(answer['mode'],'auto');self.assertEqual(answer['source'],'explicit')
        self.assertEqual(self.policy.request(self.bearer,request),answer)
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM client_operations WHERE kind='merge_policy'").fetchone()[0],1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM configuration WHERE domain='runtime.json'").fetchone()[0],1)
            row=db.execute("SELECT operator_id,connection_id FROM operator_audit WHERE json_extract(details_json,'$.kind')='configuration_change' AND json_extract(details_json,'$.domain')='runtime.json'").fetchone()
            self.assertEqual(tuple(row),('123',self.credentials['connection']['id']))
        changed={**request,'mode':'human'}
        self.assertError('operation_conflict',lambda:self.policy.request(self.bearer,changed))

    def test_changed_runtime_and_revoked_grant_cannot_write(self):
        stale=self.decision()
        self.policy.request(self.bearer,{**stale,'mode':'human','request_id':'b'*32})
        self.assertError('revision_changed',lambda:self.policy.request(self.bearer,stale))
        request=self.decision(request_id='c'*32)
        self.service.revoke(self.session,self.credentials['connection']['id'])
        self.assertError('invalid_token',lambda:self.policy.request(self.bearer,request))
        self.assertEqual(merge_mode(load(self.root),321),'human')

    def test_preexisting_connection_capabilities_do_not_authorize_policy(self):
        self.request['capabilities']=['repositories:read','repositories:prepare','repositories:activate']
        token=self.token(); self.bearer=token['access_token']
        request=self.decision()
        self.assertError('insufficient_scope',lambda:self.policy.request(self.bearer,request))
        self.assertEqual(self.read()['mode'],'human')

    def test_repeated_same_mode_does_not_rewrite_configuration(self):
        self.policy.request(self.bearer,self.decision())
        self.policy.request(self.bearer,self.decision(request_id='b'*32))
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM configuration WHERE domain='runtime.json'").fetchone()[0],1)


class PolicyMigrationTests(unittest.TestCase):
    def test_schema_25_matches_import_model_and_keeps_previous_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            migrations=storage._migrations()
            source={'merge_enabled':True,'human_required':False,'repository_merge_policies':{'2':'human'}}
            repos=[{'id':1,'name':'one/repo','enabled':True},{'id':2,'name':'two/repo','enabled':False}]
            with patch('forge.store.db._migrations',return_value=migrations[:24]):
                store=storage.Store.create(Path(directory)/'store')
                config=Configuration(store)
                config.replace('factory.json',{'repositories':repos},expected_revision=0)
                config.replace('runtime.json',source,expected_revision=0,credential_ref='private-ref')
            upgrade.apply(store);upgrade.apply(store)
            migrated=Configuration(store).get('runtime.json')
            self.assertEqual(migrated['settings'],legacy_merge_policies(source,repos))
            self.assertEqual(migrated['revision'],2);self.assertEqual(migrated['credential_ref'],'private-ref')
            with store.read_snapshot() as db:
                previous=db.execute("SELECT active,settings_json FROM configuration WHERE domain='runtime.json' AND revision=1").fetchone()
                self.assertEqual(previous[0],0);self.assertEqual(json.loads(previous[1]),source)
