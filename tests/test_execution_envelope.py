"""Trusted execution observations never come from worker result prose."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.harnesses.execution import fault, exception_fault, provider_error, valid, finalize, write_receipt
from forge.factory.execution_problems import classify, contract_problem

IMAGE = 'sha256:' + 'a'*64
JOB = dict(id='attempt', container_id='container')


def capture(status='ok', **fields):
    envelope = dict(version=1, attempt='attempt', harness_status=status, exit_code=0, **fields)
    return dict(execution=dict(id='container', image_id=IMAGE, exit_protocol='1',
                              running=False, exit_code=0, oom_killed=False), execution_envelope=envelope)


class ExecutionEnvelopeTests(unittest.TestCase):
    def test_terminal_structured_rate_limit_only(self):
        event = dict(type='result', is_error=True, api_error_status=429, retry_after=45)
        self.assertEqual(provider_error(event), dict(code='rate_limit', status=429, retry_after=45))
        for event in ({'type': 'error', 'message': '429'},
                      {'type':'result', 'structured_output':{'api_error_status':429}},
                      {'type':'result','is_error':False,'api_error_status':429},
                      {'type':'assistant','error':'rate_limit','api_error_status':429},
                      {'type':'result','is_error':False,'api_error_status':'429'}):
            self.assertIsNone(provider_error(event))

    def test_typed_terminal_without_status_is_unknown_not_prose_inference(self):
        event={'type':'result','is_error':True,'errors':['quota exhausted']}
        self.assertEqual(provider_error(event),{'code':'unknown'})
        from forge.harnesses.execution import codex_provider_error
        self.assertEqual(codex_provider_error([{'type':'turn.failed','error':{'message':'anything'}}])['code'],'unknown')
        self.assertIsNone(codex_provider_error([{'type':'turn.failed','error':{}},{'type':'turn.completed'}]))
        self.assertIsNone(codex_provider_error([{'type':'item.completed','item':{'type':'agent_message','text':'turn.failed'}}]))

    def test_provider_cooldown_is_bounded_with_default(self):
        for requested, expected in ((0,1),(40,40),(9000,3600),(float('nan'),30),('120',30)):
            provider = provider_error(dict(type='result',is_error=True,api_error_status=429,retry_after=requested))
            value = capture('provider_error',provider_error=provider)
            result = classify(JOB,value,100)
            self.assertEqual(result['kind'],'provider_error')
            self.assertEqual(result['details']['retry_at'],100+expected)

    def test_structured_auth_and_overloaded_codes(self):
        for status, code in ((401,'auth'),(403,'auth'),(503,'overloaded'),(529,'overloaded')):
            self.assertEqual(provider_error(dict(type='result',is_error=True,api_error_status=status))['code'],code)

    def test_exception_receipt_omits_exception_message(self):
        try:
            exec(compile("raise ImportError('secret token and URL')", '/opt/forge/harness.py', 'exec'))
        except ImportError as error:
            detail = exception_fault(error)
        self.assertEqual(detail['line'],1)
        self.assertEqual(detail['exception'],'ImportError')
        self.assertNotIn('secret',json.dumps(detail))
        self.assertEqual(classify(JOB,capture('harness_error',harness_error=detail),100)['kind'],'forge_problem')

    def test_private_receipt_finalization_and_missing_envelope(self):
        with tempfile.TemporaryDirectory() as directory:
            receipt, envelope = Path(directory)/'receipt', Path(directory)/'envelope'
            value=finalize(JOB,1,receipt_path=receipt,envelope_path=envelope)
            self.assertEqual(value['harness_status'],'harness_error')
            self.assertTrue(valid(value,'attempt',final=True))
            write_receipt(JOB,'provider_error',provider_error=dict(code='rate_limit',status=429),path=receipt)
            value=finalize(JOB,1,receipt_path=receipt,envelope_path=envelope)
            self.assertEqual(value['harness_status'],'provider_error')
            self.assertEqual(envelope.stat().st_mode & 0o777,0o600)
            value=finalize(JOB,124,timeout=True,receipt_path=receipt,envelope_path=envelope)
            self.assertEqual(value['harness_status'],'timeout')

    def test_container_oom_overrides_other_envelope(self):
        value = capture('provider_error',provider_error=dict(code='rate_limit',status=429))
        value['execution']['oom_killed']=True
        value['timed_out']=True
        self.assertEqual(classify(JOB,value,100)['kind'],'oom')

    def test_timeout_requires_host_or_private_parent_observation(self):
        value=capture();value['logs']='timeout';value['result']={'harness_status':'timeout'}
        value['execution']['exit_code']=124;value['execution_envelope']['exit_code']=124
        self.assertEqual(classify(JOB,value,100)['kind'],'ok')
        value['timed_out']=True;value['execution']['running']=True
        self.assertEqual(classify(JOB,value,100)['kind'],'timeout')
        value=capture('timeout');self.assertEqual(classify(JOB,value,100)['kind'],'timeout')

    def test_worker_report_cannot_forge_provider_error(self):
        value=capture();value.pop('execution_envelope')
        value['result']=dict(harness_status='provider_error',provider_error=dict(code='rate_limit',status=429))
        value['logs']=json.dumps({'type':'result','is_error':True,'api_error_status':429})
        self.assertEqual(classify(JOB,value,100)['kind'],'forge_problem')

    def test_exact_container_image_attempt_and_exit_are_required(self):
        for key,value in (('id','other'),('image_id','forge:tag'),('exit_protocol',None)):
            data=capture();data['execution'][key]=value
            self.assertIsNone(classify(JOB,data,100))
        for key,value in (('attempt','other'),('exit_code',27),('version',True)):
            data=capture();data['execution_envelope'][key]=value
            self.assertEqual(classify(JOB,data,100)['kind'],'forge_problem')

    def test_malformed_provider_or_fault_receipt_is_not_accepted(self):
        examples=[capture('provider_error',provider_error={'code':'rate_limit','status':429,'retry_after':float('inf')}),
                  capture('harness_error',harness_error={**fault('ImportError'),'file':'/work/repository/private'}),
                  capture('harness_error',harness_error={**fault('ImportError'),'signature':'0'*64})]
        for value in examples:
            self.assertEqual(classify(JOB,value,100)['details']['reason'],'execution_envelope_missing')

    def test_contract_error_has_safe_image_signature(self):
        value=contract_problem(JOB,capture())
        self.assertEqual(value['reason'],'result_contract')
        self.assertEqual(value['exception'],'InvalidResultContract')
        self.assertIsNone(contract_problem(JOB,capture('timeout')))


from tests import test_store_result_controller as fixtures
from tests import test_forge_startup_problem as startup_fixtures
from forge.store.execution_problems import ExecutionProblems, draft_in


class ExecutionDispositionTests(unittest.TestCase):
    setUp=fixtures.ResultControllerTests.setUp
    view=fixtures.ResultControllerTests.view
    command=fixtures.ResultControllerTests.command
    capture=fixtures.ResultControllerTests.capture
    charges=fixtures.ResultControllerTests.charges
    report=fixtures.ResultControllerTests.report
    reserve=startup_fixtures.StartupRepairTests.reserve
    launch=startup_fixtures.StartupResultTests.launch
    save=startup_fixtures.StartupRepairTests.save

    def evidence(self, identifier, status='ok', **fields):
        value=capture(status,**fields)
        value['execution_envelope']['attempt']=identifier
        return value

    def test_missing_envelope_is_scoped_uncharged_draft_and_replay_safe(self):
        identifier=self.launch(); value=self.evidence(identifier); value.pop('execution_envelope')
        self.capture(identifier,**value); self.github.issue_error=True
        outcome=self.controller.step(identifier)
        self.assertFalse(outcome['charged']); self.assertEqual(self.charges(),0)
        self.assertEqual(self.controller.step(identifier)['outcome'],'deferred')
        report=ExecutionProblems(self.store).report(outcome['report_signature'])
        self.assertEqual(report['status'],'draft');self.assertEqual(len(report['affected']),1)
        with self.store.read_snapshot() as db:
            gate=db.execute('SELECT * FROM unit_gates WHERE id=?',(outcome['gate_id'],)).fetchone()
            self.assertEqual(gate['job_id'],identifier)
            self.assertEqual(db.execute('SELECT count(*) FROM forge_problem_reports').fetchone()[0],1)
        with self.store.write() as db:
            detail=json.loads(gate['details_json'])
            draft_in(db,identifier,{**detail,'logs':'synthetic-secret-credential','message':'private URL'},100)
        self.assertEqual(ExecutionProblems(self.store).report(outcome['report_signature']),report)
        self.assertNotIn('synthetic-secret',report['body'])

    def test_timeout_charges_once_and_records_recovery(self):
        identifier=self.launch();self.capture(identifier,**self.evidence(identifier,'timeout'))
        self.assertTrue(self.controller.step(identifier)['charged'])
        self.assertEqual(self.controller.step(identifier)['outcome'],'failure')
        self.assertEqual(self.charges(),1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempt_failures').fetchone()[0],1)

    def test_provider_error_cooldown_uncharged_and_replay_safe(self):
        identifier=self.launch()
        self.capture(identifier,**self.evidence(identifier,'provider_error',provider_error=dict(code='rate_limit',status=429)))
        self.github.issue_error=True
        value=self.controller.step(identifier)
        self.assertFalse(value['charged']);self.assertEqual(self.controller.step(identifier)['outcome'],'deferred')
        with self.store.read_snapshot() as db:
            gate=db.execute("SELECT * FROM unit_gates WHERE code='provider_unavailable'").fetchone()
            self.assertEqual(json.loads(gate['details_json'])['retry_at'],130)
            self.assertEqual(db.execute('SELECT count(*) FROM provider_cooldowns').fetchone()[0],1)
        self.assertEqual(self.charges(),0)

    def test_invalid_result_is_forge_contract_fault_but_valid_unable_is_work_failure(self):
        identifier=self.launch();self.capture(identifier,**self.evidence(identifier))
        value=self.controller.step(identifier)
        self.assertFalse(value['charged']);self.assertIn('report_signature',value)
        report=ExecutionProblems(self.store).report(value['report_signature'])
        self.assertIn('InvalidResultContract',report['title'])

    def test_valid_unable_keeps_ordinary_work_failure(self):
        identifier=self.launch();self.capture(identifier,self.report(identifier,'unable'),**self.evidence(identifier))
        self.assertTrue(self.controller.step(identifier)['charged']);self.assertEqual(self.charges(),1)

    def test_exhausted_oom_is_owned_resource_wait_without_charge(self):
        identifier=self.launch();value=self.evidence(identifier);value['execution']['oom_killed']=True
        self.capture(identifier,**value)
        outcome=self.controller.step(identifier)
        self.assertFalse(outcome['charged']);self.assertEqual(self.charges(),0)
        with self.store.read_snapshot() as db:
            detail=json.loads(db.execute("SELECT details_json FROM unit_gates WHERE code='environment'").fetchone()[0])
            self.assertTrue(detail['oom_killed'])

    def test_qualified_new_image_releases_fault_preserving_independent_hold(self):
        identifier=self.launch();self.capture(identifier,**self.evidence(identifier,'harness_error',harness_error=fault('ImportError')))
        gate=self.controller.step(identifier)['gate_id']; service=ExecutionProblems(self.store,clock=lambda:110)
        self.assertIn('pending',service.recheck(self.command(),gate))
        self.life.open_gate(self.command(),'label_hold',details={'label':'hold'})
        self.save('runtime.json',{'image':IMAGE});self.save('host.json',{'image':IMAGE,'updated_at':105})
        self.assertIn('pending',service.recheck(self.command(),gate))
        new='sha256:'+'b'*64
        self.save('runtime.json',{'image':new});self.save('host.json',{'image':new,'updated_at':105})
        self.assertTrue(service.recheck(self.command(),gate)['resolved'])
        with self.store.read_snapshot() as db:
            self.assertEqual([row[0] for row in db.execute('SELECT code FROM unit_gates WHERE resolved_at IS NULL')],['label_hold'])
        self.assertEqual(self.charges(),0)


if __name__=='__main__': unittest.main()
