"""The configuration boundary is shared across setup, runtime and host updates."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.configuration_authority import ConfigurationAuthority
from forge.factory.config import load
from forge.host_update import runtime_document, save_runtime_document, Record
from forge.onboarding import Onboarding
from forge.settings import Settings, DEFAULT, digest
from forge.store.configuration import Configuration, ConfigurationConflict
from tests.test_sqlite_identity import selected_store


class ConfigurationAuthorityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_explicit_setup_scope_and_setup_root_use_same_domain(self):
        store = selected_store(self.root)
        Configuration(store).replace('host-runtime.json', {'cycle_seconds': 60}, expected_revision=0)
        setup = Onboarding(self.root, client=Mock())
        self.assertEqual(setup.runtime()['cycle_seconds'], 60)
        staged = setup.runtime()
        staged['cycle_seconds'] = 90
        setup.save_runtime(staged, active=False)
        self.assertEqual(load(self.root, scope='setup')['cycle_seconds'], 90)
        self.assertEqual(load(self.root / 'setup')['cycle_seconds'], 90)
        self.assertEqual(ConfigurationAuthority(self.root, scope='setup').runtime()['settings']['cycle_seconds'], 90)
        self.assertEqual(ConfigurationAuthority(self.root / 'setup').scope, 'setup')

    def test_selected_authority_ignores_stale_json_markers_and_defaults(self):
        store = selected_store(self.root)
        # Raw fixture writes represent diagnostic files left by migration. The
        # supported JSON writer must remain fenced after selection.
        (self.root / 'runtime.json').write_text(json.dumps({'cycle_seconds': 99}))
        (self.root / 'host-runtime.json').write_text(json.dumps({'cycle_seconds': 100}))
        (self.root / 'state/factory-v2.enabled').write_text('{}')
        configuration = Configuration(store)
        configuration.replace('host-runtime.json', {'cycle_seconds': 65}, expected_revision=0)
        setup = Onboarding(self.root, client=Mock())
        self.assertEqual(setup.runtime()['cycle_seconds'], 65)
        self.assertEqual(load(self.root)['cycle_seconds'], 30)
        self.assertEqual(Settings(self.root, scope='active', client=Mock()).scope, 'setup')
        setup.save_runtime({**setup.runtime(), 'cycle_seconds': 70}, active=False)
        self.assertEqual(load(self.root / 'setup')['cycle_seconds'], 70)
        configuration.replace('runtime.json', {'cycle_seconds': 80}, expected_revision=0)
        with store.write() as db:
            db.execute("INSERT INTO runtime_controls(name,enabled,updated_at) VALUES('factory_active',1,1)")
        self.assertEqual(Onboarding(self.root, client=Mock()).runtime()['cycle_seconds'], 80)
        self.assertEqual(Settings(self.root, scope='active', client=Mock()).scope, 'live')
        self.assertEqual((self.root / 'runtime.json').read_text().count('99'), 1)

    def test_setup_read_revision_survives_concurrent_host_overlay(self):
        store = selected_store(self.root)
        setup = Onboarding(self.root, client=Mock())
        config = setup.runtime()
        setup.save_runtime(config, active=False)
        stale = setup.runtime()
        row = Configuration(store).get('setup/runtime.json')
        save_runtime_document(self.root, 'setup/runtime.json', {**row['settings'], 'cycle_seconds': 90},
                              expected_revision=row['revision'])
        with self.assertRaises(ConfigurationConflict):
            setup.save_runtime({**stale, 'human_required': True}, active=False)
        resumed = Onboarding(self.root, client=Mock())
        resumed.save_runtime({**resumed.runtime(), 'human_required': True}, active=False)
        self.assertEqual(runtime_document(self.root, 'setup/runtime.json')['cycle_seconds'], 90)

    def test_settings_revision_is_checked_in_write_transaction(self):
        store = selected_store(self.root)
        settings = Settings(self.root, client=Mock())
        configuration = Configuration(store)
        initial = deepcopy(DEFAULT)
        configuration.replace('factory.json', initial, expected_revision=0)
        changed = {**initial, 'sync_interval_seconds': 400}
        def concurrent_view(**kwargs):
            row = configuration.get('factory.json')
            configuration.replace('factory.json', changed, expected_revision=row['revision'])
            return {'repositories': []}
        with patch.object(settings, 'view', side_effect=concurrent_view):
            with self.assertRaises(ConfigurationConflict):
                settings.save({**initial, 'access_max_age_seconds': 1000}, digest(initial))
        self.assertEqual(settings.load(), changed)
        self.assertFalse((self.root / 'factory.json').exists())

    def test_interrupted_legacy_host_update_resumes_with_latest_revision(self):
        selected_store(self.root)
        first = Record(self.root)
        first.save(source='one')
        stale = Record(self.root)
        first.save(image='two')
        with self.assertRaises(ConfigurationConflict):
            stale.save(image='old')
        resumed = Record(self.root)
        resumed.save(ui_release='three')
        self.assertEqual(runtime_document(self.root, 'host.json')['image'], 'two')

    def test_document_names_are_bounded_before_read_or_write(self):
        selected_store(self.root)
        authority = ConfigurationAuthority(self.root)
        for domain in ('../outside.json', '/tmp/outside.json', 'setup/../runtime.json', 'unknown.json'):
            with self.subTest(domain=domain):
                with self.assertRaisesRegex(ValueError, 'Unknown configuration document'):
                    authority.snapshot(domain)
                with self.assertRaisesRegex(ValueError, 'Unknown configuration document'):
                    authority.replace(domain, {}, expected_revision=0)

    def test_corrupt_selected_marker_never_uses_old_configuration(self):
        selected_store(self.root)
        (self.root / 'factory.json').write_text(json.dumps(DEFAULT))
        marker = self.root / 'state/storage'
        marker.write_text('broken selection')
        from forge.store.db import StoreError
        with self.assertRaises(StoreError):
            ConfigurationAuthority(self.root).snapshot('factory.json')


if __name__ == '__main__':
    unittest.main()
