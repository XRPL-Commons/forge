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
    def test_commands_discover_initial_activation_source_revision(self):
        schema=execute(parser().parse_args(['--json','commands']))['migration_commands']['activate']
        self.assertTrue(schema['initial_source_upgrade'])
        field=schema['input_schema']['properties']['source_revision']
        self.assertEqual(field['pattern'],'^[0-9a-f]{40}$')
        self.assertIn('initial_setup=true',field['description'])

    def test_initial_activation_exports_local_forward_source_and_pins_revisions(self):
        args=parser().parse_args(['migrate','activate','--ssh','anvil','--source','/srv/source','--root','/srv/runtime','--apply','--source-revision','b'*40])
        with patch('forge_client.migration.call',side_effect=[{'initial_setup':True,'plan':'p','source_revision':'a'*40},{'status':'active'}]) as call, \
                patch('forge_client.source.export',return_value={'sha256':'digest','data':'pack'}) as export:
            self.assertEqual(execute(args)['status'],'active')
        export.assert_called_once_with('b'*40,previous='a'*40)
        self.assertEqual(call.call_args.kwargs,dict(apply=True,previous_revision='a'*40,source_revision='b'*40,source_pack={'sha256':'digest','data':'pack'}))

    def test_initial_source_replacement_is_discoverable_and_pins_explicit_mode(self):
        commands=execute(parser().parse_args(['--json','commands']))['migration_commands']
        schema=commands['activate']['input_schema']
        self.assertEqual(schema['properties']['replace_initial_source']['const'],True)
        self.assertEqual(schema['dependentRequired']['replace_initial_source'],['source_revision'])
        for action,contract in commands.items():
            if action!='activate':self.assertNotIn('replace_initial_source',contract['input_schema']['properties'])
        args=parser().parse_args(['migrate','activate','--ssh','anvil','--source','/srv/source','--root','/srv/runtime',
                                 '--apply','--source-revision','b'*40,'--replace-initial-source'])
        with patch('forge_client.migration.call',side_effect=[{'initial_setup':True,'source_revision':'a'*40},{'status':'active'}]) as call, \
                patch('forge_client.source.export',return_value={'sha256':'digest','data':'pack'}) as export:
            self.assertEqual(execute(args)['status'],'active')
        export.assert_called_once_with('b'*40,previous='a'*40,replace_initial_source=True)
        self.assertEqual(call.call_args.kwargs,dict(apply=True,previous_revision='a'*40,source_revision='b'*40,
                         source_pack={'sha256':'digest','data':'pack'},replace_initial_source=True))

    def test_initial_source_replacement_rejects_other_operations_and_missing_pins_before_io(self):
        common=['--ssh','anvil','--source','/srv/source','--root','/srv/runtime','--replace-initial-source']
        cases=[['migrate',action,*common,'--apply','--source-revision','b'*40]
               for action in ('plan','prepare','capture','resume','import','verify','upgrade')]
        cases.extend([['migrate','activate',*common,'--source-revision','b'*40],
                      ['migrate','activate',*common,'--apply'],
                      ['migrate','activate',*common,'--apply','--source-revision','main'],
                      ['migrate','activate',*common,'--apply','--source-revision','']])
        for argv in cases:
            with self.subTest(argv=argv),patch('forge_client.migration.call') as call,patch('forge_client.source.export') as export:
                with self.assertRaises(SetupError):execute(parser().parse_args(argv))
                call.assert_not_called();export.assert_not_called()

    def test_initial_source_replacement_rejects_started_host_before_export(self):
        args=parser().parse_args(['migrate','activate','--ssh','anvil','--source','/srv/source','--root','/srv/runtime',
                                 '--apply','--source-revision','b'*40,'--replace-initial-source'])
        with patch('forge_client.migration.call',return_value={'initial_setup':False,'source_revision':'a'*40}) as call, \
                patch('forge_client.source.export') as export:
            with self.assertRaises(SetupError) as error:execute(args)
        self.assertEqual(error.exception.code,'migration_initial_setup_required')
        self.assertEqual(call.call_count,1);export.assert_not_called()

    def test_initial_source_bridge_rejects_active_host_before_export(self):
        args=parser().parse_args(['migrate','activate','--ssh','anvil','--source','/srv/source','--root','/srv/runtime','--apply','--source-revision','b'*40])
        with patch('forge_client.migration.call',return_value={'initial_setup':False}) as call, patch('forge_client.source.export') as export:
            with self.assertRaises(SetupError):execute(args)
        self.assertEqual(call.call_count,1);export.assert_not_called()

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

    def test_isolated_upgrade_bundle_owns_planning_contract_and_source_identity(self):
        from forge.store.migration.importer import importer_version
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
from forge.store.migration.upgrade import Upgrade
from forge.store.migration.importer import importer_version
from forge.store.migration.cutover import package_version
from forge.store.repository_planning import RepositoryPlanning
from forge.store.fresh import initialize
from forge.store.transitions import Lifecycle
before=importer_version()
assert before==package_version(sys.argv[1])
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
after=importer_version()
assert after!=before and package_version(sys.argv[1])==after
print(json.dumps({'before':before,'isolated_upgrade':'planned','contract_change_hashed':True}))
'''
            result=subprocess.run([sys.executable,'-I','-c',script,directory],cwd=directory,
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(result.stdout)
            self.assertEqual(report['before'],importer_version())
            self.assertEqual(report['isolated_upgrade'],'planned')
            self.assertTrue(report['contract_change_hashed'])

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

    def test_forward_repair_requires_explicit_resume_and_pinned_revision(self):
        base=['migrate','resume','--ssh','forge','--source','/srv/source','--root','/srv/runtime']
        with patch('forge_client.migration.call',return_value={'status':'active'}) as call:
            for extra in (['--accept-tool-update'],['--source-revision','a'*40],
                          ['--accept-tool-update','--source-revision','main']):
                with self.assertRaises(SetupError):execute(parser().parse_args([*base,*extra]))
            call.assert_not_called()
            self.assertEqual(execute(parser().parse_args([*base,'--accept-tool-update','--source-revision','a'*40])),{'status':'active'})
            self.assertEqual(call.call_args.kwargs,{'accept_tool_update':True,'source_revision':'a'*40})
            with self.assertRaises(SetupError):
                execute(parser().parse_args(['migrate','activate',*base[2:],'--accept-tool-update','--source-revision','a'*40]))

    def test_remote_error_contains_safe_type_stage_and_complete_repair_command(self):
        from types import SimpleNamespace
        from forge_client.migration import call
        args=parser().parse_args(['migrate','resume','--ssh','forge','--source','/srv/forge source','--root','/srv/runtime'])
        response={'ok':False,'error':'unexpected_migration_failure','exception_type':'IntegrityError','stage':'importing'}
        with patch('forge_client.migration.bundle',return_value=b''), patch('forge_client.migration.subprocess.run',
                return_value=SimpleNamespace(returncode=0,stdout=json.dumps(response).encode())):
            with self.assertRaises(SetupError) as error:call(args,'resume')
        self.assertIn('IntegrityError at importing',str(error.exception))
        self.assertIn("--source '/srv/forge source' --root /srv/runtime",str(error.exception))
        self.assertIn('--accept-tool-update --source-revision <full-commit-sha>',str(error.exception))
