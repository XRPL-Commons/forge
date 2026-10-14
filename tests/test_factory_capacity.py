import json
import tempfile
import threading
import unittest
import uuid
from unittest.mock import Mock, patch

from forge.factory.capacity import GIB, observe, launch_lock, parse_meminfo, parse_pressure
from forge.factory.config import validate
from forge.factory.runner import ContainerRunner, RunnerCapacity, RunnerError


def telemetry(total=16, available=14, some=0, full=0):
    return {'/proc/meminfo': f'MemTotal: {total*1024**2} kB\nMemAvailable: {available*1024**2} kB\n',
            '/proc/pressure/memory': f'some avg10={some} avg60=0 avg300=0 total=0\nfull avg10={full} avg60=0 avg300=0 total=0\n'}


def container(limit=4*GIB):
    return {'HostConfig': {'Memory': limit}}


class TelemetryParsingTests(unittest.TestCase):
    MEMORY = 'MemTotal: 8388608 kB\nMemAvailable: 5242880 kB\n'
    PSI = 'some avg10=0.00 avg60=0.00 avg300=0.00 total=10\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n'

    def test_well_formed_telemetry_is_parsed(self):
        self.assertEqual(parse_meminfo(self.MEMORY), {'MemTotal': 8 * GIB, 'MemAvailable': 5 * GIB})
        self.assertEqual(parse_pressure(self.PSI)['some']['total'], 10)

    def test_duplicate_or_inconsistent_fields_are_rejected(self):
        for bad in (self.MEMORY + 'MemAvailable: 3 kB\n', 'MemTotal: 1 kB\nMemAvailable: 2 kB\n', 'MemTotal: 1 kB\n'):
            with self.assertRaises(ValueError):
                parse_meminfo(bad)
        for bad in (self.PSI.replace('total=10', 'total=10 avg10=0'), self.PSI.replace('0.00', '101', 1),
                    self.PSI.splitlines()[0], self.PSI.replace('total=1', 'total=-1')):
            with self.assertRaises(ValueError):
                parse_pressure(bad)


