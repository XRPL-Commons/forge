"""Failed acceptance retains bounded diagnostics and still exits unsuccessfully."""
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge_client.cli import main
from forge_client.role_qualification import execute, RoleQualificationFailed


class RoleFailureReportTests(unittest.TestCase):
    def report(self):
        return dict(status='failed', reason='provider_execution_not_confirmed', current_role='build',
            source_revision='a'*40, image='sha256:'+'b'*64, cleanup='confirmed', automation_after='disabled',
            results=[dict(role='review', status='qualified', job_id='12345678-1234-1234-1234-123456789abc',
                          summary='private model text', outcome='pass')],
            failure=dict(role='build', result_present=False, missing=['result'],
                execution=dict(running=False,exit_code=1,oom_killed=False),
                execution_envelope=dict(harness_status='provider_error',
                    provider_error=dict(code='auth',status=401,retry_after=20,message='private provider reply'))),
            logs='private logs', result={'summary':'private output'}, retained_evidence_directory='/private/secret')

    def call(self, report):
        installation=Mock()
        installation.journal.read.return_value={}
        installation.host.rpc.return_value={'result':report}
        return execute(SimpleNamespace(ssh='anvil',repo='acme/project',issue=10,apply=True),installation=installation)

    def test_failed_report_retains_safe_progress_without_capture_contents(self):
        with self.assertRaises(RoleQualificationFailed) as caught:
            self.call(self.report())
        report=caught.exception.report
        self.assertEqual(report['current_role'],'build')
        self.assertEqual(report['cleanup'],'confirmed')
        self.assertEqual(report['source_revision'],'a'*40)
        self.assertEqual(report['image'],'sha256:'+'b'*64)
        self.assertEqual(report['results'][0]['role'],'review')
        self.assertEqual(report['failure']['execution']['exit_code'],1)
        self.assertEqual(report['failure']['execution_envelope']['provider_error'],dict(code='auth',status=401,retry_after=20))
        self.assertNotIn('private',json.dumps(report))

    def test_cli_failure_emits_report_and_returns_nonzero(self):
        error=RoleQualificationFailed(self.report())
        output=io.StringIO()
        with patch('forge_client.cli.execute',side_effect=error),patch('sys.stdout',output):
            code=main(['setup','qualify-roles','--ssh','anvil','--repo','acme/project','--issue','10','--apply','--json'])
        report=json.loads(output.getvalue())
        self.assertEqual(code,1)
        self.assertFalse(report['ok'])
        self.assertEqual(report['report']['current_role'],'build')
        self.assertNotIn('private',output.getvalue())

    def test_unconfirmed_cleanup_still_fails_with_diagnostics(self):
        with self.assertRaises(RoleQualificationFailed) as caught:
            self.call({**self.report(),'status':'qualified','cleanup':'unconfirmed'})
        self.assertEqual(caught.exception.report['cleanup'],'unconfirmed')

    def test_untrusted_fields_are_bounded_and_omitted(self):
        value=self.report()
        value.update(current_role='private role',source_revision='private source',image='private image',reason='private message',
                     results=value['results']*100)
        value['failure']['missing']=['private code',{'private':'value'},'result']
        value['failure']['execution_envelope']['provider_error'].update(retry_after=float('inf'),status=True)
        report=RoleQualificationFailed(value).report
        self.assertNotIn('private',json.dumps(report))
        self.assertEqual(len(report['results']),7)
        self.assertEqual(report['failure']['missing'],['result'])
        self.assertEqual(report['failure']['execution_envelope']['provider_error'],{'code':'auth'})

    def test_startup_projection_keeps_only_fixed_error_class(self):
        value=self.report()
        value['failure']['startup']=dict(reason='harness_startup',phase='before_agent',
            exception='ModuleNotFoundError',file='/private/secret',message='private error',signature='private hash')
        report=RoleQualificationFailed(value).report
        self.assertEqual(report['failure']['startup'],dict(reason='harness_startup',phase='before_agent',
            exception='ModuleNotFoundError'))
        self.assertNotIn('private',json.dumps(report))
        value['failure']['startup']['exception']='private exception'
        self.assertNotIn('startup',RoleQualificationFailed(value).report['failure'])
