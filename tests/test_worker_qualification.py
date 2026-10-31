"""Qualification refuses unsupported hosts and uses real, credential-free Git."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from forge.worker_qualification import Fixture, WORKER, qualify
from forge.factory.worker_boundary import BoundaryError
from forge.mirror.frontend import Frontend
from forge.mirror.git_store import run_git
from forge.mirror.policy import MirrorError
from forge.mirror.service import SessionServer
from forge_client.boundary_qualification import execute
from forge_client.contracts import SetupError
from tests.test_worker_boundary import Engine


class QualificationTests(unittest.TestCase):
    def test_unsupported_platform_and_engine_are_refused_before_mutation(self):
        engine=Engine();engine.version='23.0.5'
        with patch('forge.worker_qualification.platform.system',return_value='Darwin'):
            with self.assertRaisesRegex(BoundaryError,'Linux'):qualify('sha256:'+'a'*64,docker=engine)
        self.assertFalse(engine.calls)
        with patch('forge.worker_qualification.platform.system',return_value='Linux'):
            with self.assertRaisesRegex(BoundaryError,'Engine 28'):qualify('sha256:'+'a'*64,docker=engine)
        self.assertEqual(len(engine.calls),1)
        self.assertFalse(any(engine.resources.values()))

    def test_image_protocol_is_checked_before_resource_creation(self):
        calls=[]
        def docker(argv,**kwargs):
            calls.append(argv)
            return SimpleNamespace(stdout='29.5.3' if argv[0]=='version' else json.dumps([dict(Id='sha256:'+'a'*64,Config={'Labels':{}})]))
        with patch('forge.worker_qualification.platform.system',return_value='Linux'):
            with self.assertRaisesRegex(BoundaryError,'v2 image'):qualify('sha256:'+'a'*64,docker=docker)
        self.assertEqual([row[:2] for row in calls],[['version','--format'],['image','inspect']])

    def test_fixture_serves_exact_clone_then_revokes_without_any_credentials(self):
        with TemporaryDirectory(prefix='forge-q-',dir='/tmp') as directory:
            root=Path(directory);fixture=Fixture(root)
            with SessionServer(root/'session.sock',fixture.broker()) as server:
                server_thread=threading.Thread(target=server.serve_forever,daemon=True);server_thread.start()
                try:
                    with Frontend(('127.0.0.1',0),str(root/'session.sock')) as frontend:
                        frontend_thread=threading.Thread(target=frontend.serve_forever,daemon=True);frontend_thread.start()
                        try:
                            url=f'http://127.0.0.1:{frontend.server_port}/github/canary/project.git'
                            run_git(['clone',url,str(root/'checkout')])
                            self.assertEqual(run_git(['-C',str(root/'checkout'),'rev-parse','HEAD']).decode().strip(),fixture.oid)
                            with self.assertRaises(MirrorError):run_git(['-C',str(root/'checkout'),'push','origin','HEAD:refs/heads/forbidden'])
                            fixture.revoked=True
                            with self.assertRaises(MirrorError):run_git(['-C',str(root/'checkout'),'fetch','origin'])
                        finally:frontend.shutdown();frontend_thread.join()
                finally:server.shutdown();server_thread.join()
            self.assertTrue(any(row['outcome']=='served' for row in fixture.receipts))
            self.assertTrue(any(row.get('reason')=='session_revoked' for row in fixture.receipts))
            started={row['receipt_id'] for row in fixture.receipts if row['outcome']=='started'}
            finished=[row['request_id'] for row in fixture.receipts if row['outcome'] in ('served','denied','failed')]
            self.assertTrue(started, 'starting receipts must retain their original snapshot')
            self.assertTrue(all(identifier in started for identifier in finished))
            self.assertEqual(len(finished),len(set(finished)), 'each terminal receipt belongs to one request')
            compile(WORKER,'<worker-qualification>','exec')

    def test_cli_plan_is_read_only_and_failed_receipt_cannot_be_success(self):
        args=SimpleNamespace(image='sha256:'+'a'*64,ssh='anvil',apply=False)
        installation=Mock()
        self.assertEqual(execute(args,installation=installation)['status'],'planned')
        installation.host.rpc.assert_not_called()
        args.apply=True;installation.journal.read.return_value={'host_key':'verified'}
        installation.host.rpc.return_value={'ok':True,'result':{'status':'failed','reason':'canary failed','image':args.image}}
        with self.assertRaisesRegex(SetupError,'canary failed'):execute(args,installation=installation)
        self.assertEqual(installation.host.fingerprint,'verified')
        installation.host.rpc.return_value={'ok':True,'result':{'status':'qualified','cleanup':'unconfirmed','image':args.image}}
        with self.assertRaises(SetupError):execute(args,installation=installation)
        installation.host.rpc.return_value={'ok':True,'result':{'status':'qualified','cleanup':'confirmed','image':args.image}}
        self.assertEqual(execute(args,installation=installation)['status'],'qualified')


    def test_cli_default_plan_queries_host_and_apply_selects_installed_image(self):
        args=SimpleNamespace(image=None,ssh='anvil',apply=False)
        installation=Mock();installation.journal.read.return_value={}
        installed='sha256:'+'b'*64
        installation.host.rpc.return_value={'ok':True,'result':dict(status='planned',image=installed,image_selection='installed',source_revision='a'*40)}
        report=execute(args,installation=installation)
        self.assertEqual(report['image'],installed)
        self.assertEqual(installation.host.rpc.call_args.args[1],dict(version=1,image=None,apply=False,provider_proxy=False))
        args.apply=True
        installation.host.rpc.return_value={'ok':True,'result':dict(status='qualified',image=installed,cleanup='confirmed')}
        self.assertEqual(execute(args,installation=installation)['image'],installed)
        self.assertEqual(installation.host.rpc.call_args.args[1],dict(version=1,image=None,apply=True,provider_proxy=False))

    def test_remote_default_plan_only_inspects_and_apply_passes_selected_image(self):
        import io
        from forge_client.boundary_qualification import HOST
        namespace={};exec(HOST,namespace)
        namespace['Path']=Mock(return_value=SimpleNamespace(is_symlink=lambda:False,stat=lambda:SimpleNamespace(st_uid=1000)))
        installed='sha256:'+'b'*64
        observed=dict(image=installed,source_revision='a'*40,engine_version='29.1.3',seccomp='/installed/runner-seccomp.json')
        for apply, provider in ((False,False),(True,False),(True,True)):
            with self.subTest(apply=apply,provider=provider), patch('pwd.getpwnam',return_value=SimpleNamespace(pw_uid=1000,pw_dir='/home/forge')), \
                    patch('sys.stdin',SimpleNamespace(buffer=io.BytesIO(json.dumps(dict(version=1,image=None,apply=apply,provider_proxy=provider)).encode()))), \
                    patch('subprocess.run',side_effect=[SimpleNamespace(returncode=0,stdout=json.dumps(observed)),
                        SimpleNamespace(returncode=0,stdout=json.dumps(dict(status='qualified',cleanup='confirmed')))]) as run, \
                    patch('builtins.print') as output:
                self.assertEqual(namespace['main'](None),0)
                result=json.loads(output.call_args.args[0])['result']
                self.assertEqual(result['image'],installed)
                self.assertEqual(result['image_selection'],'installed')
                self.assertEqual(run.call_count,2 if apply else 1)
                if apply:
                    expected=['--image',installed]+(['--provider-proxy','--seccomp',observed['seccomp']] if provider else [])
                    self.assertEqual(run.call_args.args[0][-len(expected):],expected)
                else:
                    self.assertEqual(result['status'],'planned')
