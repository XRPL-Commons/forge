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
    def faults(self, *, full_second=False, only_first=False):
        from tests import test_delivery_pipeline as delivery
        fixture=delivery.DeliveryPipelineTests('runTest')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        f=fixture
        parent=f.dispatch.step('batch')['job_id']
        members=f.jobs.job(parent)['members']
        tasks=[dict(key='task-'+str(index),executor='child',member_ids=[pin['member_id']],
                    scope='Implement member',depends_on=[],internalizes=[]) for index,pin in enumerate(members)]
        self.assertTrue(f.capture(parent,outcome='delegate',delegation_plan=f.plan_value(tasks))['accepted'])
        jobs=[f.dispatch.step('batch')['job_id']]
        if full_second:
            from forge.factory.dispatch_controller import PreparedDispatch
            f.dispatch.prepare=lambda snapshot,role: PreparedDispatch(f.base,role+'[bot]',
                {**f.config,'image':'sha256:'+'c'*64,'environment_tier':'full'})
        jobs.append(f.dispatch.step('batch')['job_id'])
        for identifier in jobs:
            job=f.jobs.job(identifier)
            f.executions.progress(f.command(),identifier,execution_fence=job['execution_fence'],phase='running',container_id=identifier)
        failed=jobs[:1] if only_first else jobs
        if only_first:
            commit,bundle=f.child_bundle(2)
            self.assertTrue(f.capture(jobs[1],output_commit=commit,bundle=bundle)['accepted'])
        for identifier in failed:
            job=f.jobs.job(identifier)
            capture={'forge_startup':receipt(identifier),'execution':{'id':identifier,'image_id':IMAGE,
                'startup_protocol':'1','exit_code':78,'running':False,'oom_killed':False}}
            f.executions.collect(f.command(),identifier,execution_fence=job['execution_fence'],capture=capture)
            f.life.verify_cleanup(f.command(),identifier,execution_fence=job['execution_fence'],
                                  evidence={'process_stopped':True,'resources_released':True})
        outcomes=[f.results.step(identifier) for identifier in failed]
        return f,jobs,outcomes

    def test_concurrent_task_faults_share_gate_but_preserve_both_immutable_results(self):
        f,jobs,outcomes=self.faults()
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

    def qualified(self,f, *, full=None):
        from forge.store.configuration import Configuration
        config=Configuration(f.store,clock=lambda:110)
        for domain,value in [('runtime.json',{'image':'sha256:'+'b'*64,
                            'environment_tiers':{'full':{'image':full}} if full else {}}),
                            ('host.json',{'image':'sha256:'+'b'*64,'full_image':full,'updated_at':90})]:
            row=config.get(domain)
            config.replace(domain,value,expected_revision=row['revision'] if row else 0)

    def test_both_standard_and_full_faults_need_their_qualified_replacements_then_dispatch_resumes(self):
        from forge.factory.wait_controller import WaitController
        f,jobs,outcomes=self.faults(full_second=True); gate=outcomes[0]['gate_id']
        waits=WaitController(f.store,clock=lambda:110)
        self.qualified(f,full='sha256:'+'c'*64)
        self.assertIn('pending',waits.step(gate))
        self.qualified(f,full='sha256:'+'d'*64)
        self.assertTrue(waits.step(gate)['resolved'])
        f.config={**f.config,'image':'sha256:'+'b'*64}
        f.dispatch=f.dispatcher()
        self.assertTrue(f.dispatch.step('batch')['reserved'])

    def test_dropped_standard_fault_does_not_block_original_units_full_image_repair(self):
        from forge.factory.wait_controller import WaitController
        from forge.store.configuration import Configuration
        f,jobs,outcomes=self.faults(full_second=True)
        pin=f.jobs.job(jobs[0])['members'][0]['member_id']
        successor=f.delivery.drop_member(f.command(),pin,membership_generation=1,
            dependent_decisions={},evidence={'approved':True})['released']
        config=Configuration(f.store,clock=lambda:110)
        config.replace('runtime.json',{'image':IMAGE,'environment_tiers':{'full':{'image':'sha256:'+'d'*64}}},expected_revision=0)
        config.replace('host.json',{'image':IMAGE,'full_image':'sha256:'+'d'*64,'updated_at':90},expected_revision=0)
        waits=WaitController(f.store,clock=lambda:110)
        self.assertTrue(waits.step(outcomes[0]['gate_id'])['resolved'])
        with f.store.read_snapshot() as db:
            inherited=db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='forge_problem'",(successor['unit_id'],)).fetchone()[0]
        self.assertIn('pending',waits.step(inherited))
        self.qualified(f,full='sha256:'+'d'*64)
        self.assertTrue(waits.step(inherited)['resolved'])

    def test_drop_of_only_affected_member_moves_obligation_off_original_unit(self):
        from forge.factory.wait_controller import WaitController
        f,jobs,outcomes=self.faults(only_first=True)
        pin=f.jobs.job(jobs[0])['members'][0]['member_id']
        successor=f.delivery.drop_member(f.command(),pin,membership_generation=1,
            dependent_decisions={},evidence={'approved':True})['released']
        with f.store.read_snapshot() as db:
            source=db.execute('SELECT resolved_at,resolution FROM unit_gates WHERE id=?',(outcomes[0]['gate_id'],)).fetchone()
            self.assertIsNotNone(source['resolved_at']); self.assertEqual(source['resolution'],'superseded')
            inherited=db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='forge_problem' AND resolved_at IS NULL",(successor['unit_id'],)).fetchone()[0]
            self.assertEqual([r[0] for r in db.execute('SELECT job_id FROM forge_problem_jobs WHERE gate_id=?',(inherited,))],[jobs[0]])
        waits=WaitController(f.store,clock=lambda:110)
        self.assertIn('pending',waits.step(inherited))
        self.qualified(f)
        self.assertTrue(waits.step(inherited)['resolved'])

    def test_dissolve_and_drop_keep_only_scoped_faults_and_their_repair_exit(self):
        from forge.factory.wait_controller import WaitController
        for operation in ('dissolve','drop'):
            with self.subTest(operation=operation):
                f,jobs,outcomes=self.faults()
                pins={job:f.jobs.job(job)['members'][0]['member_id'] for job in jobs}
                f.life.pause(f.command())
                if operation=='dissolve':
                    units=f.life.dissolve(f.command(),evidence={'approved':True})['released']
                else:
                    released=f.delivery.drop_member(f.command(),pins[jobs[0]],membership_generation=1,
                        dependent_decisions={},evidence={'approved':True})['released']
                    units=[released,{'unit_id':'batch','member_id':pins[jobs[1]]}]
                with f.store.read_snapshot() as db:
                    gates=[dict(row) for row in db.execute("SELECT g.id,g.unit_id FROM unit_gates g JOIN units u ON u.id=g.unit_id WHERE g.code='forge_problem' AND g.resolved_at IS NULL AND u.state!='dissolved'")]
                    self.assertEqual(len(gates),2)
                    for unit in units:
                        linked={row[0] for row in db.execute('SELECT f.job_id FROM forge_problem_jobs f JOIN unit_gates g ON g.id=f.gate_id WHERE g.unit_id=? AND g.resolved_at IS NULL AND f.member_id=?',(unit['unit_id'],unit['member_id']))}
                        issue=db.execute('SELECT issue_id FROM unit_issues WHERE id=?',(unit['member_id'],)).fetchone()[0]
                        self.assertEqual(linked,{job for job in jobs if db.execute('SELECT issue_id FROM unit_issues WHERE id=?',(pins[job],)).fetchone()[0]==issue})
                self.qualified(f)
                waits=WaitController(f.store,clock=lambda:110)
                for gate in gates: self.assertTrue(waits.step(gate['id'])['resolved'])
                with f.store.read_snapshot() as db:
                    for unit in units:
                        self.assertEqual([r[0] for r in db.execute('SELECT code FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL',(unit['unit_id'],))],['operator_pause'])
                    self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0],0)


