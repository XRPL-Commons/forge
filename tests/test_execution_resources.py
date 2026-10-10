"""Exact-container, bounded engine sampling and durable numeric provenance."""
from contextlib import contextmanager
import json
import math
import socket
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from forge.factory import resource_usage as engine
from forge.factory.runner import ContainerRunner
from forge.factory.config import validate
from forge.store.resource_usage import read_in, wall_usage
from forge.store.models import TransitionError
from forge.store.artifacts import Artifacts
from tests import test_store_executions as fixtures

CID = 'a'*64
READ = '2026-10-10T01:02:03.000000000Z'


def observed(cpu=2.5, memory=4096):
    return dict(checked_at=200, status='available', reason=None,
                sample=dict(sampled_at=190, cpu_seconds=cpu, memory_bytes=memory, provenance='docker_engine_stats'))


@contextmanager
def engine_stub(*, slow=False):
    """Real local HTTP/Unix transport, with an independent daemon-side clock."""
    with tempfile.TemporaryDirectory(prefix='forge-stats-',dir='/tmp') as directory:
        path=directory+'/engine.sock'
        listener=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
        try:
            listener.bind(path);listener.listen(2);listener.settimeout(0.1)
        except OSError:
            listener.close();raise
        stop=threading.Event();requests=[]
        def serve():
            try:
                while len(requests)<2 and not stop.is_set():
                    try:connection,_=listener.accept()
                    except TimeoutError:continue
                    with connection:
                        connection.settimeout(1)
                        with connection.makefile('rb') as stream:
                            line=stream.readline()
                            requests.append(line.decode().strip())
                            while stream.readline() not in (b'\r\n',b''):pass
                        if len(requests)==1:
                            body=json.dumps({'ApiVersion':'1.51','MinAPIVersion':'1.44'}).encode()
                            if slow:stop.wait(0.04)
                        else:
                            body=json.dumps({'read':READ,'cpu_stats':{'cpu_usage':{'total_usage':2500000000}},
                                             'memory_stats':{'usage':8192}}).encode()
                        header=f'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode()
                        connection.sendall(header)
                        if slow and len(requests)==2:
                            # Each byte arrives sooner than the socket's initial
                            # timeout, but the whole response exceeds the budget.
                            for byte in body:
                                if stop.wait(0.01):break
                                connection.sendall(bytes([byte]))
                        else:connection.sendall(body)
            except OSError:
                # A timed-out client closes its exact connection; stopping the
                # fixture also wakes a listener with no more requests.
                pass
        thread=threading.Thread(target=serve,daemon=True);thread.start()
        try:yield path,requests
        finally:
            stop.set();listener.close();thread.join(timeout=2)
            if thread.is_alive():raise AssertionError('HTTP fixture did not stop')


