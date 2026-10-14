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
    def test_upgrade_is_the_only_migrate_command(self):
        commands=execute(parser().parse_args(['--json','commands']))
        self.assertEqual(list(commands['migration_commands']),['upgrade'])
        self.assertEqual([c for c in commands['commands'] if c.startswith('migrate')],['migrate upgrade'])
        contract=commands['migration_commands']['upgrade']
        self.assertEqual(contract['input_schema']['required'],['ssh','source','root'])
        self.assertEqual(contract['input_schema']['properties']['source_revision']['pattern'],'^[0-9a-f]{40}$')
        for action in ('plan','prepare','capture','resume','import','verify','activate'):
            with self.subTest(action=action), patch('sys.stderr', io.StringIO()):
                with self.assertRaises(SystemExit):
                    parser().parse_args(['migrate',action,'--ssh','forge','--source','/srv/source','--root','/srv/runtime'])

    def test_upgrade_plan_and_pinned_apply_are_machine_readable(self):
        values=['migrate','upgrade','--ssh','forge','--source','/srv/source','--root','/srv/runtime']
        with patch('forge_client.migration.call',return_value={'status':'planned'}) as call:
            execute(parser().parse_args(values))
            self.assertEqual(call.call_args.kwargs,{'apply':False,'source_revision':None})
            call.reset_mock()
            with self.assertRaises(SetupError):execute(parser().parse_args([*values,'--apply']))
            call.assert_not_called()
            execute(parser().parse_args([*values,'--apply','--source-revision','a'*40]))
            self.assertEqual(call.call_args.kwargs,{'apply':True,'source_revision':'a'*40})

    def test_upgrade_draining_reply_contains_the_complete_pinned_resume_command(self):
        from types import SimpleNamespace
        from forge_client.migration import call
        args = parser().parse_args(['migrate','upgrade','--ssh','forge','--source','/srv/source',
            '--root','/srv/runtime','--apply','--source-revision','a'*40])
        reply = dict(ok=True,status='draining',next_command='forge migrate upgrade')
        with patch('forge_client.migration.bundle', return_value=b'code'), \
                patch('forge_client.migration.subprocess.run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(reply).encode())):
            result = call(args, 'upgrade', apply=True, source_revision=args.source_revision)
        self.assertEqual(result['next_command'], 'forge migrate upgrade --ssh forge --source /srv/source '
            '--root /srv/runtime --json --apply --source-revision '+'a'*40)

    def test_transfer_bundle_contains_only_code_schema_and_harness_manifests(self):
        with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
            names=archive.namelist()
            self.assertIn('forge/store/migration/host.py',names)
            self.assertIn('forge/store/schema/002_import_archive.sql',names)
            self.assertIn('forge/harnesses/codex/manifest.json', names)
            self.assertTrue(all(n.startswith('forge/') and (n.endswith(('.py','.sql')) or
                Path(n).match('forge/harnesses/*/manifest.json')) for n in names))

    def test_isolated_upgrade_bundle_owns_planning_contract_and_source_identity(self):
        from forge.store.migration.upgrade import package_version
        with tempfile.TemporaryDirectory() as directory:
            directory=str(Path(directory).resolve())
            with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
                self.assertIn('forge/planning_contract.py',archive.namelist())
                self.assertNotIn('forge_client/planning.py',archive.namelist())
                archive.extractall(directory)
            script = r'''import importlib.util,json,sys,tempfile
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
assert importlib.util.find_spec('forge_client') is None
from forge.store.migration.host import Host,run
from forge.store.migration.upgrade import Upgrade, package_version
from forge.store.repository_planning import RepositoryPlanning
from forge.store.fresh import initialize
from forge.store.transitions import Lifecycle
before=package_version(sys.argv[1])
with tempfile.TemporaryDirectory() as runtime:
    root=Path(runtime).resolve()
    store=initialize(root)
    Lifecycle(store).admit_issue(request_id='admit',actor_id='operator',unit_id='unit',issue_id='1:1',
        repository_id=1,repository_name='example/project',issue_number=1,title='Work',body='',requirements='req',observed_at=1)
    plans=RepositoryPlanning(store)
    proposal=plans.queue(1,request_id='proposal',actor_id='operator',base_branch='main')
    context=plans.context(proposal['proposal_id'],head='a'*40)
    assert context['rows_schema']['anyOf'] and context['note_sources_schema']['anyOf']
    with patch.object(Host,'identity',return_value='a'*40):
        status=Upgrade(Host(root,sys.argv[1])).status()
        result=run(dict(action='upgrade',root=str(root),source=sys.argv[1],apply=False))
    assert status['blockers']==[] and result['status']=='planned'
    assert result['current_schema']==result['target_schema']
path=Path(sys.argv[1])/'forge/planning_contract.py'
path.write_text(path.read_text()+'\n')
after=package_version(sys.argv[1])
assert after!=before
print(json.dumps({'before':before,'isolated_upgrade':'planned','contract_change_hashed':True}))
'''
            result=subprocess.run([sys.executable,'-I','-c',script,directory],cwd=directory,
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(result.stdout)
            self.assertEqual(report['before'],package_version(Path(__file__).resolve().parents[1]))
            self.assertEqual(report['isolated_upgrade'],'planned')
            self.assertTrue(report['contract_change_hashed'])

    def test_isolated_bundle_validates_runtime_and_binds_manifest_identity(self):
        from forge.store.migration.upgrade import package_version
        with tempfile.TemporaryDirectory() as directory:
            directory = str(Path(directory).resolve())
            with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
                archive.extractall(directory)
            script = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from forge.factory.config import validate
from forge.store.migration.upgrade import package_version
assert validate({})['provider']=='codex'
before=package_version(sys.argv[1])
path=Path(sys.argv[1])/'forge/harnesses/codex/manifest.json'
path.write_text(path.read_text()+'\\n')
after=package_version(sys.argv[1])
assert after!=before
print(json.dumps({'before':before}))
'''
            result=subprocess.run([sys.executable,'-I','-c',script,directory],cwd=directory,
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['before'],package_version(Path(__file__).resolve().parents[1]))

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
        args = parser().parse_args(['migrate', 'upgrade', '--ssh', 'forge', '--source', '/srv/forge source', '--root', '/srv/runtime'])
        response = {'ok': True, 'status': 'draining', 'next_command': 'forge migrate upgrade'}
        with patch('forge_client.migration.bundle', return_value=b''), patch('forge_client.migration.subprocess.run',
                return_value=SimpleNamespace(returncode=0, stdout=json.dumps(response).encode())):
            value = call(args, 'upgrade', apply=False)
        command = shlex.split(value['next_command'])
        parsed = parser().parse_args(command[1:])
        self.assertEqual((parsed.migration_command, parsed.ssh, parsed.source, parsed.root),
                         ('upgrade', 'forge', '/srv/forge source', '/srv/runtime'))

    def test_remote_error_contains_safe_type_stage_and_complete_resume_command(self):
        from types import SimpleNamespace
        from forge_client.migration import call
        args=parser().parse_args(['migrate','upgrade','--ssh','forge','--source','/srv/forge source','--root','/srv/runtime',
                                  '--apply','--source-revision','a'*40])
        response={'ok':False,'error':'unexpected_migration_failure','exception_type':'IntegrityError','stage':'migrating'}
        with patch('forge_client.migration.bundle',return_value=b''), patch('forge_client.migration.subprocess.run',
                return_value=SimpleNamespace(returncode=0,stdout=json.dumps(response).encode())):
            with self.assertRaises(SetupError) as error:call(args,'upgrade',apply=True,source_revision=args.source_revision)
        self.assertIn('IntegrityError at migrating',str(error.exception))
        self.assertIn("--source '/srv/forge source' --root /srv/runtime",str(error.exception))
        self.assertIn('--apply --source-revision '+'a'*40,str(error.exception))
