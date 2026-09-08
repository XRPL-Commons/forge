import subprocess
from types import SimpleNamespace
import unittest
from unittest import mock

from forge.executor import AttemptPlan, Receipt, SystemdExecutor, ExecutorError, command_plan, SHOW_FIELDS, verify_preconditions

ID = '00000000-0000-4000-8000-000000000001'
INVOCATION = 'a' * 32


def response(plan, *, absent=False, active='active', invocation=INVOCATION, cgroup=None, **extra):
    values = dict(Id=plan.unit, LoadState='not-found' if absent else 'loaded',
                  ActiveState='inactive' if absent else active, SubState='dead' if absent else 'running',
                  ControlGroup='' if absent else (plan.cgroup if cgroup is None else cgroup),
                  InvocationID='' if absent else invocation, User='' if absent else plan.user,
                  Group='' if absent else plan.user, ExecMainCode='0', ExecMainStatus='0')
    values.update(extra)
    return SimpleNamespace(returncode=0, stdout='\n'.join(name + '=' + values[name] for name in SHOW_FIELDS), stderr='')


class ExecutorTests(unittest.TestCase):
    def setUp(self):
        self.plan = AttemptPlan('codex', ID)
        self.receipt = Receipt('codex', ID, self.plan.unit, INVOCATION, self.plan.cgroup)

    def test_reject_noncanonical_provider_uuid_or_path_injection(self):
        for provider, identity in [('codex;touch /tmp/a', ID), ('codex', '../../elsewhere'),
                                   ('codex', ID.upper().replace('8000', 'A000')), ('claude', '0'*32)]:
            with self.subTest(provider=provider, identity=identity), self.assertRaises(ValueError):
                AttemptPlan(provider, identity)

    def test_dry_run_has_no_process_or_filesystem_side_effects(self):
        run = mock.Mock(side_effect=AssertionError('unexpected execution'))
        executor = SystemdExecutor(run)
        self.assertTrue(executor.start(self.plan)['dry_run'])
        self.assertTrue(executor.start_synthetic(self.plan)['dry_run'])
        self.assertTrue(executor.cancel(self.plan, self.receipt)['dry_run'])
        run.assert_not_called()

    def test_real_cli_activation_is_always_refused(self):
        with self.assertRaisesRegex(ExecutorError, 'disabled'):
            SystemdExecutor().start(self.plan, apply=True)

    def test_commands_properties_and_provider_policy_paths_are_fixed(self):
        for provider in ('codex', 'claude'):
            plan = AttemptPlan(provider, ID)
            command = command_plan(plan, 'linger')
            self.assertEqual(command[:3], ['/usr/bin/systemd-run', '--system', '--quiet'])
            for value in ['KillMode=control-group', 'RuntimeMaxSec=20s', 'TasksMax=32',
                          'MemoryMax=512M', 'MemorySwapMax=0', 'PrivateNetwork=yes',
                          'TemporaryFileSystem=/run:ro /var/lib:ro /etc:ro', 'ProtectHome=tmpfs',
                          'SupplementaryGroups=', 'NoNewPrivileges=yes']:
                self.assertIn('--property=' + value, command)
            start = command.index('--') + 1
            self.assertEqual(command[start:start+2], ['/usr/bin/env', '-i'])
            self.assertEqual(command[-5:], ['/usr/bin/python3', '-I', '/opt/forge-executor/fixture.py', 'linger', provider])
            self.assertNotIn('--user', command)
        self.assertTrue(any('/home/forge/client/config.toml' in v for v in command_plan(self.plan)))
        with self.assertRaises(ValueError):
            command_plan(self.plan, '/bin/sh')

    def test_start_verifies_absence_then_records_exact_invocation(self):
        run = mock.Mock(side_effect=[response(self.plan, absent=True), SimpleNamespace(returncode=0), response(self.plan)])
        executor = SystemdExecutor(run, exists=lambda p: False)
        with mock.patch('forge.executor.os.geteuid', return_value=0), mock.patch('forge.executor.verify_preconditions'):
            self.assertEqual(executor.start_synthetic(self.plan, apply=True), self.receipt)
        self.assertEqual(len(run.call_args_list), 3)

    def test_existing_unit_never_reused_and_nonroot_cannot_activate(self):
        with mock.patch('forge.executor.os.geteuid', return_value=1000), self.assertRaises(ExecutorError):
            SystemdExecutor().start_synthetic(self.plan, apply=True)
        run = mock.Mock(return_value=response(self.plan))
        with mock.patch('forge.executor.os.geteuid', return_value=0), mock.patch('forge.executor.verify_preconditions'), self.assertRaisesRegex(ExecutorError, 'already exists'):
            SystemdExecutor(run).start_synthetic(self.plan, apply=True)
        self.assertEqual(len(run.call_args_list), 1)

    def test_reconcile_refuses_replaced_invocation_or_foreign_cgroup(self):
        for value in [response(self.plan, invocation='b'*32), response(self.plan, cgroup='/system.slice/other.service'),
                      response(self.plan, User='root'), response(self.plan, absent=True, ControlGroup=self.plan.cgroup)]:
            with self.subTest(value=value.stdout), self.assertRaises(ExecutorError):
                SystemdExecutor(lambda *a, **k: value).reconcile(self.plan, self.receipt)

    def test_remaining_descendant_blocks_replacement_even_after_main_exit(self):
        executor = SystemdExecutor(lambda *a, **k: response(self.plan, active='failed'),
                                   exists=lambda p: True, read_text=lambda p: 'populated 1\nfrozen 0\n')
        result = executor.reconcile(self.plan, self.receipt)
        self.assertFalse(result['cleanup_verified'])
        self.assertFalse(result['cgroup_empty'])

    def test_absent_service_and_absent_or_empty_cgroup_prove_cleanup(self):
        for exists in (False, True):
            executor = SystemdExecutor(lambda *a, **k: response(self.plan, absent=True),
                                       exists=lambda p: exists, read_text=lambda p: 'populated 0\nfrozen 0\n')
            self.assertTrue(executor.reconcile(self.plan, self.receipt)['cleanup_verified'])

    def test_service_read_failure_or_bad_receipt_never_authorizes_cancel(self):
        run = mock.Mock(return_value=SimpleNamespace(returncode=1, stdout='', stderr='unavailable'))
        with mock.patch('forge.executor.os.geteuid', return_value=0), self.assertRaises(ExecutorError):
            SystemdExecutor(run).cancel(self.plan, self.receipt, apply=True)
        self.assertEqual(len(run.call_args_list), 1)
        bad = Receipt('codex', ID, 'other.service', INVOCATION, '/system.slice/other.service')
        with self.assertRaises(ExecutorError):
            SystemdExecutor(run).cancel(self.plan, bad)

    def test_cancel_stops_only_exact_verified_unit_and_checks_cgroup(self):
        run = mock.Mock(side_effect=[response(self.plan), SimpleNamespace(returncode=0), response(self.plan, absent=True)])
        executor = SystemdExecutor(run, exists=lambda p: False)
        with mock.patch('forge.executor.os.geteuid', return_value=0):
            self.assertTrue(executor.cancel(self.plan, self.receipt, apply=True)['cleanup_verified'])
        self.assertEqual(run.call_args_list[1].args[0], ['/usr/bin/systemctl', '--system', 'stop', self.plan.unit])

    def test_cancellation_timeout_has_no_process_group_fallback(self):
        run = mock.Mock(side_effect=[response(self.plan), subprocess.TimeoutExpired('systemctl', 15)])
        with mock.patch('forge.executor.os.geteuid', return_value=0), self.assertRaises(subprocess.TimeoutExpired):
            SystemdExecutor(run).cancel(self.plan, self.receipt, apply=True)
        self.assertEqual(len(run.call_args_list), 2)

    def test_unknown_cgroup_population_is_not_cleanup(self):
        executor = SystemdExecutor(lambda *a, **k: response(self.plan, active='failed'),
                                   exists=lambda p: True, read_text=lambda p: 'frozen 0\n')
        with self.assertRaises(ExecutorError):
            executor.reconcile(self.plan, self.receipt)

    def test_root_or_supplementary_group_identity_is_rejected(self):
        for uid, gid, groups in [(0, 99, [99]), (99, 0, [0]), (99, 99, [99, 27])]:
            account = SimpleNamespace(pw_uid=uid, pw_gid=gid)
            with mock.patch('forge.executor.pwd.getpwnam', return_value=account), \
                 mock.patch('forge.executor.grp.getgrnam', return_value=SimpleNamespace(gr_gid=gid)), \
                 mock.patch('forge.executor.os.getgrouplist', return_value=groups), \
                 self.assertRaises(ExecutorError):
                verify_preconditions(self.plan)
