"""Factory display names are explicit configuration, independent of hostnames."""
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.factory_identity import initialize, display_name
from forge.store.fresh import initialize as initialize_store
from forge.store.configuration import Configuration


class FactoryIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = initialize_store(self.root)

    def test_missing_name_stays_missing_without_using_hostname(self):
        self.assertIsNone(display_name(self.root))
        self.assertIsNone(Configuration(self.store).get('factory_identity'))

    def test_initialize_is_idempotent_and_cannot_rename_or_change_unrelated_settings(self):
        configuration = Configuration(self.store)
        configuration.replace('unrelated', {'keep': 'value'}, expected_revision=0)
        initialize(self.root, 'Anvil')
        initialize(self.root, 'Anvil')
        self.assertEqual(display_name(self.root), 'Anvil')
        self.assertEqual(configuration.get('factory_identity')['revision'], 1)
        with self.assertRaisesRegex(ValueError, 'cannot rename'):
            initialize(self.root, 'Another factory')
        self.assertEqual(display_name(self.root), 'Anvil')
        self.assertEqual(configuration.get('unrelated')['settings'], {'keep': 'value'})

    def test_invalid_names_are_rejected(self):
        for name in ('', 'x' * 65, '\nAnvil', '<b>Anvil</b>', None):
            with self.subTest(name=name), self.assertRaises(ValueError):
                initialize(self.root, name)
        self.assertIsNone(display_name(self.root))

    def test_host_initialize_publishes_approved_name(self):
        from forge.host_setup import initialize as initialize_host
        with patch('forge.store.db.qualified_sqlite', return_value=True), redirect_stdout(StringIO()):
            initialize_host(self.root, 'Anvil')
        self.assertEqual(display_name(self.root), 'Anvil')

    def test_sqlite_runtime_reexec_preserves_name(self):
        from forge.host_setup import initialize as initialize_host
        with patch('forge.store.db.qualified_sqlite', return_value=False), \
             patch('forge.store.migration.runtime.prepare', return_value={'library_directory': '/test/sqlite'}), \
             patch('forge.host_setup.os.execve', side_effect=RuntimeError('exec')) as execute:
            with self.assertRaisesRegex(RuntimeError, 'exec'):
                initialize_host(self.root, 'Anvil')
        self.assertEqual(execute.call_args.args[1][-2:], ['--name', 'Anvil'])

    def test_fresh_installer_passes_its_approved_name_to_host_setup(self):
        from forge_client import _install_host as remote
        from unittest.mock import Mock
        service = Mock(side_effect=lambda argv, **kwargs: 'a' * 40 if 'rev-parse' in argv else
                       'active' if 'is-active' in argv else '')
        with patch.object(remote, 'prepare_service_cache'), patch.object(remote, 'service', service):
            remote.install_step('host', {'intent': {'name': 'Anvil', 'revision': 'a' * 40}})
        calls = [call.args[0] for call in service.call_args_list]
        self.assertIn([str(remote.SOURCE / 'bin/forge'), 'setup', 'initialize', '--root',
                       str(remote.ROOT), '--name', 'Anvil'], calls)
