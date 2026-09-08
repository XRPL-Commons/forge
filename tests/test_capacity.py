import json
from pathlib import Path
from types import SimpleNamespace
import subprocess
import tempfile
import unittest
from unittest import mock

from forge.capacity import Limits, check, observe, parse_meminfo, parse_pressure, read_sessions

MEMORY = 'MemTotal: 8388608 kB\nMemAvailable: 5242880 kB\n'
PSI = 'some avg10=0.00 avg60=0.00 avg300=0.00 total=10\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=1\n'


class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'repos.d'
        self.config.mkdir()
        (self.config / 'one.repo').write_text('REPO="o/one"\nOWNER_LABEL="auto:owned-by-forge"\n')
        (self.config / 'two.repo').write_text('REPO="o/two"\nOWNER_LABEL="auto:owned-by-forge"\nENABLED=false\n')
        self.telemetry = {'/proc/meminfo': MEMORY, '/proc/pressure/memory': PSI}

    def run_tmux(self, names=('personal',), code=0, error=''):
        return lambda *args, **kwargs: SimpleNamespace(returncode=code, stdout='\n'.join(names) + ('\n' if names else ''), stderr=error)

    def decision(self, names=('personal',), session='one-build', **kwargs):
        return observe(self.config, session, run=self.run_tmux(names), read_text=self.telemetry.__getitem__, now=100, **kwargs)

    def test_admit_with_headroom_and_no_fleet_sessions(self):
        value = self.decision(['personal session with spaces'])
        self.assertTrue(value['admitted'])
        self.assertEqual(value['fleet_count'], 0)
        self.assertEqual(value['memory_after_launch_reserve_bytes'], (5120 - 512) * 1024**2)

    def test_all_roles_and_disabled_survivors_consume_slots(self):
        value = self.decision(['one-review', 'two-build', 'two-audit', 'personal', 'one-build-private'])
        self.assertFalse(value['admitted'])
        self.assertEqual(value['reason'], 'fleet_session_limit')
        self.assertEqual(value['fleet_count'], 3)
        self.assertEqual(value['disabled_survivors'], ['two-audit', 'two-build'])
        self.assertNotIn('personal', str(value))
        self.assertNotIn('one-build-private', str(value))

    def test_role_cap_preserves_review_and_audit_progression(self):
        (self.config / 'three.repo').write_text('REPO="o/three"\nOWNER_LABEL="auto:owned-by-forge"\n')
        value = self.decision(['one-build'], session='three-build')
        self.assertEqual(value['reason'], 'role_session_limit')
        self.assertEqual(value['role_counts'], {'build': 1, 'review': 0, 'audit': 0})
        self.assertTrue(self.decision(['one-build'], session='three-review')['admitted'])
        self.assertTrue(self.decision(['one-build', 'three-review'], session='one-audit')['admitted'])
        self.assertEqual(self.decision(['two-build'])['reason'], 'role_session_limit')

    def test_disabled_or_existing_session_never_admitted(self):
        self.assertEqual(self.decision(session='two-build')['reason'], 'repository_disabled')
        self.assertEqual(self.decision(['one-build'])['reason'], 'session_already_exists')
        self.assertEqual(self.decision(session='personal')['reason'], 'session_not_configured')

    def test_reserve_must_leave_two_gib_available(self):
        self.telemetry['/proc/meminfo'] = MEMORY.replace('5242880', '2621439')
        self.assertEqual(self.decision()['reason'], 'memory_headroom')
        self.telemetry['/proc/meminfo'] = MEMORY.replace('5242880', '2621440')
        self.assertTrue(self.decision()['admitted'])

    def test_recent_some_or_full_pressure_blocks(self):
        for old, new in [('some avg10=0.00', 'some avg10=1.01'),
                         ('some avg10=0.00 avg60=0.00', 'some avg10=0.00 avg60=1.01'),
                         ('full avg10=0.00', 'full avg10=0.11')]:
            with self.subTest(new=new):
                self.telemetry['/proc/pressure/memory'] = PSI.replace(old, new)
                self.assertEqual(self.decision()['reason'], 'memory_pressure')

    def test_missing_or_malformed_telemetry_fails_closed(self):
        for path, value in [('/proc/meminfo', 'MemTotal: 1 kB\n'),
                            ('/proc/meminfo', 'MemTotal: 1 kB\nMemAvailable: 2 kB\n'),
                            ('/proc/pressure/memory', PSI.replace('avg10=0.00', 'avg10=nan')),
                            ('/proc/pressure/memory', PSI.splitlines()[0])]:
            with self.subTest(value=value):
                self.telemetry = {'/proc/meminfo': MEMORY, '/proc/pressure/memory': PSI, path: value}
                self.assertEqual(self.decision()['reason'], 'telemetry_unavailable')
        self.telemetry = {}
        self.assertFalse(self.decision()['admitted'])

    def test_only_explicit_server_absence_is_empty(self):
        for message in ['no server running on /tmp/tmux-1000/default',
                        'error connecting to /tmp/tmux-1000/default (No such file or directory)', 'no sessions']:
            names, _ = read_sessions(self.run_tmux([], 1, message))
            self.assertEqual(names, [])
        for message in ['permission denied', 'error connecting to /tmp/socket (Connection refused)', '']:
            with self.subTest(message=message), self.assertRaises(ValueError):
                read_sessions(self.run_tmux([], 1, message))
        with self.assertRaises(ValueError):
            read_sessions(self.run_tmux([], 0, ''))

    def test_tmux_timeout_does_not_admit(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired('tmux', 10)
        value = observe(self.config, 'one-build', run=timeout, read_text=self.telemetry.__getitem__)
        self.assertEqual(value['reason'], 'telemetry_unavailable')

    def test_session_names_only_no_pane_or_process_reads(self):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            return self.run_tmux()(command, **kwargs)
        self.assertTrue(observe(self.config, 'one-build', run=run, read_text=self.telemetry.__getitem__)['admitted'])
        self.assertEqual(calls, [['tmux', 'list-sessions', '-F', '#{session_name}']])

    def test_decisions_persist_and_failed_persistence_denies(self):
        kwargs = dict(run=self.run_tmux(), read_text=self.telemetry.__getitem__, now=100)
        value = check(self.config, self.root / 'state', 'one-build', **kwargs)
        self.assertEqual(json.loads((self.root / 'state/capacity.json').read_text()), value)
        with mock.patch('forge.capacity.atomic_json', side_effect=OSError('disk full')):
            value = check(self.config, self.root / 'state', 'one-build', **kwargs)
        self.assertFalse(value['admitted'])
        self.assertEqual(value['reason'], 'decision_persistence_failed')

    def test_invalid_config_and_limits_fail_closed(self):
        (self.config / 'bad.repo').write_text('touch /tmp/nope\n')
        self.assertFalse(self.decision()['admitted'])
        for args in [{'max_sessions': 0}, {'launch_reserve_mib': -1}, {'max_memory_psi_some': float('nan')}]:
            with self.assertRaises(ValueError):
                Limits(**args)

    def test_duplicate_pressure_fields_and_session_names_rejected(self):
        with self.assertRaises(ValueError):
            parse_pressure(PSI.replace('total=10', 'total=10 avg10=0'))
        with self.assertRaises(ValueError):
            parse_meminfo(MEMORY + 'MemAvailable: 3 kB\n')
        with self.assertRaises(ValueError):
            read_sessions(self.run_tmux(['one-build', 'one-build']))
