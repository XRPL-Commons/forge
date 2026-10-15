import io
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from forge_client.cli import parser, main
from forge_client.contracts import SetupError
from forge_client.server_identity import execute


class ServerIdentityTests(unittest.TestCase):
    def args(self, step='plan', *extra):
        return parser().parse_args(['server','identity',step,'--ssh','host','--source','/srv/forge',
            '--root','/srv/private','--service-user','forge',*extra])

    def test_plan_runs_as_service_account_without_secrets_in_arguments(self):
        run=Mock(return_value=subprocess.CompletedProcess([],0,json.dumps({'ok':True,'steps':{'apps':'ready'}}).encode()))
        self.assertEqual(execute(self.args(),run=run)['steps'],{'apps':'ready'})
        request=json.loads(run.call_args.kwargs['input'])
        self.assertEqual(request['step'],'plan');self.assertEqual(request['service_user'],'forge')
        self.assertIn('StrictHostKeyChecking=yes',run.call_args.args[0])
        self.assertIsNone(request['secret'])

    def test_secret_uses_only_stdin_and_a_failed_host_step_is_failure(self):
        secret='synthetic-identity-secret'
        run=Mock(return_value=subprocess.CompletedProcess([],0,b'{"ok":true,"status":"rotated"}'))
        result=execute(self.args('sign-in','--secret-stdin'),run=run,stdin=io.StringIO(secret+'\n'))
        self.assertEqual(result['status'],'rotated')
        self.assertNotIn(secret,str(run.call_args.args))
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['secret'],secret)
        run.return_value=subprocess.CompletedProcess([],0,b'{"ok":false,"error":"plan_changed"}',b'private host diagnostic')
        with self.assertRaises(SetupError) as failure:execute(self.args(),run=run)
        self.assertEqual(failure.exception.code,'identity_plan_changed')
        self.assertNotIn('private host diagnostic',str(failure.exception))

    def test_invalid_apply_or_secret_input_never_contacts_host(self):
        for args in (self.args('apps'),self.args('apps','--revision','bad'),self.args('plan','--secret-stdin')):
            run=Mock()
            with self.assertRaises(SetupError):execute(args,run=run)
            run.assert_not_called()

    def test_cli_dispatch_and_failed_step_exit_code(self):
        with patch('forge_client.server_identity.execute',side_effect=SetupError('identity_plan_changed','Read a new plan')),patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(main(['server','identity','apps','--ssh','host','--source','/srv/forge',
                '--root','/srv/private','--service-user','forge','--revision','a'*64]),1)
        self.assertFalse(json.loads(output.getvalue())['ok'])