class EngineResourceTests(unittest.TestCase):
    def sample(self, stats, version=None):
        responses=[SimpleNamespace(status=200,read=Mock(return_value=json.dumps(value).encode()))
                   for value in (version or {'ApiVersion':'1.54','MinAPIVersion':'1.44'},stats)]
        connection=Mock();connection.getresponse.side_effect=responses
        with patch.object(engine,'_EngineConnection',return_value=connection) as constructor:
            value=engine.stats('/private/specific-engine.sock',CID)
        return value,constructor,connection

    def test_raw_counters_use_negotiated_engine_and_accept_missing_one_shot_id(self):
        value,constructor,connection=self.sample({'read':READ,'cpu_stats':{'cpu_usage':{'total_usage':2500000000}},
            'memory_stats':{'usage':8192,'stats':{'inactive_file':4096},'limit':1000000,'max_usage':99999}})
        self.assertEqual(constructor.call_args.args[0],'/private/specific-engine.sock')
        self.assertEqual([call.args[1] for call in connection.request.call_args_list],
            ['/version',f'/v1.51/containers/{CID}/stats?stream=false&one-shot=true'])
        self.assertEqual(value['sample'],dict(sampled_at=engine.engine_time(READ),cpu_seconds=2.5,memory_bytes=8192,provenance='docker_engine_stats'))
        self.assertNotIn('peak',json.dumps(value))
        connection.close.assert_called_once()

    def test_absent_stopped_counters_and_wrong_container_are_unavailable(self):
        for stats in ({'id':CID,'name':'container'}, {'id':'b'*64,'read':READ,'memory_stats':{'usage':2}},
                      {'read':'0001-01-01T00:00:00Z','cpu_stats':{'cpu_usage':{'total_usage':0}}}):
            value,_,_=self.sample(stats)
            self.assertEqual(value['status'],'unavailable');self.assertIsNone(value['sample'])

    def test_valid_zero_and_partial_metrics_are_distinct_from_missing(self):
        value,_,_=self.sample({'read':READ,'memory_stats':{'usage':0}})
        self.assertEqual(value['status'],'partial')
        self.assertIsNone(value['sample']['cpu_seconds'])
        self.assertEqual(value['sample']['memory_bytes'],0)
        value,_,_=self.sample({'read':READ,'cpu_stats':{'cpu_usage':{'total_usage':True}},'memory_stats':{'usage':-1}})
        self.assertIsNone(value['sample'])

    def test_transport_bounds_and_failures_do_not_fail_inspection(self):
        with patch.object(engine,'_EngineConnection') as constructor:
            constructor.return_value.request.side_effect=TimeoutError()
            self.assertEqual(engine.stats('/specific',CID,deadline=0)['status'],'unavailable')
            self.assertEqual(constructor.call_args.args,('/specific',0))
        for version in ({'ApiVersion':'1.40','MinAPIVersion':'1.24'}, {'ApiVersion':'1.55','MinAPIVersion':'1.52'}):
            value,_,connection=self.sample({},version)
            self.assertEqual(value['reason'],'engine_api_unsupported')
            self.assertEqual(connection.request.call_count,1)
        connection=Mock();connection.getresponse.return_value=SimpleNamespace(status=200,read=lambda size:b'x'*size)
        with patch.object(engine,'_EngineConnection',return_value=connection):
            self.assertEqual(engine.stats('/specific',CID)['status'],'unavailable')
        with patch.object(engine.time,'monotonic',return_value=5):
            with self.assertRaises(TimeoutError):engine._DeadlineSocket(4)

    def test_verified_inspection_requests_optional_stats_only_from_configured_daemon(self):
        import uuid
        runner=ContainerRunner(validate({}),run=Mock(),environ={'DOCKER_HOST':'tcp://wrong-host:2375'})
        job=dict(id=str(uuid.uuid4()),slot='build-1')
        name=runner.name(job)
        state=dict(Status='running',ExitCode=0,Running=True,StartedAt=READ,FinishedAt='0001-01-01T00:00:00Z')
        value=dict(Name='/'+name,Id=CID,Config={'Labels':{'forge.execution':job['id'],'forge.slot':job['slot']}},State=state)
        def responses():
            return [Mock(stdout=json.dumps({'Names':name,'ID':CID}).encode()),Mock(stdout=json.dumps([value]).encode())]
        runner.call=Mock(side_effect=responses())
        with patch.object(engine,'stats') as stats:
            runner.inspect(job)
            stats.assert_not_called()
        unavailable=dict(checked_at=200,status='unavailable',reason='engine_stats_unavailable',sample=None)
        runner.call=Mock(side_effect=responses())
        with patch.object(engine,'stats',return_value=unavailable) as stats:
            deadline=engine.time.monotonic()+30
            current=runner.inspect(job,resources=True,deadline=deadline)
            self.assertTrue(current['running'])
            self.assertEqual(current['resources'],unavailable)
            self.assertIsNone(current['finished_at'])
            stats.assert_called_once_with(runner.config['engine_socket'],CID,deadline=deadline)
        value['Config']['Labels']['forge.execution']='wrong-owner'
        runner.call=Mock(side_effect=responses())
        from forge.factory.runner import RunnerError
        with patch.object(engine,'stats') as stats, self.assertRaises(RunnerError):
            runner.inspect(job,resources=True)
        stats.assert_not_called()

    def test_real_unix_http_version_and_stats_connections(self):
        with engine_stub() as (path,requests):
            result=engine.stats(path,CID)
            self.assertEqual(result['status'],'available')
            self.assertEqual(result['sample']['cpu_seconds'],2.5)
            self.assertEqual(result['sample']['memory_bytes'],8192)
            self.assertEqual(requests,['GET /version HTTP/1.1',
                f'GET /v1.51/containers/{CID}/stats?stream=false&one-shot=true HTTP/1.1'])

    def test_real_unix_http_slow_reads_share_version_and_stats_deadline(self):
        with engine_stub(slow=True) as (path,requests), patch.object(engine,'STATS_TIMEOUT',0.09):
            started=time.monotonic();result=engine.stats(path,CID);elapsed=time.monotonic()-started
            self.assertEqual(len(requests),2)
            self.assertEqual(result['status'],'unavailable')
            self.assertIsNone(result['sample'])
            self.assertGreaterEqual(elapsed,0.06)
            self.assertLess(elapsed,0.5)

    def test_wall_time_requires_verified_finished_timestamps(self):
        self.assertEqual(wall_usage(dict(status='exited',running=False,started_at=5,finished_at=8))['seconds'],3)
        for execution in (None,dict(status='running',running=True,started_at=5,finished_at=8),
                          dict(status='exited',running=False,started_at=8,finished_at=5),
                          dict(status='exited',running=False,started_at=float('nan'),finished_at=8)):
            self.assertIsNone(wall_usage(execution)['seconds'])
        self.assertIsNone(engine.engine_time('2026-10-10T01:02:03'))


