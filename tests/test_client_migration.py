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
        with patch('forge_client.migration.call',return_value={'status':'planned','source_revision':'b'*40}) as call:
            execute(parser().parse_args(values))
            self.assertEqual(call.call_args.kwargs,{'apply':False,'source_revision':None})
            call.reset_mock()
            with self.assertRaises(SetupError):execute(parser().parse_args([*values,'--apply']))
            call.assert_not_called()
            pack={'data':'bounded-pack','sha256':'digest'}
            with patch('forge_client.source.export',return_value=pack) as export:
                execute(parser().parse_args([*values,'--apply','--source-revision','a'*40]))
            export.assert_called_once_with('a'*40,previous='b'*40)
            self.assertEqual(call.call_args_list[0].kwargs,{'apply':False,'source_revision':'a'*40})
            self.assertEqual(call.call_args.kwargs,{'apply':True,'source_revision':'a'*40,'source_pack':pack})

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
            self.assertEqual({n for n in names if n.startswith('forge_client/')},{'forge_client/__init__.py','forge_client/contracts.py'})
            self.assertTrue(all(n in {'forge_client/__init__.py','forge_client/contracts.py'} or n.startswith('forge/') and (n.endswith(('.py','.sql')) or
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
assert importlib.util.find_spec('forge_client.contracts') is not None
assert importlib.util.find_spec('forge_client.cli') is None
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

    def test_isolated_bundle_executes_fenced_probe_cleanup_and_binds_shared_contract(self):
        from forge.store.migration.upgrade import package_version
        with tempfile.TemporaryDirectory() as directory:
            directory=str(Path(directory).resolve())
            with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
                archive.extractall(directory)
            script = r'''import importlib.util,json,sys,tempfile,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,sys.argv[1])
from forge.store.fresh import initialize
from forge.onboarding import SetupState
from forge.setup_service import Service
from forge.factory.probe_runner import ProbeRunner,PROVIDER
from forge.factory.runner import ContainerRunner
from forge.factory.worker_boundary import WorkerBoundary
from forge.store.migration.probe_cleanup import cleanup_fenced_probe
from forge.store.migration.upgrade import package_version
assert importlib.util.find_spec('forge_client.cli') is None
before=package_version(sys.argv[1])
with tempfile.TemporaryDirectory() as runtime:
    root=Path(runtime).resolve();store=initialize(root)
    state=SetupState(root,store,time.time)
    config=dict(image='sha256:'+'a'*64,network='forge-workers',harness_argv=PROVIDER,
                grace_seconds=2,provider='codex')
    job=Service.new_probe(None,config,'provider')
    operation=dict(id='existing-operation',action='harness_check',step='probe')
    with state.transaction() as data:
        data.update(generation=1,phase='setup',probe_jobs=[job],operation=operation,
                    error='auxiliary execution inputs changed')
    with state.transaction() as data:
        data['probe_jobs'][0].update(fenced=True,capacity_wait_since=101)
    with patch.object(ContainerRunner,'reset',return_value={'container_absent':True}), \
            patch.object(WorkerBoundary,'inspect',return_value=None):
        assert cleanup_fenced_probe(root,store)==1
    data=state.read()
    assert data['probe_jobs'][0]['cleanup'] is True
    assert 'execution_fence' not in data['probe_jobs'][0]
    assert data['operation']==operation and data['error']=='auxiliary execution inputs changed'
    with store.read_snapshot() as db:
        assert db.execute('SELECT count(*) FROM auxiliary_leases').fetchone()[0]==0
path=Path(sys.argv[1])/'forge_client/contracts.py'
path.write_text(path.read_text()+'\n')
assert package_version(sys.argv[1])!=before
path.unlink()
try: package_version(sys.argv[1])
except FileNotFoundError: pass
else: raise AssertionError('Missing contract accepted')
print(json.dumps({'before':before,'cleanup':'confirmed'}))
'''
            result=subprocess.run([sys.executable,'-I','-c',script,directory],cwd=directory,
                                  capture_output=True,text=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
            report=json.loads(result.stdout)
            self.assertEqual(report['before'],package_version(Path(__file__).resolve().parents[1]))
            self.assertEqual(report['cleanup'],'confirmed')

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


    def test_failed_updater_output_tail_reaches_the_operator(self):
        from types import SimpleNamespace
        from forge_client.migration import call
        args=parser().parse_args(['migrate','upgrade','--ssh','forge','--source','/srv/source','--root','/srv/runtime',
                                  '--apply','--source-revision','a'*40])
        response={'ok':False,'error':'upgrade_host_update_failed','exception_type':'MigrationError','stage':'reconciling',
                  'detail':'Error response from daemon: No such container: forge-ui-0123456789ab'}
        with patch('forge_client.migration.bundle',return_value=b''), patch('forge_client.migration.subprocess.run',
                return_value=SimpleNamespace(returncode=0,stdout=json.dumps(response).encode())):
            with self.assertRaises(SetupError) as error:call(args,'upgrade',apply=True,source_revision=args.source_revision)
        self.assertIn('upgrade_host_update_failed (MigrationError at reconciling)',str(error.exception))
        self.assertIn('Host output (last lines):\nError response from daemon: No such container',str(error.exception))

    def test_resume_after_checkout_needs_no_new_github_access(self):
        args=parser().parse_args(['migrate','upgrade','--ssh','anvil','--source','/srv/source',
            '--root','/srv/runtime','--apply','--source-revision','a'*40])
        with patch('forge_client.migration.call',return_value={'status':'planned','source_revision':'a'*40}) as call, \
                patch('forge_client.source.export',side_effect=AssertionError('unexpected GitHub access')):
            execute(args)
        self.assertEqual(call.call_args.kwargs,{'apply':True,'source_revision':'a'*40})

    def test_local_source_failure_stops_before_remote_apply(self):
        args=parser().parse_args(['migrate','upgrade','--ssh','anvil','--source','/srv/source',
            '--root','/srv/runtime','--apply','--source-revision','a'*40])
        with patch('forge_client.migration.call',return_value={'status':'planned','source_revision':'b'*40}) as call, \
                patch('forge_client.source.export',side_effect=SetupError('source_unavailable','local source unavailable')):
            with self.assertRaises(SetupError):execute(args)
        self.assertEqual(call.call_count,1)
        self.assertFalse(call.call_args.kwargs['apply'])