class MemoryAdmissionTests(unittest.TestCase):
    def test_full_budgets_prevent_spending_unused_ram_twice(self):
        host = telemetry()
        self.assertTrue(observe(4*GIB, [container(), container()], read_text=host.__getitem__)['allowed'])
        result = observe(4*GIB, [container(), container(), container()], read_text=host.__getitem__)
        self.assertEqual(result['reason'], 'memory_budget')
        self.assertFalse(result['allowed'])
        self.assertEqual(observe(4*GIB, [container()], read_text=telemetry(8, 7).__getitem__)['reason'], 'memory_budget')

    def test_host_headroom_and_pressure_can_postpone_then_resume(self):
        for host, reason in [(telemetry(16, 5), 'memory_headroom'),
                             (telemetry(some=1.1), 'memory_pressure'),
                             (telemetry(full=.2), 'memory_pressure')]:
            self.assertEqual(observe(4*GIB, [], read_text=host.__getitem__)['reason'], reason)
        self.assertTrue(observe(4*GIB, [], read_text=telemetry(16, 6).__getitem__)['allowed'])

    def test_missing_or_malformed_telemetry_and_unlimited_workers_fail_closed(self):
        for read in [Mock(side_effect=OSError()), lambda path: 'invalid']:
            self.assertEqual(observe(4*GIB, [], read_text=read)['reason'], 'telemetry_unavailable')
        for existing in [container(0), {}, container(-1)]:
            self.assertFalse(observe(4*GIB, [existing], read_text=telemetry().__getitem__)['allowed'])
        self.assertFalse(observe(True, [], read_text=telemetry().__getitem__)['allowed'])

    def test_the_retired_manual_total_is_refused(self):
        with self.assertRaisesRegex(ValueError, 'Unknown runtime settings'):
            validate({'max_running': 1})

    def test_check_and_launch_lock_is_shared(self):
        with tempfile.TemporaryDirectory() as directory, patch('forge.factory.capacity.tempfile.gettempdir', return_value=directory):
            entered = threading.Event()
            def contender():
                with launch_lock('/var/run/docker.sock'):
                    entered.set()
            with launch_lock('/var/run/docker.sock'):
                thread = threading.Thread(target=contender)
                thread.start()
                self.assertFalse(entered.wait(.05))
            thread.join(1)
            self.assertTrue(entered.is_set())

    def test_runner_inventories_created_and_orphaned_workers_and_uses_actual_limit(self):
        runner = ContainerRunner(validate({}))
        job = {'id': str(uuid.uuid4()), 'slot': 'build-1', 'fenced': False}
        own = {**container(2*GIB), 'Id': 'a'*64, 'Name': '/'+runner.name(job),
               'Config': {'Labels': {'forge.execution': job['id']}}}
        other = {**container(), 'Id': 'b'*64, 'Name': '/orphan'}
        runner.call = Mock(side_effect=[Mock(stdout=('a'*64+'\n'+'b'*64+'\n').encode()),
                                       Mock(stdout=json.dumps([own, other]).encode())])
        with patch('forge.factory.runner.observe_capacity', wraps=lambda requested, containers, **kw: observe(requested, containers, read_text=telemetry().__getitem__, **kw)):
            result = runner.capacity(job)
        self.assertTrue(result['allowed'])
        self.assertEqual(result['host_reserve_bytes'], 2*GIB)
        self.assertEqual(result['requested_bytes'], 2*GIB)
        self.assertEqual(result['reserved_bytes'], 4*GIB)
        runner.call = Mock(side_effect=RunnerError('Engine unavailable'))
        self.assertFalse(runner.capacity()['allowed'])

    def test_host_reserve_is_a_runtime_setting(self):
        # 3 GiB free on the host: a 2 GiB worker is refused with the 2 GiB default and admitted at 1 GiB.
        host = telemetry(8, 3)
        workers = [container(2*GIB), container(2*GIB)]
        self.assertEqual(observe(2*GIB, workers, read_text=host.__getitem__)['reason'], 'memory_headroom')
        result = observe(2*GIB, workers, reserve=GIB, read_text=host.__getitem__)
        self.assertTrue(result['allowed']); self.assertEqual(result['host_reserve_bytes'], GIB)
        runner = ContainerRunner(validate({'host_reserve': '1536m'}))
        runner.call = Mock(side_effect=[Mock(stdout=b''), Mock(stdout=b'[]')])
        with patch('forge.factory.runner.observe_capacity', wraps=lambda requested, containers, **kw: observe(requested, containers, read_text=host.__getitem__, **kw)):
            self.assertEqual(runner.capacity()['host_reserve_bytes'], int(1.5*GIB))
        with self.assertRaises(ValueError):
            validate({'host_reserve': 'lots'})

    def test_create_and_start_both_recheck_without_starting_on_denial(self):
        runner = ContainerRunner(validate({}))
        job = {'id': str(uuid.uuid4()), 'slot': 'build-1', 'fenced': False}
        runner.inspect = Mock(return_value=None)
        runner.capacity = Mock(return_value={'allowed': False, 'reason': 'memory_headroom'})
        runner.call = Mock()
        with tempfile.TemporaryDirectory() as directory, patch('forge.factory.capacity.tempfile.gettempdir', return_value=directory):
            with self.assertRaises(RunnerCapacity): runner.create(job)
            runner.call.assert_not_called()
            runner.inspect.return_value = {'id': 'a'*64, 'status': 'created'}
            with self.assertRaises(RunnerCapacity): runner.start(job)
            runner.call.assert_not_called()
            runner.capacity.return_value = {'allowed': True}
            runner.start(job)
            runner.call.assert_called_once_with(['start', 'a'*64])
