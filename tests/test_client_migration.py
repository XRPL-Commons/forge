import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from forge_client.cli import parser,execute
from forge_client.contracts import SetupError
from forge_client.migration import bundle


class MigrationCLITests(unittest.TestCase):
    def test_runtime_preparation_requires_apply_and_never_invokes_capture(self):
        values=['migrate','prepare','--ssh','forge','--source','/srv/source','--root','/srv/runtime']
        with patch('forge_client.migration.call',return_value={'status':'runtime_ready'}) as call:
            with self.assertRaises(SetupError):execute(parser().parse_args(values))
            call.assert_not_called()
            self.assertEqual(execute(parser().parse_args([*values,'--apply'])),{'status':'runtime_ready'})
            self.assertEqual(call.call_args.args[1],'prepare')

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

    def test_transfer_bundle_contains_only_code_schema_and_harness_manifests(self):
        with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
            names=archive.namelist()
            self.assertIn('forge/store/migration/host.py',names)
            self.assertIn('forge/store/schema/002_import_archive.sql',names)
            self.assertIn('forge/harnesses/codex/manifest.json', names)
            self.assertTrue(all(n.startswith('forge/') and (n.endswith(('.py','.sql')) or
                Path(n).match('forge/harnesses/*/manifest.json')) for n in names))

    def test_isolated_bundle_validates_runtime_and_binds_manifest_identity(self):
        from forge.store.migration.importer import importer_version
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
                archive.extractall(directory)
            script = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from forge.factory.config import validate
from forge.store.migration.importer import importer_version
from forge.store.migration.cutover import package_version
assert validate({})['provider']=='codex'
before=importer_version()
assert package_version(sys.argv[1])==before
path=Path(sys.argv[1])/'forge/harnesses/codex/manifest.json'
path.write_text(path.read_text()+'\\n')
after=importer_version()
assert after!=before and package_version(sys.argv[1])==after
print(json.dumps({'before':before}))
'''
            result=subprocess.run([sys.executable,'-I','-c',script,directory],cwd=directory,
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['before'],importer_version())

    def test_activate_requires_explicit_apply_and_routes_host_protocol(self):
        values=['migrate','activate','--ssh','forge','--source','/srv/source','--root','/srv/runtime']
        with patch('forge_client.migration.call',return_value={'status':'draining'}) as call:
            with self.assertRaises(SetupError):execute(parser().parse_args(values))
            call.assert_not_called()
            self.assertEqual(execute(parser().parse_args([*values,'--apply'])),{'status':'draining'})
            self.assertEqual(call.call_args.args[1],'activate')
            self.assertIs(call.call_args.kwargs['apply'],True)

    def test_agent_contract_exposes_one_way_activation(self):
        from forge_client.cli import migration_commands
        contract=migration_commands()['activate']
        self.assertTrue(contract['activation_supported'])
        self.assertTrue(contract['maintenance'])
        self.assertIn('apply',contract['input_schema']['required'])

    def test_bootstrap_is_executable_python_with_qualified_child(self):
        from forge_client.migration import BOOTSTRAP
        compile(BOOTSTRAP,'migration-bootstrap','exec')
        self.assertIn('LD_LIBRARY_PATH',BOOTSTRAP)
        self.assertIn('qualified_sqlite()',BOOTSTRAP)

    def test_drain_response_has_complete_resumable_command(self):
        import json
        import shlex
        from types import SimpleNamespace
        from forge_client.migration import call
        args = parser().parse_args(['migrate', 'activate', '--ssh', 'forge', '--source', '/srv/forge source', '--root', '/srv/runtime', '--apply'])
        response = {'ok': True, 'status': 'draining', 'next_command': 'forge migrate resume'}
        with patch('forge_client.migration.bundle', return_value=b''), patch('forge_client.migration.subprocess.run',
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response).encode())):
            value = call(args, 'activate', apply=True)
        command = shlex.split(value['next_command'])
        parsed = parser().parse_args(command[1:])
        self.assertEqual((parsed.migration_command, parsed.ssh, parsed.source, parsed.root),
                         ('resume', 'forge', '/srv/forge source', '/srv/runtime'))
