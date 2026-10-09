import unittest
import json
from forge.store.migration.importer import Importer
from forge.store.migration.identity import AUTH, CREDENTIAL
from forge.store.migration.snapshot import MigrationError
from forge.store.identity import IdentityQueries
from tests import test_store_import as fixtures


class IdentityImportTests(unittest.TestCase):
    setUp=fixtures.ImportTests.setUp
    write=fixtures.ImportTests.write
    snapshot=fixtures.ImportTests.snapshot

    def auth(self):
        scope={'repositories':[{'id':1,'name':'test/repo'}],'owners':[]}
        self.source=dict(version=1,config=dict(origin='https://forge.example',client_id='client-id',client_secret='x'*40,operators=[7]),
                         instance_id='legacy-instance',epoch='current-opaque-epoch',
                         sessions={'session':dict(operator={'id':7,'login':'operator'},csrf='csrf',expires=4102444800)},
                         flows={'flow':dict(browser='browser-hash',verifier='verifier',expires=4102444800,return_to='/')},
                         clients={},devices={},device_window={'start':1,'count':2},connection_lookups={'7':{'start':1,'count':3}})
        for identifier in ('active','revoked','old-epoch','old-generation','expired'):
            value=dict(id=identifier,name='Agent',operator={'id':7,'login':'operator'},capabilities=['repositories:read'],scope=scope,
                       created=1,expires=4102444800,idle_expires=4102444800,last_used=2,revoked=None,
                       access_hash='access-'+identifier,access_expires=4102444800,refresh_hash='refresh-'+identifier,
                       used_refresh=['used-'+identifier],instance='legacy-instance',generation='generation',
                       origin='https://forge.example',epoch='current-opaque-epoch')
            if identifier=='revoked':value['revoked']='operator_signed_out'
            if identifier=='old-epoch':value['epoch']='previous-epoch'
            if identifier=='old-generation':value['generation']='previous-generation'
            if identifier=='expired':value['idle_expires']=10
            self.source['clients'][identifier]=value
        self.source['devices']['device']=dict(instance='legacy-instance',generation='generation',epoch='current-opaque-epoch',
            origin='https://forge.example',name='Agent',user_code='ABCD-EFGH',capabilities=['repositories:read'],scope=scope,
            expires=4102444800,interval=5,next_poll=20,status='consumed',grant_id='active')
        self.write(AUTH,self.source)
        self.write('state/onboarding/setup.json',{'generation':'generation'})

    def test_import_keeps_revocations_epochs_and_refresh_replay_fences(self):
        self.auth()
        importer=Importer(self.snapshot(),self.base/'staging');report=importer.run()
        reader=IdentityQueries(importer.store)
        self.assertEqual(reader.session('session',100),self.source['sessions']['session'])
        self.assertEqual(reader.bearer('access-active',generation='generation',now=100)['id'],'active')
        for identifier in ('revoked','old-epoch','old-generation','expired'):
            self.assertIsNone(reader.bearer('access-'+identifier,generation='generation',now=100))
        self.assertIsNone(reader.bearer('',generation='generation',now=100))
        self.assertEqual(report['supporting_checks']['consumed_refresh_tokens'],5)
        with importer.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT status FROM client_pairings').fetchone()[0],'consumed')
        credential=importer.store.root/CREDENTIAL
        self.assertEqual(credential.stat().st_mode&0o777,0o600)
        self.assertNotIn('x'*40,str(report))

    def test_import_preserves_legacy_admissions_without_identity_credentials(self):
        from forge.store.migration.identity import AUDIT
        self.auth()
        event = dict(time=123, event='operator_request', operator={'id': 7, 'login': 'operator'},
                     route='/api/settings/sync')
        path = self.root / AUDIT
        path.write_text(json.dumps(event) + '\n')
        path.chmod(0o600)
        importer = Importer(self.snapshot(), self.base / 'staging')
        report = importer.run()
        importer.run()
        with importer.store.read_snapshot() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM operator_audit')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['outcome'], 'admitted')
        self.assertEqual(rows[0]['operator_id'], '7')
        self.assertEqual(report['supporting_checks']['operator_audit'], 1)
        for secret in ('x' * 40, 'csrf', 'verifier', 'access-active', 'refresh-active'):
            self.assertNotIn(secret, json.dumps(rows))

    def test_identity_replay_preserves_credential_and_detects_corruption(self):
        self.auth();source=self.snapshot()
        importer=Importer(source,self.base/'staging',batch=1);report=importer.run()
        self.assertEqual(importer.run()['supporting_checks'],report['supporting_checks'])
        (importer.store.root/CREDENTIAL).write_bytes(b'changed')
        with self.assertRaisesRegex(MigrationError,'identity_credential_parity_mismatch'):
            importer.validate()
        with self.assertRaisesRegex(MigrationError,'identity_credential_conflict'):
            Importer(source,self.base/'staging').run()

    def test_parity_detects_revoked_connection_becoming_active(self):
        self.auth();importer=Importer(self.snapshot(),self.base/'staging');importer.run()
        with importer.store.write() as db:
            db.execute("UPDATE client_connections SET revocation_reason=NULL,revoked_at=NULL WHERE id='revoked'")
        with self.assertRaisesRegex(MigrationError,'identity_connection_parity_mismatch'):
            importer.validate()

    def test_setup_probe_blocks_capture_before_destination_creation(self):
        self.write('state/onboarding/setup.json',{'probe_jobs':[{'id':'setup-probe','cleanup':False}]})
        with self.assertRaisesRegex(MigrationError,'uncleaned_jobs'):
            self.snapshot()
        self.assertFalse((self.base/'snapshot').exists())

    def test_interrupted_credential_publication_resumes_from_sealed_source(self):
        self.auth();source=self.snapshot()
        importer=Importer(source,self.base/'staging')
        from unittest.mock import patch
        from forge.store.db import _new_file
        def interrupted(path,content):
            _new_file(path,content[:5])
            raise RuntimeError('power lost during credential staging')
        with patch('forge.store.migration.identity._new_file',interrupted):
            with self.assertRaises(RuntimeError):importer.run()
        self.assertFalse((importer.store.root/CREDENTIAL).exists())
        report=Importer(source,self.base/'staging').run()
        self.assertEqual(report['supporting_checks']['operator_sessions'],1)
        self.assertEqual((importer.store.root/CREDENTIAL).read_bytes(),b'x'*40)


if __name__=='__main__':unittest.main()
