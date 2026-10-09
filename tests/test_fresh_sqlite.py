from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.store.db import StoreError
from forge.store.fresh import initialize
from forge.store.runtime import selected


class FreshSQLiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_new_store_is_selected_before_setup_and_repeated_calls_preserve_identity(self):
        first = initialize(self.root)
        self.assertIs(first, selected(self.root))
        from forge.onboarding import Onboarding
        view = Onboarding(self.root).view()
        self.assertEqual(view['phase'], 'setup')
        again = initialize(self.root)
        self.assertEqual(first.instance_id, again.instance_id)
        self.assertEqual(Onboarding(self.root).view()['generation'], view['generation'])
        self.assertFalse((self.root/'state/onboarding/setup.json').exists())

    def test_existing_legacy_data_is_never_replaced(self):
        path = self.root/'factory.json'
        path.write_text('{"existing":true}')
        with self.assertRaises(StoreError) as error:
            initialize(self.root)
        self.assertEqual(error.exception.code, 'existing_runtime')
        self.assertEqual(path.read_text(), '{"existing":true}')
        self.assertFalse((self.root/'sqlite').exists())

    def test_interruption_after_database_publication_resumes_same_instance(self):
        from forge.store import fresh
        original = fresh._new_file
        def fail_marker(path, content):
            if path.name == 'storage.pending':
                raise OSError('connection lost')
            return original(path, content)
        with patch('forge.store.fresh._new_file', side_effect=fail_marker):
            with self.assertRaises(OSError):
                initialize(self.root)
        from forge.store.db import Store
        identity = Store.open(self.root/'sqlite').instance_id
        self.assertFalse((self.root/'state/storage').exists())
        self.assertEqual(initialize(self.root).instance_id, identity)

    def test_interruption_at_marker_publication_keeps_one_resumable_private_inode(self):
        from forge.store import fresh
        original = fresh.os.rename
        def interrupted(source, target):
            if source.name == 'storage.pending':
                raise OSError('connection lost')
            return original(source, target)
        with patch('forge.store.fresh.os.rename', side_effect=interrupted):
            with self.assertRaises(OSError):
                initialize(self.root)
        pending = self.root/'state/storage.pending'
        inode = pending.stat().st_ino
        from forge.store.db import Store
        identity = Store.open(self.root/'sqlite').instance_id
        self.assertEqual(initialize(self.root).instance_id, identity)
        self.assertEqual((self.root/'state/storage').stat().st_ino, inode)
        self.assertEqual((self.root/'state/storage').stat().st_nlink, 1)
        self.assertFalse(pending.exists())
