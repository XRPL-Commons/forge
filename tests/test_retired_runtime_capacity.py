from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.config import validate
from forge.store import db as storage, upgrade
from forge.store.configuration import Configuration


class RetiredRuntimeCapacityTests(unittest.TestCase):
    def setUp(self):
        # Exercise this migration's exact target; later migrations may add roles.
        catalog = patch('forge.store.db._migrations', return_value=storage._migrations()[:42])
        catalog.start()
        self.addCleanup(catalog.stop)

    def test_existing_setup_view_recovers_after_explicit_upgrade(self):
        from forge.onboarding import Onboarding
        from tests.sqlite_fixture import selected_store
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with patch('forge.store.db._migrations', return_value=storage._migrations()[:41]):
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
            with patch('forge.store.db._migrations', return_value=storage._migrations()[:41]):
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
