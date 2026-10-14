"""Read-only startup qualification and prepared-interpreter handoff."""
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.launch import launch
from forge.host_update import qualify_sqlite_services, desired_units, Record
from forge.store.migration.snapshot import MigrationError
from forge.store.db import StoreError
from tests.test_sqlite_identity import selected_store


class Reexec(BaseException):
    pass


class LauncherTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.environment = patch.dict(os.environ, {}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        saved = sys.argv
        self.addCleanup(setattr, sys, 'argv', saved)

    def test_prepared_library_reexec_preserves_target_arguments_and_environment(self):
        arguments = ['--script', '/source/legacy/forge-status.py', '--root', str(self.root)]
        with patch('forge.store.db.qualified_sqlite', return_value=False), \
                patch('forge.store.migration.runtime.inspect', return_value={'library_directory': '/private/runtime/lib'}), \
                patch('forge.launch.os.execve', side_effect=Reexec) as execute, \
                patch('forge.launch.runpy.run_path') as start:
            os.environ['FORGE_TEST_CONTEXT'] = 'preserved'
            with self.assertRaises(Reexec):
                launch(arguments)
        executable, command, environment = execute.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(command, [sys.executable, '-m', 'forge.launch', *arguments])
        self.assertEqual(environment['LD_LIBRARY_PATH'], '/private/runtime/lib')
        self.assertEqual(environment['FORGE_TEST_CONTEXT'], 'preserved')
        start.assert_not_called()

    def test_library_failure_does_not_reexec_forever_or_launch_unqualified_service(self):
        os.environ['LD_LIBRARY_PATH'] = '/private/runtime/lib'
        with patch('forge.store.db.qualified_sqlite', return_value=False), \
                patch('forge.store.migration.runtime.inspect', return_value={'library_directory': '/private/runtime/lib'}), \
                patch('forge.launch.os.execve') as execute, patch('forge.launch.runpy.run_module') as start:
            with self.assertRaisesRegex(SystemExit, 'did not load'):
                launch(['forge.setup_service', '--root', str(self.root)])
        execute.assert_not_called()
        start.assert_not_called()

    def test_selected_state_cannot_start_without_qualified_interpreter_or_valid_marker(self):
        (self.root / 'state').mkdir()
        (self.root / 'state/storage').write_text('selected')
        with patch('forge.store.db.qualified_sqlite', return_value=False), \
                patch('forge.store.migration.runtime.inspect', return_value=None), patch('forge.launch.runpy.run_module') as start:
            with self.assertRaisesRegex(SystemExit, 'qualification'):
                launch(['forge.setup_service', '--root=' + str(self.root)])
        start.assert_not_called()
        with patch('forge.store.db.qualified_sqlite', return_value=True), patch('forge.launch.runpy.run_module') as start:
            with self.assertRaises(StoreError):
                launch(['forge.setup_service', '--root', str(self.root)])
        start.assert_not_called()

    def test_explicit_last_root_and_default_host_setup_root_are_qualified(self):
        with patch('forge.store.db.qualified_sqlite', return_value=True), patch('forge.store.runtime.selected') as selected, \
                patch('forge.launch.runpy.run_module') as start, patch('forge.launch.Path.home', return_value=self.root):
            launch(['forge.host_setup', 'status'])
            selected.assert_called_with(str(self.root / '.local/share/forge'))
            os.environ['FORGE_ROOT'] = '/environment'
            launch(['forge.setup_service', '--root', '/first', '--root=/last'])
            selected.assert_called_with('/last')
            self.assertEqual(start.call_args.args[0], 'forge.setup_service')
            self.assertEqual(sys.argv, ['forge.setup_service', '--root', '/first', '--root=/last'])

    def test_qualified_start_checks_selected_store_before_script(self):
        events = []
        with patch('forge.store.db.qualified_sqlite', return_value=True), \
                patch('forge.store.runtime.selected', side_effect=lambda root: events.append('selected')), \
                patch('forge.launch.runpy.run_path', side_effect=lambda *a, **k: events.append('script')):
            launch(['--script', '/source/status.py', '--root', str(self.root)])
        self.assertEqual(events, ['selected', 'script'])
        with self.assertRaises(SystemExit):
            launch(['unrelated.module'])



class ServiceQualificationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.root, self.source = self.directory / 'runtime', self.directory / 'source'
        self.root.mkdir(mode=0o700)
        (self.source / 'bin').mkdir(parents=True)
        (self.source / 'legacy').mkdir()
        (self.source / 'bin/forge').write_text('python3 -m forge.launch forge.setup_service "$@"\n')
        (self.source / 'legacy/forge-status.py').write_text('# status service\n')
        self.receipt = {'qualified': True, 'source': 'source-digest'}
        self.units = {}
        for name in ('forge-status.service', 'forge-supervisor.service'):
            arguments = (['/usr/bin/python3', '-m', 'forge.launch', '--script', str(self.source / 'legacy/forge-status.py')]
                if name.startswith('forge-status') else [str(self.source / 'bin/forge'), 'service', '--root', str(self.root)])
            self.units[name] = ('ExecStart={ path=' + shlex.quote(arguments[0]) + ' ; argv[]=' + shlex.join(arguments) + ' ; ignore_errors=no ; }\n'
                + 'Environment=' + shlex.join(['PYTHONPATH=' + str(self.source), 'FORGE_ROOT=' + str(self.root)]) + '\n')
        self.calls = []

    def host_run(self, args, **kwargs):
        self.calls.append((args, kwargs))
        if args[0] == '/usr/bin/python3':
            return Mock(returncode=0, stdout=json.dumps(self.receipt))
        return Mock(returncode=0, stdout=self.units[args[3]])

    def qualify(self, prepared=None):
        with patch('forge.store.migration.runtime.inspect', return_value=prepared), \
                patch('forge.store.migration.importer.importer_version', return_value='source-digest'):
            return qualify_sqlite_services(self.root, self.source, run=self.host_run)

    def test_exact_managed_services_and_pinned_interpreter_are_qualified(self):
        result = self.qualify({'library_directory': '/private/runtime/lib'})
        self.assertTrue(result['qualified'])
        self.assertEqual(result['library_directory'], '/private/runtime/lib')
        self.assertEqual(self.calls[0][0][0], '/usr/bin/python3')
        self.assertEqual(self.calls[0][1]['env']['LD_LIBRARY_PATH'], '/private/runtime/lib')
        self.assertTrue(all(args[0] in ('/usr/bin/python3', 'systemctl') for args, _ in self.calls))

    def test_wrong_executable_target_root_or_source_cannot_hide_in_environment(self):
        original = self.units['forge-status.service']
        wrong = [original.replace('argv[]=/usr/bin/python3', 'argv[]=/wrong/python'),
                 original.replace('path=/usr/bin/python3', 'path=/wrong/python'),
                 original.replace('/legacy/forge-status.py ;', '/legacy/other.py ;'),
                 original.replace('FORGE_ROOT=' + str(self.root), 'FORGE_ROOT=/other'),
                 original.replace('PYTHONPATH=' + str(self.source), 'PYTHONPATH=/other')]
        for output in wrong:
            self.units['forge-status.service'] = output + 'Comment=forge.launch --script ' + str(self.source) + ' ' + str(self.root)
            with self.subTest(output=output), self.assertRaises(MigrationError) as caught:
                self.qualify()
            self.assertEqual(caught.exception.code, 'service_launcher_unqualified')

    def test_unqualified_or_other_source_receipt_is_rejected(self):
        for receipt in ({'qualified': False, 'source': 'source-digest'}, {'qualified': True, 'source': 'old'},
                        {'qualified': 'yes', 'source': 'source-digest'}, []):
            self.receipt = receipt
            with self.subTest(receipt=receipt), self.assertRaises(MigrationError):
                self.qualify()

    def test_legacy_launcher_or_symlink_status_is_rejected(self):
        (self.source / 'bin/forge').write_text('python3 -m forge.setup_service\n')
        with self.assertRaises(MigrationError) as caught:
            self.qualify()
        self.assertEqual(caught.exception.code, 'legacy_launcher_detected')
        (self.source / 'bin/forge').write_text('python3 -m forge.launch forge.setup_service\n')
        (self.source / 'legacy/forge-status.py').unlink()
        (self.source / 'legacy/forge-status.py').symlink_to(self.source / 'bin/forge')
        with self.assertRaises(MigrationError):
            self.qualify()

    def test_selected_host_without_operator_config_can_render_units_and_persist_record(self):
        database = selected_store(self.root)
        units = desired_units(self.root, '127.0.0.1')
        self.assertIn('forge.launch --script', units['forge-status'])
        self.assertNotIn('NITRO_FORGE_PUBLIC_ORIGIN', units['forge-ui'])
        Record(self.root).save(bind='127.0.0.1', source_revision='current')
        self.assertEqual(Record(self.root).data['source_revision'], 'current')
        self.assertFalse((self.root / 'host.json').exists())
        with database.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT 1 FROM configuration WHERE domain='host.json' AND active=1").fetchone())

if __name__ == '__main__':
    unittest.main()