class ResourcePersistenceTests(unittest.TestCase):
    setUp=fixtures.ExecutionTests.setUp
    driver=fixtures.ExecutionTests.driver
    reserve=fixtures.ExecutionTests.reserve
    command=fixtures.ExecutionTests.command
    view=fixtures.ExecutionTests.view

    def execution(self,job,resources=None,**extra):
        current=dict(id=CID,status='running',running=True,started_at=150,**extra)
        if resources is not None:current['resources']=resources
        self.life.observe(job,execution_fence='fence',observation=current)
        return current

    def test_restart_failure_and_capture_preserve_original_sample_and_lazy_public_receipt(self):
        job=self.reserve();self.execution(job,observed())
        failed=dict(checked_at=210,status='unavailable',reason='engine_stats_unavailable',sample=None)
        current=self.execution(job,failed)
        current.update(status='exited',running=False,finished_at=205)
        capture=dict(execution=current,result={'summary':'worker report'},logs='private log')
        command=self.command()
        receipt=self.life.collect(command,job,execution_fence='fence',capture=capture)
        self.assertEqual(self.life.collect(command,job,execution_fence='fence',capture=capture),receipt)
        from forge.factory.store_views import PipelineViews
        # Resource reads must not hydrate the immutable result or transcript.
        with patch.object(Artifacts,'read',side_effect=AssertionError('artifact hydration')):
            public=PipelineViews(self.store,runtime=validate({})).job(job)['resource_usage']
        self.assertEqual(public['wall']['seconds'],55)
        self.assertEqual(public['sample'],observed()['sample'])
        self.assertEqual(public['collection'],{k:failed[k] for k in ('checked_at','status','reason')})
        self.assertEqual(json.loads(Artifacts(self.store).read(job,'worker_capture').content),{k:v for k,v in capture.items() if k!='logs'})
        from forge.store.db import Store
        with Store(self.store.root).read_snapshot() as db:
            self.assertEqual(read_in(db,job),public)
        # Later observations cannot rewrite a captured receipt.
        self.execution(job,observed(cpu=9,memory=3000))
        with self.store.read_snapshot() as db:self.assertEqual(read_in(db,job),public)

    def test_unverified_capture_time_is_unavailable_without_changing_result_acceptance(self):
        job=self.reserve();self.execution(job,observed())
        capture=dict(execution=dict(exit_code=0,started_at=1,finished_at=100,status='exited',running=False),
                     result={'summary':'legacy adapter capture'},logs='')
        self.life.collect(self.command(),job,execution_fence='fence',capture=capture)
        with self.store.read_snapshot() as db:
            usage=read_in(db,job)
            self.assertIsNone(usage['wall']['seconds'])
            self.assertEqual(usage['wall']['unavailable_reason'],'container_identity_unavailable')
            self.assertEqual(usage['sample'],observed()['sample'])

    def test_wrong_container_invalid_counter_and_no_measurement_do_not_invent_values(self):
        job=self.reserve();self.execution(job)
        with self.store.read_snapshot() as db:
            value=read_in(db,job);self.assertIsNone(value['sample']);self.assertIsNone(value['wall']['seconds'])
        for bad in (observed(cpu=float('inf')),observed(memory=True)):
            with self.assertRaises(TransitionError):self.execution(job,bad)
        with self.assertRaises(TransitionError):
            self.life.observe(job,execution_fence='fence',observation=dict(id='b'*64,status='running',resources=observed()))
        with self.store.read_snapshot() as db:self.assertIsNone(read_in(db,job)['sample'])

class ResourceMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_records_without_manufacturing_historical_usage(self):
        import tempfile
        from pathlib import Path
        from forge.store import db as storage, upgrade
        from tests.historical_store_fixtures import reservation
        with tempfile.TemporaryDirectory() as temporary:
            catalog=storage._migrations()
            with patch('forge.store.db._migrations',return_value=catalog[:31]):
                store=storage.Store.create(Path(temporary)/'store')
                job_id=reservation(store,config={},policy={},base_oid='base')
                with store.read_snapshot() as db:
                    tables=[row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT IN ('schema_migrations','store_metadata')")]
                    before={table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in tables}
            # This migration is additive; later topology migrations intentionally
            # rebuild existing records and have their own populated parity tests.
            with patch('forge.store.db._migrations',return_value=catalog[:32]):
                store=storage.Store(store.root)
            self.assertEqual(upgrade.apply(store)['current_schema'],32)
            upgrade.apply(store)
            with store.read_snapshot() as db:
                self.assertEqual(before,{table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in tables})
                self.assertEqual(db.execute('SELECT COUNT(*) FROM job_resource_observations').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM job_resource_receipts').fetchone()[0],0)
                self.assertIsNone(read_in(db,job_id)['sample'])
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
            # Later migrations must not turn absence of historical measurements
            # into a zero-valued sample or lose the physical job identity.
            current=storage.Store(store.root)
            upgrade.apply(current)
            with current.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM job_resource_observations').fetchone()[0],0)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM job_resource_receipts').fetchone()[0],0)
                self.assertIsNone(read_in(db,job_id)['sample'])
                self.assertEqual(db.execute('SELECT job_id FROM job_launches').fetchone()[0],job_id)
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
