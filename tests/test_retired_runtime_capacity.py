from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.config import validate
from forge.store import db as storage, upgrade
from forge.store.configuration import Configuration


class RetiredRuntimeCapacityTests(unittest.TestCase):
    def setUp(self):
        # Test the capacity migration, independently of later schema additions.
        catalog = storage._migrations()[:2]
        current = patch('forge.store.db._migrations', return_value=catalog)
        current.start(); self.addCleanup(current.stop)

    def test_existing_setup_view_recovers_after_explicit_upgrade(self):
        from forge.onboarding import Onboarding
        from tests.sqlite_fixture import selected_store
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch('forge.store.db._migrations', return_value=storage._migrations()[:1]):
                old = selected_store(root)
                config = Configuration(old)
                for domain in ('host-runtime.json', 'setup/runtime.json'):
                    config.replace(domain, {'max_running': 2}, expected_revision=0)
                with self.assertRaisesRegex(ValueError, 'Unknown runtime settings'):
                    Onboarding(root).view()
            upgrade.apply(storage.Store(old.root))
            # Maintenance restarts services with the new release's store handles.
            with patch('forge.store.runtime._stores', {}):
                self.assertFalse(Onboarding(root).view()['provider_connected'])

    def test_upgrade_supersedes_only_affected_active_runtime_revisions(self):
        with tempfile.TemporaryDirectory() as temporary:
            with patch('forge.store.db._migrations', return_value=storage._migrations()[:1]):
                old = storage.Store.create(Path(temporary) / 'store')
                config = Configuration(old)
                for domain in ('runtime.json', 'setup/runtime.json', 'host-runtime.json'):
                    config.replace(domain, {'max_running': 2, 'enabled': False,
                                   'memory': '8g', 'cpus': 3}, expected_revision=0,
                                   credential_ref='retained-reference')
                # A later inactive revision need not be the active revision.
                with old.write() as db:
                    db.execute("INSERT INTO configuration VALUES('setup/runtime.json','instance',7,0,'{}',NULL,1)")
                config.replace('runtime.json', {'enabled': True}, expected_revision=1)
                config.replace('unrelated', {'max_running': 10}, expected_revision=0)
                with old.read_snapshot() as db:
                    before = [tuple(row) for row in db.execute('SELECT * FROM configuration ORDER BY domain,revision')]
            target = storage.Store(old.root)
            upgrade.apply(target)
            settings = Configuration(target)
            self.assertEqual(settings.get('runtime.json')['revision'], 2)
            self.assertEqual(settings.get('unrelated')['settings'], {'max_running': 10})
            for domain, revision in (('setup/runtime.json', 8), ('host-runtime.json', 2)):
                current = settings.get(domain)
                self.assertEqual(current['revision'], revision)
                self.assertEqual(current['credential_ref'], 'retained-reference')
                self.assertEqual(current['settings'], {'enabled': False, 'memory': '8g', 'cpus': 3})
                validate(current['settings'])
            with target.read_snapshot() as db:
                after = [tuple(row) for row in db.execute('SELECT * FROM configuration ORDER BY domain,revision')]
                for row in before:
                    retained = next(item for item in after if item[:3] == row[:3])
                    self.assertEqual(retained[:3] + retained[4:], row[:3] + row[4:])
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            upgrade.apply(target)
            with target.read_snapshot() as db:
                self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM configuration ORDER BY domain,revision')], after)

    def test_fresh_store_and_strict_validation_do_not_accept_retired_setting(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = storage.Store.create(Path(temporary) / 'store')
            with store.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM configuration').fetchone()[0], 0)
            with self.assertRaisesRegex(ValueError, 'Unknown runtime settings'):
                validate({'max_running': 1})
            with self.assertRaisesRegex(ValueError, 'Unknown runtime settings'):
                validate({'unrecognized': 1})

    def test_released_v41_store_rebases_and_removes_retired_setting_in_one_upgrade(self):
        from tests.test_schema_baseline import legacy_catalog
        with tempfile.TemporaryDirectory() as temporary:
            with patch('forge.store.db._migrations', return_value=legacy_catalog()):
                old = storage.Store.create(Path(temporary) / 'store')
                Configuration(old).replace('host-runtime.json', {'max_running': 2, 'memory': '8g'},
                                           expected_revision=0, credential_ref='retained-reference')
            target = storage.Store(old.root)
            self.assertTrue(upgrade.inspect(target)['legacy_history'])
            result = upgrade.apply(target)
            self.assertEqual(result['current_schema'], 2)
            current = Configuration(target).get('host-runtime.json')
            self.assertEqual(current['settings'], {'memory': '8g'})
            self.assertEqual(current['credential_ref'], 'retained-reference')
            with target.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM configuration').fetchone()[0], 2)
                self.assertIn('max_running', db.execute('SELECT settings_json FROM configuration WHERE revision=1').fetchone()[0])
