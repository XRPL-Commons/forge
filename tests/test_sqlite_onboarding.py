"""Selected setup state preserves CAS, probe ownership and one-use callbacks."""
import unittest
from unittest.mock import Mock, patch
from copy import deepcopy

from tests import test_onboarding as legacy
from tests.test_sqlite_identity import selected_store
from forge.onboarding import Onboarding
from forge.setup_service import Service
from forge.settings import Settings, digest
from forge.store.configuration import Configuration
from forge.sqlite_onboarding import SqliteOnboarding
from forge.state import StateError


class SqliteSetupTests(unittest.TestCase):
    def setUp(self):
        legacy.SetupTests.setUp(self)
        (self.root / 'state').rename(self.root / 'legacy-fixture')
        self.database = selected_store(self.root)
        self.setup = Onboarding(self.root, client=self.api)
        self.setup.read()

    begin = legacy.SetupTests.begin
    service = legacy.SetupTests.service
    test_manifest_role_permissions_and_no_admin_token = legacy.SetupTests.test_manifest_role_permissions_and_no_admin_token
    test_callback_requires_cookie_nonce_and_is_single_use = legacy.SetupTests.test_callback_requires_cookie_nonce_and_is_single_use
    test_expired_or_reset_callback_is_rejected = legacy.SetupTests.test_expired_or_reset_callback_is_rejected
    test_lost_manifest_exchange_is_not_replayed = legacy.SetupTests.test_lost_manifest_exchange_is_not_replayed
    test_installation_is_verified_independently = legacy.SetupTests.test_installation_is_verified_independently

    def connected(self):
        from forge.onboarding import ROLES
        settings = self.setup.settings()
        config = settings.load()
        config['connections'] = {r: {'app_id': i, 'installation_id': 100+i, 'private_key_env': 'FORGE_SETUP_'+r.upper()+'_KEY'} for i,r in enumerate(ROLES, 1)}
        settings.save(config, digest(settings.load()))
        settings.refresh()
        with self.setup.store.transaction() as data:
            data['apps'] = {r: {'connection': config['connections'][r], 'install_url': 'https://github.com/apps/fixture/installations/new'} for r in ROLES}
            data['owner'], data['account_type'] = 'example', 'organization'
        self.setup.select(10)
        self.setup.connect('codex', api_key='test-provider-value-no-real-credential')

    def test_selected_configuration_and_setup_are_normalized(self):
        self.connected()
        self.assertIsInstance(self.setup, SqliteOnboarding)
        self.assertEqual(Onboarding(self.root).read()['selected']['id'], 10)
        self.assertTrue(Configuration(self.database).get('setup/runtime.json'))
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT settings_json FROM setup_instances WHERE scope='live'").fetchone()
        self.assertNotIn('test-provider-value', row[0])
        self.assertFalse((self.root / 'setup/runtime.json').exists())

    def test_callback_network_runs_without_sql_transaction(self):
        state = self.begin()
        request = self.api.request.side_effect
        def network(*args, **kwargs):
            with self.database.write() as db:
                db.execute("INSERT OR REPLACE INTO store_metadata VALUES('parallel','ok')")
            return request(*args, **kwargs)
        self.api.request.side_effect = network
        self.setup.callback(state, 'code', self.browser)

    def test_bounded_setup_cas_rejects_lost_update(self):
        with self.assertRaises(StateError):
            with self.setup.store.transaction() as data:
                data['owner'] = 'stale'
                with self.database.write() as db:
                    db.execute("UPDATE setup_instances SET revision=revision+1 WHERE scope='live'")
        self.assertNotEqual(self.setup.read()['owner'], 'stale')

    def test_probe_launch_and_cleanup_are_separate_from_setup_json(self):
        service, engine, resets = self.service()
        job = service.new_probe(self.setup.runtime(), 'provider')
        with self.setup.store.transaction() as data:
            data['probe_jobs'] = [job]
        recovered = self.setup.read()['probe_jobs'][0]
        self.assertEqual(recovered['id'], job['id'])
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 1)
            self.assertNotIn('runner_config', db.execute('SELECT settings_json FROM setup_instances').fetchone()[0])
        recovered.update(fenced=True, cleanup=True)
        with self.setup.store.transaction() as data:
            data['probe_jobs'] = [recovered]
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 0)
        with self.setup.store.transaction() as data:
            data['probe_jobs'] = []
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_executions').fetchone()[0], 1)

    def test_reset_fence_is_sql_and_generation_fences_agents(self):
        previous = self.setup.read()['generation']
        self.setup.queue('reset', generation=previous)
        self.assertTrue(Settings(self.root).paused())
        self.assertFalse((self.root / 'state/factory-paused.json').exists())
        self.assertEqual(self.setup.read()['phase'], 'resetting')

    def test_readiness_probe_cycle_start_and_reset_keep_database_and_evidence(self):
        from forge.operator_auth import OperatorAuth
        auth = OperatorAuth(self.root)
        auth.configure('https://forge.example', 'identity-client', 's' * 40, [123])
        self.connected()
        config = self.setup.runtime()
        config['image'] = 'sha256:' + 'a' * 64
        self.setup.save_runtime(config, active=False)
        service, engine, resets = self.service()
        self.setup.queue('check', self.setup.view()['revision'])
        for _ in range(30):
            state = self.setup.read()
            if not state['operation']:
                break
            service.tick()
        self.assertIsNone(self.setup.read()['operation'])
        self.assertFalse(engine)
        self.assertTrue(self.setup.view()['can_start'])
        self.setup.queue('start', self.setup.view()['revision'])
        service.tick()
        self.assertEqual(self.setup.read()['phase'], 'active')
        self.assertTrue(Settings(self.root).load()['repositories'][0]['enabled'])
        from forge.client_connections import active_generation
        old_generation = active_generation(self.root)
        with self.database.write() as db:
            db.execute("INSERT INTO environment_scans(id,repository_id,source_revision,status,created_at) VALUES('pre-reset',10,'old','running',1)")
        from forge.mutation_audit import MutationAudit
        audit = MutationAudit(self.database, '/api/runtime')
        audit.result({'ok': True, 'private': 'never-store-this-response'})
        audit.finish()
        with self.database.read_snapshot() as db:
            saved_audit = dict(db.execute('SELECT * FROM operator_audit WHERE request_id=?', (audit.request_id,)).fetchone())
        self.setup.queue('reset', generation=old_generation)
        service.tick()
        self.assertEqual(self.setup.read()['phase'], 'setup')
        self.assertNotEqual(self.setup.read()['generation'], old_generation)
        self.assertFalse(Settings(self.root).paused())
        self.assertTrue(self.database.path.exists())
        self.assertTrue((self.root / 'state/storage').exists())
        self.assertEqual(OperatorAuth(self.root).config()['client_secret'], 's' * 40)
        from forge.settings import read_json
        self.assertEqual(read_json(self.setup.credentials.root / 'index.json'), {})
        with self.database.read_snapshot() as db:
            retained_audit = dict(db.execute('SELECT * FROM operator_audit WHERE request_id=?', (audit.request_id,)).fetchone())
            self.assertEqual(retained_audit, saved_audit)
            self.assertNotIn('never-store-this-response', str(retained_audit))
            self.assertNotIn('s' * 40, str(list(db.execute('SELECT * FROM operator_audit'))))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_executions').fetchone()[0], 3)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM auxiliary_artifacts').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM auxiliary_leases').fetchone()[0], 0)
            self.assertGreater(db.execute('SELECT COUNT(*) FROM configuration WHERE active=0').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT status FROM environment_scans WHERE id='pre-reset'").fetchone()[0], 'failed')
        self.connected()
        with self.database.read_snapshot() as db:
            row = db.execute("SELECT revision FROM configuration WHERE domain='setup/factory.json' AND active=1").fetchone()
            self.assertGreater(row[0], 1)

    def test_reset_waits_for_uncertain_effect_preserves_history_and_pauses_units(self):
        from forge.store.transitions import Lifecycle
        Lifecycle(self.database).admit_issue(request_id='admit', actor_id='operator', unit_id='old-unit',
            issue_id='old-issue', repository_id=10, repository_name='example/project', issue_number=1,
            title='Old work', body='Do it', requirements={'acceptance': ['done']}, observed_at=1)
        with self.database.write() as db:
            db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,payload_json,created_at,updated_at) "
                       "VALUES('uncertain','uncertain','old-unit','test','uncertain','{}',1,1)")
        service, _, _ = self.service()
        self.setup.queue('reset', generation=self.setup.read()['generation'])
        with patch('forge.factory.sqlite_runtime.cleanup', return_value={'pending': 0}):
            service.reset(self.setup.read())  # first page pauses old units
            service.reset(self.setup.read())
        self.assertEqual(self.setup.read()['phase'], 'resetting')
        self.assertIn('GitHub effect', self.setup.read()['error'])
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='operator_pause' AND resolved_at IS NULL").fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM issues').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM external_effects WHERE id='uncertain'").fetchone()[0], 'uncertain')

    def test_reset_same_generation_fence_is_not_cleared_by_old_receipt(self):
        from forge.sqlite_onboarding import control_in
        with self.setup.store.transaction() as data:
            data['reset_receipt'] = {'id': 'old', 'generation': 'old-generation'}
        with self.database.write() as db:
            control_in(db, 'factory_paused', True, 1, {'generation': self.setup.read()['generation']})
        service, _, _ = self.service()
        service.tick()
        self.assertTrue(Settings(self.root).paused())

if __name__ == '__main__':
    unittest.main()
