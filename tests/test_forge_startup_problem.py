"""Only trusted pre-agent image failures become noncharged Forge-owned waits."""
import io
import json
import os
import select
import subprocess
import sys
from pathlib import Path
import tarfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge.factory import job as entrypoint, startup
from forge.factory.startup_problems import problem
from forge.factory.store_views import PipelineViews
from forge.store.queries import Queries
from tests import test_store_result_controller as fixtures

IMAGE = 'sha256:' + 'a'*64


def receipt(attempt):
    try:
        exec(compile("raise ModuleNotFoundError('sensitive message')", '/opt/forge/harness.py', 'exec'), {})
    except ModuleNotFoundError as error:
        return {**startup.failure(error), 'attempt': attempt}


class StartupBoundaryTests(unittest.TestCase):
    def test_pre_agent_import_bug_has_bounded_secret_free_signature(self):
        value = receipt('job')
        self.assertNotIn('sensitive', json.dumps(value))
        raw = json.dumps({k:v for k,v in value.items() if k!='attempt'}).encode()
        code, proof = entrypoint.completed_startup(raw, 1, 'job')
        self.assertEqual(code, 78); self.assertEqual(proof, value)

    def test_syntax_receipt_names_unexecuted_image_file_and_line(self):
        try:
            exec(compile("compile('def broken(:', '/opt/forge/harnesses/schema.py', 'exec')", '/opt/forge/startup.py', 'exec'), {})
        except SyntaxError as error:
            value=startup.failure(error)
        self.assertEqual((value['file'],value['line']),('/opt/forge/harnesses/schema.py',1))
        self.assertEqual(entrypoint.startup_receipt(json.dumps(value).encode(),1),value)

    def test_after_agent_exit78_and_worker_prose_cannot_become_startup_proof(self):
        raw = json.dumps({k:v for k,v in receipt('job').items() if k!='attempt'}).encode()
        for value in (b'agent_started\n'+raw, b'ImportError: Forge crashed', b'', b'{"exception":[]}'):
            self.assertEqual(entrypoint.completed_startup(value, 78, 'job'), (1, None))
        self.assertIsNone(entrypoint.startup_receipt(raw, 0))

    def test_expected_config_input_and_preparation_exceptions_are_not_forge_bugs(self):
        for exception in ('ValueError','KeyError','TypeError','OSError','RuntimeError'):
            try:
                exec(compile(f"raise {exception}('private input')", '/opt/forge/harness.py','exec'), {})
            except Exception as error:
                self.assertIsNone(startup.failure(error), exception)
        try:
            exec(compile("raise ImportError('test dependency')", '/work/repository/tests/test_app.py', 'exec'), {})
        except ImportError as error:
            self.assertIsNone(startup.failure(error))

    def test_wrapper_ignores_import_error_after_descriptor_handoff(self):
        read_fd, write_fd = os.pipe()
        def run(*args, **kwargs):
            descriptor = int(os.environ.pop('FORGE_STARTUP_FD'))
            os.write(descriptor, b'agent_started\n'); os.close(descriptor)
            exec(compile("raise ImportError('application test failed')", '/work/repository/tests/test_app.py','exec'), {})
        try:
            with patch.dict(os.environ, {'FORGE_STARTUP_FD':str(write_fd)}), patch.object(startup.runpy,'run_path',side_effect=run):
                with self.assertRaises(ImportError): startup.main()
            self.assertEqual(os.read(read_fd,4096), b'agent_started\n')
        finally:
            os.close(read_fd)

    def test_real_process_waits_for_parent_latch_before_provider_and_cannot_forge_late_proof(self):
        read_fd, write_fd = os.pipe()
        ack_read, ack_write = os.pipe()
        channel = entrypoint.StartupChannel(read_fd, ack_write)
        self.addCleanup(channel.close)
        harness = str(Path(__file__).parents[1] / 'forge/factory/harness.py')
        script = """
import os, runpy
scope=runpy.run_path(%r)
env=dict(os.environ)
print('waiting',flush=True)
scope['begin_agent'](env)
assert 'FORGE_STARTUP_FD' not in os.environ and 'FORGE_STARTUP_ACK_FD' not in os.environ
assert 'FORGE_STARTUP_FD' not in env and 'FORGE_STARTUP_ACK_FD' not in env
try:
    os.write(%d,b'forged startup proof')
except OSError:
    pass
else:
    raise AssertionError('proof descriptor remains open')
print('agent',flush=True)
raise SystemExit(78)
""" % (harness, write_fd)
        child = subprocess.Popen([sys.executable,'-I','-c',script],
            env={**os.environ,'FORGE_STARTUP_FD':str(write_fd),'FORGE_STARTUP_ACK_FD':str(ack_read)},
            pass_fds=(write_fd,ack_read),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
        os.close(write_fd); os.close(ack_read)
        try:
            self.assertEqual(child.stdout.readline(),b'waiting\n')
            self.assertFalse(select.select([child.stdout],[],[],0.05)[0])
            self.assertIsNone(child.poll())
            channel.read(2)
            self.assertTrue(channel.agent_started)
            self.assertIsNone(channel.read_fd); self.assertIsNone(channel.ack_fd)
            stdout,stderr=child.communicate(timeout=3)
            self.assertEqual(stdout,b'agent\n',stderr.decode())
            self.assertEqual(channel.completed(child.returncode,'job'),(1,None))
        finally:
            if child.poll() is None: child.kill()
            child.wait(); child.stdout.close(); child.stderr.close()

    def test_pre_agent_receipt_survives_live_pipe_and_closes_ack_without_admission(self):
        read_fd,write_fd=os.pipe(); ack_read,ack_write=os.pipe()
        channel=entrypoint.StartupChannel(read_fd,ack_write)
        value=receipt('job'); raw=json.dumps({k:v for k,v in value.items() if k!='attempt'}).encode()
        try:
            os.write(write_fd,raw); os.close(write_fd)
            channel.read(); channel.read()
            self.assertFalse(channel.agent_started)
            self.assertEqual(os.read(ack_read,1),b'')
            self.assertEqual(channel.completed(1,'job'),(78,value))
        finally:
            channel.close(); os.close(ack_read)


class StartupResultTests(unittest.TestCase):
    setUp = fixtures.ResultControllerTests.setUp
    view = fixtures.ResultControllerTests.view
    command = fixtures.ResultControllerTests.command
    reserve = fixtures.ResultControllerTests.reserve
    capture = fixtures.ResultControllerTests.capture
    charges = fixtures.ResultControllerTests.charges

    def launch(self):
        job = self.reserve()
        self.life.progress(self.command(), job, execution_fence=self.queries.job(job)['execution_fence'], phase='running', container_id='container')
        return job

    def evidence(self, job):
        return {'forge_startup':receipt(job), 'execution': {'id':'container','image_id':IMAGE,'startup_protocol':'1','exit_code':78,'running':False,'oom_killed':False}}

    def test_forge_fault_consumes_once_without_budget_charge_or_remote_reads(self):
        job = self.launch(); self.capture(job, **self.evidence(job))
        self.github.issue_error = True
        outcome = self.controller.step(job)
        self.assertTrue(outcome['accepted']); self.assertFalse(outcome['charged'])
        self.assertEqual(self.controller.step(job)['outcome'], 'deferred')
        self.assertEqual(self.charges(), 0)
        with self.store.read_snapshot() as db:
            gates = list(db.execute("SELECT code,details_json FROM unit_gates WHERE resolved_at IS NULL"))
            self.assertEqual([g['code'] for g in gates], ['forge_problem'])
            details = json.loads(gates[0]['details_json'])
            self.assertEqual(details['owner'], 'forge'); self.assertEqual(details['image_id'], IMAGE)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM attempt_failures').fetchone()[0], 0)
        issue = Queries(self.store).issue('i')
        self.assertEqual(issue['lifecycle']['reason_code'], 'forge_problem')
        from forge.factory.status_projection import operator_status
        status = operator_status(issue)
        self.assertEqual(status['code'], 'attention'); self.assertEqual(status['owner'], 'forge')
        self.assertIn('No issue attempt', status['reason'])
        from forge.factory.config import validate
        public=PipelineViews(self.store,runtime=validate({}),clock=lambda:100).issues(key='i')['issue']
        self.assertEqual(public['operator_status'],status)
        self.assertEqual(public['forge_problem']['job_id'],job)
        self.assertEqual(public['summary']['headline'],'Forge needs a repair')

    def test_cleanup_and_stale_assignment_keep_existing_fences(self):
        job = self.launch(); self.capture(job, cleanup=False, **self.evidence(job))
        self.assertEqual(self.controller.step(job)['pending'], 'cleanup')
        self.life.verify_cleanup(self.command(), job, execution_fence=self.queries.job(job)['execution_fence'], evidence={'process_stopped':True,'resources_released':True})
        self.life.pause(self.command())
        result = self.controller.step(job)
        self.assertFalse(result['accepted']); self.assertFalse(result['charged'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE code='forge_problem'").fetchone()[0], 0)

    def test_untrusted_or_legacy_capture_still_uses_ordinary_failure_path(self):
        for field, value in [('startup_protocol',None),('exit_code',1),('running',True),('oom_killed',True),('id','other')]:
            job = {'id':'job','container_id':'container'}
            capture = self.evidence('job'); capture['execution'][field] = value
            self.assertIsNone(problem(job,capture))
        job = self.launch()
        self.capture(job, logs='ImportError: application tests failed', execution={'exit_code':1})
        self.assertTrue(self.controller.step(job)['charged']); self.assertEqual(self.charges(),1)

class GroupedStartupTests(unittest.TestCase):
    def test_concurrent_task_faults_share_gate_but_preserve_both_immutable_results(self):
        from tests import test_delivery_pipeline as delivery
        fixture=delivery.DeliveryPipelineTests('runTest')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        f=fixture
        parent=f.dispatch.step('batch')['job_id']
        members=f.jobs.job(parent)['members']
        tasks=[dict(key='task-'+str(index),executor='child',member_ids=[pin['member_id']],
                    scope='Implement member',depends_on=[],internalizes=[]) for index,pin in enumerate(members)]
        self.assertTrue(f.capture(parent,outcome='delegate',delegation_plan=f.plan_value(tasks))['accepted'])
        jobs=[f.dispatch.step('batch')['job_id'],f.dispatch.step('batch')['job_id']]
        for identifier in jobs:
            job=f.jobs.job(identifier)
            f.executions.progress(f.command(),identifier,execution_fence=job['execution_fence'],phase='running',container_id=identifier)
        for identifier in jobs:
            job=f.jobs.job(identifier)
            capture={'forge_startup':receipt(identifier),'execution':{'id':identifier,'image_id':IMAGE,
                'startup_protocol':'1','exit_code':78,'running':False,'oom_killed':False}}
            f.executions.collect(f.command(),identifier,execution_fence=job['execution_fence'],capture=capture)
            f.life.verify_cleanup(f.command(),identifier,execution_fence=job['execution_fence'],
                                  evidence={'process_stopped':True,'resources_released':True})
        outcomes=[f.results.step(identifier) for identifier in jobs]
        self.assertTrue(all(row['accepted'] and not row['charged'] for row in outcomes))
        self.assertEqual(outcomes[0]['gate_id'],outcomes[1]['gate_id'])
        for identifier in jobs:
            self.assertEqual(f.results.step(identifier)['outcome'],'deferred')
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='forge_problem' AND resolved_at IS NULL").fetchone()[0],1)
            self.assertEqual({row[0] for row in db.execute('SELECT job_id FROM forge_problem_jobs')},set(jobs))
            self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM attempt_failures').fetchone()[0],0)
        for identifier in jobs:
            self.assertEqual(f.results.queries.capture(identifier)['forge_startup'],receipt(identifier))


class RunnerStartupCaptureTests(unittest.TestCase):
    def test_capture_requires_exact_entrypoint_exit_and_protocol(self):
        from forge.factory.runner import ContainerRunner, RunnerError
        runner = object.__new__(ContainerRunner)
        value = receipt('job')
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w') as tar:
            raw=json.dumps(value).encode(); item=tarfile.TarInfo('forge-startup.json'); item.size=len(raw)
            tar.addfile(item,io.BytesIO(raw))
        def call(argv, **kwargs):
            if argv[0]=='logs': return SimpleNamespace(stdout=b'ImportError: never authoritative')
            if argv[1].endswith('/forge-startup.json'): return SimpleNamespace(stdout=archive.getvalue())
            raise RunnerError('missing')
        runner.call=call; runner._scrub_values=lambda job:[]
        current={'id':'container','startup_protocol':'1','exit_code':78,'running':False,'oom_killed':False,'image_id':IMAGE}
        runner.inspect=lambda job:current
        captured=runner.capture({'id':'job','role':'build'})
        self.assertEqual(captured['forge_startup'],value)
        current['startup_protocol']=None
        self.assertNotIn('forge_startup',runner.capture({'id':'job','role':'build'}))


class StartupMigrationTests(unittest.TestCase):
    def test_schema34_preserves_existing_gates_and_registers_attention(self):
        import sqlite3
        from forge.store.remote_time import register
        from forge.factory.issue_selection import IssueSelection
        db=sqlite3.connect(':memory:'); self.addCleanup(db.close); register(db)
        schemas=Path(__file__).parents[1]/'forge/store/schema'
        for schema in sorted(schemas.glob('[0-9]*.sql')):
            if int(schema.name.split('_')[0])<=33: db.executescript(schema.read_text())
        db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'owner/repo')")
        db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES('u',1,'main','building',1,1)")
        db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES('i',1,1,'open','r',1)")
        db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES('r','i','Issue','','c','r','{}',1)")
        db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) VALUES('m','u','i',1,'main','r','pending',1)")
        db.execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('hold','u','unit','label_hold',1)")
        before=list(db.execute('SELECT * FROM unit_gates'))
        db.executescript((schemas/'034_forge_problem.sql').read_text())
        self.assertEqual(list(db.execute('SELECT * FROM unit_gates')),before)
        db.execute("INSERT INTO unit_gates(id,unit_id,subject_key,code,created_at) VALUES('forge','u','unit','forge_problem',2)")
        self.assertEqual(IssueSelection(db).execute("SELECT reason_code FROM public_issue_index WHERE id='i'").fetchone()[0],'label_hold')
