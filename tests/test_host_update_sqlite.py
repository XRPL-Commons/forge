"""Host overlays must not replace configuration changed after they read it."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge import host_update
from forge.factory.config import load, save
from forge.settings import digest
from forge.store.configuration import Configuration, ConfigurationConflict
from tests.test_sqlite_identity import selected_store


OLD_IMAGE = 'sha256:' + '1' * 64
NEW_IMAGE = 'sha256:' + '2' * 64
OLD_FULL = 'sha256:' + '3' * 64
NEW_FULL = 'sha256:' + '4' * 64


class HostConfigurationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = selected_store(self.root)
        self.configuration = Configuration(self.store)

    def test_concurrent_operator_change_rejects_overlay_and_retry_preserves_it(self):
        for domain in ('host-runtime.json', 'setup/runtime.json', 'runtime.json'):
            for overlay in ('runner', 'full'):
                with self.subTest(domain=domain, overlay=overlay):
                    # Keep only this domain active, so each supported path is exercised.
                    with self.store.write() as db:
                        db.execute('UPDATE configuration SET active=0')
                    initial = {'image': OLD_IMAGE, 'human_required': False,
                               'environment_tiers': {'full': {'image': OLD_FULL, 'memory': '8g', 'cpus': 4}}}
                    self.configuration.replace(domain, initial, expected_revision=0)
                    original_save = host_update.save_runtime_document
                    def concurrent_save(root, name, value, **kwargs):
                        if name == 'runtime.json':
                            # Use the same revision-checked save as the operator API.
                            current = load(root)
                            save(root, {**current, 'human_required': True}, revision=digest(current))
                        else:
                            current = self.configuration.get(name)
                            self.configuration.replace(name, {**current['settings'], 'human_required': True},
                                                       expected_revision=current['revision'])
                        return original_save(root, name, value, **kwargs)
                    def switch():
                        if overlay == 'runner':
                            host_update.switch_runtime(self.root, NEW_IMAGE, 'new-profile')
                        else:
                            host_update.switch_full_environment(self.root, NEW_FULL)
                    with patch.object(host_update, 'save_runtime_document', side_effect=concurrent_save):
                        with self.assertRaisesRegex(ConfigurationConflict, 'changed'):
                            switch()
                    current = self.configuration.get(domain)['settings']
                    self.assertTrue(current['human_required'])
                    self.assertEqual(current['image'], OLD_IMAGE)
                    self.assertEqual(current['environment_tiers']['full']['image'], OLD_FULL)
                    switch()
                    current = self.configuration.get(domain)['settings']
                    self.assertTrue(current['human_required'])
                    self.assertEqual(current['image'], NEW_IMAGE if overlay == 'runner' else OLD_IMAGE)
                    full = current['environment_tiers']['full']
                    self.assertEqual(full['image'], NEW_FULL if overlay == 'full' else OLD_FULL)
                    self.assertEqual((full['memory'], full['cpus']), ('8g', 4))
                    self.assertFalse((self.root / domain).exists())

    def test_stale_host_record_cannot_overwrite_another_update(self):
        first, stale = host_update.Record(self.root), host_update.Record(self.root)
        first.save(source='first')
        with self.assertRaisesRegex(ConfigurationConflict, 'changed'):
            stale.save(source='stale')
        first.save(image='runner')
        resumed = host_update.Record(self.root)
        resumed.save(ui_release='ui')
        self.assertEqual(self.configuration.get('host.json')['settings'],
                         {'managed': {}, 'source': 'first', 'image': 'runner', 'ui_release': 'ui'})


if __name__ == '__main__':
    unittest.main()
