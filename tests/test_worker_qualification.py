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
            compile(WORKER,'<worker-qualification>','exec')

    def test_cli_plan_is_read_only_and_failed_receipt_cannot_be_success(self):
        args=SimpleNamespace(image='sha256:'+'a'*64,ssh='anvil',apply=False)
        installation=Mock()
        self.assertEqual(execute(args,installation=installation)['status'],'planned')
        installation.host.rpc.assert_not_called()
        args.apply=True;installation.journal.read.return_value={'host_key':'verified'}
        installation.host.rpc.return_value={'ok':True,'result':{'status':'failed','reason':'canary failed'}}
        with self.assertRaisesRegex(SetupError,'canary failed'):execute(args,installation=installation)
        self.assertEqual(installation.host.fingerprint,'verified')
        installation.host.rpc.return_value={'ok':True,'result':{'status':'qualified','cleanup':'unconfirmed'}}
        with self.assertRaises(SetupError):execute(args,installation=installation)
        installation.host.rpc.return_value={'ok':True,'result':{'status':'qualified','cleanup':'confirmed'}}
        self.assertEqual(execute(args,installation=installation)['status'],'qualified')