class StartupRepairTests(unittest.TestCase):
    setUp=fixtures.ResultControllerTests.setUp
    view=fixtures.ResultControllerTests.view
    command=fixtures.ResultControllerTests.command
    capture=fixtures.ResultControllerTests.capture
    charges=fixtures.ResultControllerTests.charges
    launch=StartupResultTests.launch
    evidence=StartupResultTests.evidence

    def reserve(self):
        from forge.store.executions import Launch
        return self.life.reserve_job(self.command(),'build',slot='slot',execution_fence='fence-'+str(self.serial),
            input_manifest=self.manifest,member_id=self.member,
            launch=Launch('forge/issue-1','builder[bot]','codex',200,{'image':IMAGE},self.policy))['job_id']

    def save(self,domain,settings):
        from forge.store.configuration import Configuration
        config=Configuration(self.store,clock=lambda:101)
        row=config.get(domain)
        config.replace(domain,settings,expected_revision=row['revision'] if row else 0)

    def fault(self):
        identifier=self.launch(); self.capture(identifier,**self.evidence(identifier))
        return self.controller.step(identifier)['gate_id']

    def test_only_qualified_changed_image_releases_and_late_capture_does_not_strand(self):
        from forge.factory.wait_controller import WaitController
        gate=self.fault(); waits=WaitController(self.store,clock=lambda:110)
        version=self.view()['version']
        for _ in range(2): self.assertEqual(waits.step(gate)['pending'],'qualified_runner_update')
        self.assertEqual(self.view()['version'],version)
        self.save('runtime.json',{'image':IMAGE})
        self.save('host.json',{'image':IMAGE,'updated_at':105})
        self.assertIn('pending',waits.step(gate))
        new='sha256:'+'b'*64
        self.save('runtime.json',{'image':new})
        self.assertIn('pending',waits.step(gate))  # configuration alone is not qualification
        self.life.open_gate(self.command(),'label_hold',details={'label':'hold'})
        self.save('host.json',{'image':new,'updated_at':90})  # qualified before the old job's late capture
        self.save('runtime.json',{'image':new,'environment_tiers':{'standard':{'image':IMAGE}}})
        self.assertIn('pending',waits.step(gate))  # explicit standard image wins over runtime default
        self.save('runtime.json',{'image':new})
        self.assertTrue(waits.cycle()['items'][0]['resolved'])
        self.assertTrue(waits.step(gate)['resolved'])
        with self.store.read_snapshot() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT code FROM unit_gates WHERE resolved_at IS NULL')],['label_hold'])
            self.assertEqual(db.execute('SELECT count(*) FROM forge_problem_jobs').fetchone()[0],1)
        self.assertEqual(self.charges(),0)

    def test_writer_rechecks_configuration_and_generic_resolution_cannot_bypass(self):
        from forge.store.forge_problems import ForgeProblems
        from forge.store.models import TransitionError,VersionConflict
        gate=self.fault(); new='sha256:'+'b'*64
        self.save('runtime.json',{'image':new}); self.save('host.json',{'image':new,'updated_at':105})
        with self.assertRaises(TransitionError):
            self.life.resolve_gate(self.command(),gate,release='observation',evidence={'verified':True,'observed_at':110})
        commands=ForgeProblems(self.store,clock=lambda:110); apply=commands._apply
        def raced(*args,**kwargs):
            self.save('runtime.json',{'image':IMAGE})
            return apply(*args,**kwargs)
        with patch.object(commands,'_apply',side_effect=raced),self.assertRaises(VersionConflict):
            commands.recheck(self.command(),gate)
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(gate,)).fetchone()[0])


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
