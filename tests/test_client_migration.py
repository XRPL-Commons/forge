import io
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from forge_client.cli import parser,execute
from forge_client.contracts import SetupError
from forge_client.migration import bundle


class MigrationCLITests(unittest.TestCase):
    def test_capture_requires_explicit_apply_before_any_remote_call(self):
        args=parser().parse_args(['migrate','capture','--ssh','forge','--source','/srv/source','--root','/srv/runtime','--snapshot','/private/tmp/new-snapshot'])
        with patch('forge_client.migration.call') as call:
            with self.assertRaises(SetupError):execute(args)
        call.assert_not_called()

    def test_host_plan_and_resume_are_explicit_separate_operations(self):
        for action in ('plan','resume'):
            args=parser().parse_args(['migrate',action,'--ssh','forge','--source','/srv/source','--root','/srv/runtime'])
            with patch('forge_client.migration.call',return_value={'status':action}) as call:
                self.assertEqual(execute(args),{'status':action})
            self.assertEqual(call.call_args.args[1],action)

    def test_transfer_bundle_contains_only_python_and_schema_source(self):
        with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
            names=archive.namelist()
            self.assertIn('forge/store/migration/host.py',names)
            self.assertIn('forge/store/schema/002_import_archive.sql',names)
            self.assertTrue(all(n.startswith('forge/') and n.endswith(('.py','.sql')) for n in names))
