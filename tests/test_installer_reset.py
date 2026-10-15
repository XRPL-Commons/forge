from contextlib import ExitStack
import json
import os
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge import install_reset, host_update
from forge.onboarding import Onboarding
from forge_client import _reset_host as host
from forge_client.installer_reset import Reset
from forge_client.contracts import SetupError, digest
from forge_client.workflow import Journal
from tests.sqlite_fixture import selected_store

INTENT = {'id': 'a' * 32, 'name': 'Anvil', 'revision': 'b' * 40}


class ResetImportTests(unittest.TestCase):
    def test_docker_removal_output_does_not_corrupt_rpc_json(self):
        with tempfile.TemporaryDirectory() as directory:
            command = Path(directory) / 'docker'
            command.write_text('#!/bin/sh\nprintf "%s\\n" "container-removed"\n')
            command.chmod(0o700)
            script = """
import json, sys
from pathlib import Path
from forge_client import _reset_host as host, _install_host as install
install.ENV = {'PATH': sys.argv[1] + ':/usr/bin:/bin'}
observations = iter([[{'Id': 'd' * 64, 'Name': '/forge-egress-1'}], []])
host.containers = lambda: next(observations)
host.egress_identity = lambda container, receipt: {'id': container['Id']}
def dispatch(request):
    receipt = {}
    host.cleanup_egress(Path(sys.argv[1]), receipt)
    return {'ok': True, 'result': receipt['egress_cleanup']}
host.dispatch = dispatch
sys.exit(host.main())
"""
            result = subprocess.run([sys.executable, '-c', script, directory], input='{}',
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'ok': True, 'result': {
                'status': 'removed', 'identity': {'id': 'd' * 64}}})

    def test_installed_reset_module_imports_from_foreign_working_directory(self):
        # --help imports the actual installed entrypoint without initiating reset.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'runtime'; selected_store(root)
            foreign = Path(directory) / 'login'; (foreign / 'forge').mkdir(parents=True)
            (foreign / 'forge/__init__.py').write_text("raise RuntimeError('wrong working directory')")
            source = Path(__file__).resolve().parents[1]
            def service(argv, capture=False):
                command = [sys.executable if arg == '/usr/bin/python3' else arg for arg in argv]
                # CI qualifies SQLite through its library path; preserve that
                # runtime while still excluding PYTHONPATH and the login cwd.
                environment = {'HOME': directory, 'PATH': '/usr/bin:/bin'}
                if 'LD_LIBRARY_PATH' in os.environ:
                    environment['LD_LIBRARY_PATH'] = os.environ['LD_LIBRARY_PATH']
                result = subprocess.run([*command, '--help'], cwd=foreign,
                    env=environment, text=True, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('--generation', result.stdout)
                return '{"imported": true}'
            with patch.object(host, 'SOURCE', source), patch.object(host, 'ROOT', root), \
                    patch.object(host, 'service', side_effect=service):
                self.assertEqual(host.snapshot(), {'imported': True})


class HostResetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base = Path(self.directory.name).resolve()
        self.home = base / 'home/forge'; self.home.mkdir(parents=True)
        self.source = self.home / 'forge'; self.source.mkdir()
        self.root = self.home / '.local/share/forge'; self.root.mkdir(parents=True)
        self.state = base / 'var/lib/forge-install'; self.state.mkdir(parents=True, mode=0o700)
        self.archives = base / 'var/lib/forge-reset'
        (self.state / 'installation.json').write_text(json.dumps({'version': 1, 'intent': INTENT, 'step': 'complete', 'completed': []}))
        self.owner = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(self.home))
        self.scope = {'home_device': self.home.stat().st_dev, 'home_inode': self.home.stat().st_ino,
            'state_device': self.state.stat().st_dev, 'state_inode': self.state.stat().st_ino,
            'source_device': self.source.stat().st_dev, 'source_inode': self.source.stat().st_ino}
        self.units = self.home / '.config/systemd/user'; self.units.mkdir(parents=True)
        for name in host.SERVICES:
            (self.units / name).write_text('[Service]\nExecStart=/known/forge')
        self.view = {'instance': 'selected-instance', 'generation': 'original-generation', 'phase': 'setup',
            'source_revision': 'c' * 40, 'units': {str(self.units / name): host.file_digest(self.units / name) for name in host.SERVICES},
            'pending_effects': 0, 'leases': 0, 'reset_receipt': None, 'error': None,
            'setup_revision': 4, 'setup_fingerprint': 'f' * 64}
        self.drained = False
        self.calls = []
        self.manager_state = 'active'
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for key, value in {'HOME': self.home, 'SOURCE': self.source, 'ROOT': self.root, 'STATE': self.state, 'ARCHIVES': self.archives}.items():
            self.stack.enter_context(patch.object(host, key, value))
        self.stack.enter_context(patch.object(host, 'read', side_effect=self.read))
        self.stack.enter_context(patch.object(host, 'account', side_effect=lambda: self.owner))
        self.stack.enter_context(patch.object(host, 'paths', return_value=self.scope))
        self.stack.enter_context(patch.object(host, 'snapshot', side_effect=self.snapshot))
        self.stack.enter_context(patch.object(host, 'private_directory', side_effect=self.private))
        self.stack.enter_context(patch.object(host, 'service', side_effect=self.service))
        self.stack.enter_context(patch.object(host, 'run', side_effect=self.command))
        self.stack.enter_context(patch.object(host.pwd, 'getpwnam', side_effect=self.getpwnam))

    def read(self):
        return json.loads((self.state / 'installation.json').read_text()) if self.state.exists() else None

    def getpwnam(self, name):
        if self.owner is None:
            raise KeyError(name)
        return self.owner

    def private(self, path, *, create=False):
        if create: path.mkdir(mode=0o700, exist_ok=True)
        host.need(path.is_dir() and not path.is_symlink() and path.stat().st_mode & 0o077 == 0, 'unsafe_install_state')

    def snapshot(self, **kwargs):
        if self.drained:
            return {**self.view, 'generation': 'drained-generation', 'reset_receipt': {'id': 'reset-operation', 'generation': 'original-generation'}}
        return self.view

    def service(self, argv, capture=False):
        self.calls.append(argv)
        if argv[0] == 'git':
            return host.REPOSITORY if argv[-3:] == ['remote', 'get-url', 'origin'] else ''
        if 'list-units' in argv: return ''
        return ''

    def command(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[0] == 'userdel': self.owner = None
        if argv[:2] == ['systemctl', 'show']: return self.manager_state
        if argv[:2] == ['systemctl', 'stop']: self.manager_state = 'inactive'
        if argv[:3] == ['docker', 'container', 'ls']: return '\n'.join(getattr(self, 'containers', {}))
        if argv[:3] == ['docker', 'container', 'inspect']: return json.dumps([self.containers[key] for key in argv[3:]])
        if argv[:3] == ['docker', 'container', 'rm']:
            receipt = host.read_receipt(self.archives / INTENT['id'])
            self.assertEqual(receipt['egress_cleanup']['status'], 'pending')
            self.assertEqual(receipt['egress_cleanup']['identity']['id'], argv[-1])
            del self.containers[argv[-1]]
            if getattr(self, 'lose_remove_response', False):
                self.lose_remove_response = False
                raise host.InstallError('command_failed')
        return ''

    def request(self, *, apply=False, revision=None):
        return host.dispatch({'version': 1, 'intent': INTENT, 'apply': apply, 'revision': revision})['result']

    def add_egress(self):
        self.containers = {}
        network = self.root / 'deploy/runner'; network.mkdir(parents=True)
        source = self.source / 'deploy/runner'; source.mkdir(parents=True)
        for name in ('compose.yaml', 'egress.py'):
            body = (Path(__file__).resolve().parents[1] / 'deploy/runner' / name).read_text()
            (network / name).write_text(body); (source / name).write_text(body)
        identifier = 'd' * 64
        self.containers[identifier] = {
            'Id': identifier, 'Name': '/forge-egress-1',
            'Config': {'Labels': {'com.docker.compose.project': 'forge', 'com.docker.compose.service': 'egress',
                'com.docker.compose.project.working_dir': str(network),
                'com.docker.compose.project.config_files': str(network / 'compose.yaml')}},
            'Mounts': [{'Type': 'bind', 'RW': False, 'Source': str(network / 'egress.py'),
                        'Destination': '/opt/forge/egress.py'}]}
        return self.containers[identifier]

    def paused_reset(self):
        plan = self.request()
        self.request(apply=True, revision=plan['revision'])
        self.drained = True
        return plan

    def test_reset_removes_only_verified_proxy_with_durable_intent(self):
        proxy = self.add_egress()
        foreign = 'e' * 64
        self.containers[foreign] = {'Id': foreign, 'Name': '/other', 'Config': {}, 'Mounts': []}
        plan = self.paused_reset()
        self.assertEqual(self.request(apply=True, revision=plan['revision'])['status'], 'reset_complete')
        self.assertEqual(list(self.containers), [foreign])
        self.assertEqual([row for row in self.calls if row[:3] == ['docker', 'container', 'rm']],
                         [['docker', 'container', 'rm', '--force', proxy['Id']]])
        receipt = host.read_receipt(self.archives / INTENT['id'])
        self.assertEqual(receipt['egress_cleanup']['status'], 'removed')

    def test_lost_remove_response_resumes_by_observed_absence(self):
        self.add_egress(); plan = self.paused_reset()
        self.lose_remove_response = True
        with self.assertRaises(host.InstallError): self.request(apply=True, revision=plan['revision'])
        self.assertTrue(self.home.exists())
        self.assertEqual(host.read_receipt(self.archives / INTENT['id'])['step'], 'stop')
        self.assertEqual(self.request(apply=True, revision=plan['revision'])['status'], 'reset_complete')
        self.assertEqual(sum(row[:3] == ['docker', 'container', 'rm'] for row in self.calls), 1)

    def test_replaced_proxy_is_not_removed_after_uncertain_response(self):
        proxy = self.add_egress(); plan = self.paused_reset()
        self.lose_remove_response = True
        with self.assertRaises(host.InstallError): self.request(apply=True, revision=plan['revision'])
        proxy['Id'] = 'e' * 64
        self.containers[proxy['Id']] = proxy
        with self.assertRaisesRegex(host.InstallError, 'egress_changed'):
            self.request(apply=True, revision=plan['revision'])
        self.assertTrue(self.home.exists())
        self.assertEqual(sum(row[:3] == ['docker', 'container', 'rm'] for row in self.calls), 1)

    def test_proxy_reappearing_after_cleanup_blocks_home_archive(self):
        proxy = self.add_egress(); plan = self.paused_reset()
        original = self.command
        def replace_after_account(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[0] == 'userdel':
                proxy['Id'] = 'e' * 64
                self.containers[proxy['Id']] = proxy
            return result
        with patch.object(host, 'run', side_effect=replace_after_account):
            with self.assertRaisesRegex(host.InstallError, 'egress_changed'):
                self.request(apply=True, revision=plan['revision'])
        self.assertTrue(self.home.exists())
        self.assertEqual(sum(row[:3] == ['docker', 'container', 'rm'] for row in self.calls), 1)

    def test_foreign_proxy_scope_or_mount_is_never_removed(self):
        proxy = self.add_egress(); plan = self.paused_reset()
        for change in ('labels', 'mount', 'script'):
            with self.subTest(change=change):
                original = json.loads(json.dumps(proxy))
                if change == 'labels': proxy['Config']['Labels']['com.docker.compose.project.working_dir'] = '/foreign'
                if change == 'mount': proxy['Mounts'][0]['RW'] = True
                if change == 'script': (self.root / 'deploy/runner/egress.py').write_text('foreign script')
                with self.assertRaisesRegex(host.InstallError, 'foreign_egress'):
                    self.request(apply=True, revision=plan['revision'])
                proxy.clear(); proxy.update(original)
        self.assertFalse(any(row[:3] == ['docker', 'container', 'rm'] for row in self.calls))
        self.assertTrue(self.home.exists())

    def test_absent_proxy_needs_no_removal_and_preserves_old_reset_identity(self):
        plan = self.paused_reset()
        self.assertEqual(self.request(apply=True, revision=plan['revision'])['status'], 'reset_complete')
        receipt = host.read_receipt(self.archives / INTENT['id'])
        self.assertEqual(receipt['plan']['revision'], plan['revision'])
        self.assertEqual(receipt['egress_cleanup'], {'status': 'absent'})
        self.assertFalse(any(row[:3] == ['docker', 'container', 'rm'] for row in self.calls))

    def test_plan_is_read_only_and_lists_archive_and_preserved_resources(self):
        result = self.request()
        self.assertEqual(result['status'], 'reset_planned')
        self.assertEqual(result['revision'], digest(result['identity']))
        self.assertIn('Tailscale identity and Serve', result['preserved'])
        self.assertFalse(self.archives.exists())
        self.assertTrue(all(row[0] == 'git' or 'list-units' in row for row in self.calls))

    def test_stale_plan_cannot_queue_reset(self):
        plan = self.request()
        self.view['generation'] = 'changed'
        with self.assertRaises(host.InstallError):
            self.request(apply=True, revision=plan['revision'])
        self.assertFalse(any('stop' in row or 'start' in row for row in self.calls))

    def test_pending_git_effects_block_account_and_archives(self):
        plan = self.request()
        self.request(apply=True, revision=plan['revision'])
        self.view['pending_effects'] = 1
        self.drained = True
        result = self.request(apply=True, revision=plan['revision'])
        self.assertEqual(result['status'], 'reset_pending')
        self.assertTrue(self.home.exists())
        self.assertTrue(self.state.exists())
        self.assertFalse(any(row[0] == 'userdel' for row in self.calls))

    def test_completed_reset_archives_home_and_installer_and_is_replayable(self):
        plan = self.request()
        first = self.request(apply=True, revision=plan['revision'])
        self.assertEqual(first['status'], 'reset_pending')
        self.drained = True
        result = self.request(apply=True, revision=plan['revision'])
        self.assertEqual(result['status'], 'reset_complete')
        self.assertFalse(self.home.exists())
        self.assertFalse(self.state.exists())
        self.assertTrue(Path(result['archive']['home']).is_dir())
        self.assertTrue(Path(result['archive']['installer']).is_dir())
        self.assertEqual(self.request(apply=True, revision=plan['revision']), result)
        self.assertFalse(any(row[0] in ('rm', 'tailscale', 'apt-get') for row in self.calls))

    def test_resume_after_home_rename_before_receipt_write(self):
        plan = self.request()
        self.request(apply=True, revision=plan['revision'])
        self.drained = True
        original = host.write_receipt
        failed = False
        def write(directory, value):
            nonlocal failed
            if value['step'] == 'archive_installer' and not failed:
                failed = True
                raise OSError('lost power after home rename')
            original(directory, value)
        with patch.object(host, 'write_receipt', side_effect=write):
            with self.assertRaises(OSError):
                self.request(apply=True, revision=plan['revision'])
        self.assertFalse(self.home.exists())
        self.assertEqual(self.request(apply=True, revision=plan['revision'])['status'], 'reset_complete')

    def test_unmanaged_service_prevents_reset_plan(self):
        (self.units / 'unrelated.service').write_text('[Service]')
        with self.assertRaises(host.InstallError):
            self.request()

    def test_modified_managed_service_prevents_reset_plan(self):
        (self.units / host.SERVICES[0]).write_text('custom service')
        with self.assertRaises(host.InstallError):
            self.request()

    def test_new_mount_owner_blocks_archive_instead_of_removing_container(self):
        plan = self.request()
        self.request(apply=True, revision=plan['revision'])
        self.drained = True
        with patch.object(host, 'docker_mounts_clear', side_effect=host.InstallError('unsafe_install_state')):
            with self.assertRaises(host.InstallError):
                self.request(apply=True, revision=plan['revision'])
        self.assertTrue(self.home.exists())
        self.assertIsNotNone(self.owner)

    def test_initial_receipt_interruption_can_resume(self):
        plan = self.request()
        self.archives.mkdir(mode=0o700)
        (self.archives / INTENT['id']).mkdir(mode=0o700)
        self.assertEqual(self.request(apply=True, revision=plan['revision'])['status'], 'reset_pending')

    def test_changed_setup_revision_invalidates_plan_before_any_stop(self):
        plan = self.request()
        self.view['setup_revision'] += 1
        with self.assertRaises(host.InstallError):
            self.request(apply=True, revision=plan['revision'])
        self.assertFalse(any('stop' in row or 'start' in row for row in self.calls))

    def test_resume_after_drain_rejects_new_operator_configuration(self):
        plan = self.request()
        self.request(apply=True, revision=plan['revision'])
        self.drained = True
        with patch.object(host, 'confirm_drained', side_effect=OSError('power loss before stop')):
            with self.assertRaises(OSError):
                self.request(apply=True, revision=plan['revision'])
        self.view['setup_fingerprint'] = 'e' * 64
        with self.assertRaises(host.InstallError):
            self.request(apply=True, revision=plan['revision'])
        self.assertTrue(self.home.exists())
        self.assertFalse(any(row[0] == 'userdel' or row[:2] == ['systemctl', 'stop'] for row in self.calls))

    def test_change_during_owned_stop_blocks_account_retirement(self):
        plan = self.request()
        self.request(apply=True, revision=plan['revision'])
        self.drained = True
        original = self.service
        def race(argv, capture=False):
            if argv[:3] == ['systemctl', '--user', 'stop']:
                self.view['setup_revision'] += 1
            return original(argv, capture=capture)
        with patch.object(host, 'service', side_effect=race):
            with self.assertRaises(host.InstallError):
                self.request(apply=True, revision=plan['revision'])
        self.assertTrue(self.home.exists())
        self.assertFalse(any(row[0] == 'userdel' or row[:2] == ['systemctl', 'stop'] for row in self.calls))

    def test_interruption_after_user_manager_stop_resumes_without_user_bus(self):
        plan = self.request()
        self.request(apply=True, revision=plan['revision'])
        self.drained = True
        original = host.write_receipt
        failed = False
        def write(directory, value):
            nonlocal failed
            if value['step'] == 'account' and not failed:
                failed = True
                raise OSError('power loss after user manager stopped')
            original(directory, value)
        with patch.object(host, 'write_receipt', side_effect=write):
            with self.assertRaises(OSError):
                self.request(apply=True, revision=plan['revision'])
        self.assertEqual(self.manager_state, 'inactive')
        def stopped_bus(argv, capture=False):
            if argv[0] == 'systemctl':
                raise AssertionError('stopped user bus cannot accept commands')
            return self.service(argv, capture=capture)
        with patch.object(host, 'service', side_effect=stopped_bus):
            self.assertEqual(self.request(apply=True, revision=plan['revision'])['status'], 'reset_complete')

    def test_unrelated_transient_workload_blocks_before_any_service_stop(self):
        plan = self.request()
        self.request(apply=True, revision=plan['revision'])
        self.drained = True
        original = self.service
        def units(argv, capture=False):
            if 'list-units' in argv:
                return 'backup-work.service loaded active running Unrelated backup'
            return original(argv, capture=capture)
        with patch.object(host, 'service', side_effect=units):
            with self.assertRaisesRegex(host.InstallError, 'unrelated_user_unit'):
                self.request(apply=True, revision=plan['revision'])
        self.assertFalse(any('stop' in row for row in self.calls))
        self.assertTrue(self.home.exists())

    def test_loaded_managed_name_cannot_hide_a_foreign_unit(self):
        def units(argv, capture=False):
            if 'list-units' in argv:
                return 'forge-ui.service loaded active running UI'
            return 'FragmentPath=/run/user/1001/systemd/transient/forge-ui.service\nDropInPaths=\nTransient=yes'
        with patch.object(host, 'service', side_effect=units):
            with self.assertRaisesRegex(host.InstallError, 'unsafe_user_units'):
                host.user_units_clear(INTENT, 1001)

    def test_only_matching_owned_environment_transient_is_allowed(self):
        name = 'forge-install-environment-' + INTENT['id'] + '.service'
        def units(argv, capture=False):
            if 'list-units' in argv:
                return name + ' loaded active running Forge environment'
            return 'FragmentPath=/run/user/1001/systemd/transient/' + name + '\nDropInPaths=\nTransient=yes'
        with patch.object(host, 'service', side_effect=units):
            host.user_units_clear(INTENT, 1001)
            with self.assertRaisesRegex(host.InstallError, 'unrelated_user_unit'):
                host.user_units_clear({**INTENT, 'id': 'b' * 32}, 1001)

    def test_package_user_units_require_unmodified_root_owned_fragments(self):
        def units(argv, capture=False):
            if 'list-units' in argv:
                return 'dbus.service loaded active running User message bus'
            return 'FragmentPath=/usr/lib/systemd/user/dbus.service\nDropInPaths=\nTransient=no'
        with patch.object(host, 'service', side_effect=units), patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'resolve', lambda path: path), \
             patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)):
            host.user_units_clear(INTENT, 1001)
        with patch.object(host, 'service', side_effect=units), patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'resolve', lambda path: path), \
             patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=1001, st_mode=0o100644)):
            with self.assertRaises(host.InstallError):
                host.user_units_clear(INTENT, 1001)


    def test_default_ubuntu_inventory_ignores_reflected_units_and_inactive_package_services(self):
        rows = ['init.scope loaded active running Manager',
                'sys-devices-virtual-net-eth0.device loaded active plugged Network',
                'proc-sys-fs-binfmt_misc.mount loaded active mounted Kernel',
                '-.slice loaded active active Root',
                'blockdev@dev-loop0.target loaded inactive dead Block device',
                'network-online.target loaded active active Network']
        rows += [name + ' loaded active running Forge' for name in host.SERVICES]
        rows += [name + '.service loaded inactive dead Package service' for name in
                 ('dbus', 'dirmngr', 'gpg-agent', 'keyboxd', 'launchpadlib-cache-clean',
                  'pk-debconf-helper', 'snapd.session-agent')]
        rows += [name + '.socket loaded active listening Package socket' for name in
                 ('dbus', 'dirmngr', 'gpg-agent', 'gpg-agent-browser', 'gpg-agent-extra',
                  'gpg-agent-ssh', 'keyboxd', 'pk-debconf-helper', 'snapd.session-agent')]
        rows.append('launchpadlib-cache-clean.timer loaded active waiting Package timer')
        inspected = []
        def units(argv, capture=False):
            if 'list-units' in argv:
                self.assertIn('--type=service,scope,socket,timer,path', argv)
                return '\n'.join(rows)
            name = argv[3]; inspected.append(name)
            path = self.units / name if name in host.SERVICES else Path('/usr/lib/systemd/user') / name
            result = 'FragmentPath=' + str(path) + '\nDropInPaths=\nTransient=no'
            if 'ControlGroup' in argv[-1]: result += '\nControlGroup='
            return result
        with patch.object(host, 'service', side_effect=units), patch.object(Path, 'is_file', return_value=True), \
             patch.object(Path, 'resolve', lambda path: path), \
             patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)):
            host.user_units_clear(INTENT, 1001)
        self.assertTrue(all(name.endswith(('.service', '.socket', '.timer')) for name in inspected))

    def test_inactive_service_requires_empty_cgroup_and_unmodified_package_fragment(self):
        base = {'FragmentPath': '/usr/lib/systemd/user/gpg-agent.service', 'DropInPaths': '',
                'Transient': 'no', 'ControlGroup': ''}
        cases = [dict(base, ControlGroup='/user.slice/leftover'), dict(base, Transient='yes'),
                 dict(base, FragmentPath='/run/user/1001/systemd/transient/gpg-agent.service'),
                 dict(base, FragmentPath=str(self.units / 'gpg-agent.service')),
                 dict(base, DropInPaths=str(self.units / 'gpg-agent.service.d/custom.conf'))]
        for value in cases:
            with self.subTest(value=value):
                def units(argv, capture=False):
                    if 'list-units' in argv: return 'gpg-agent.service loaded inactive dead Agent'
                    return '\n'.join(key + '=' + item for key, item in value.items())
                with patch.object(host, 'service', side_effect=units), patch.object(Path, 'is_file', return_value=True), \
                     patch.object(Path, 'resolve', lambda path: path), \
                     patch.object(Path, 'stat', return_value=SimpleNamespace(st_uid=0, st_mode=0o100644)):
                    with self.assertRaisesRegex(host.InstallError, 'unsafe_user_units'):
                        host.user_units_clear(INTENT, 1001)

    def test_unrelated_active_package_workload_and_activation_units_remain_blocked(self):
        for unit, active, substate in [('gpg-agent.service', 'active', 'running'),
                                     ('gpg-agent.service', 'failed', 'failed'),
                                     ('custom-backup.timer', 'active', 'waiting'),
                                     ('custom-backup.path', 'active', 'waiting'),
                                     ('custom-agent.socket', 'active', 'listening')]:
            with self.subTest(unit=unit, active=active), patch.object(host, 'service',
                    return_value=f'{unit} loaded {active} {substate} Unrelated workload'):
                with self.assertRaisesRegex(host.InstallError, 'unrelated_user_unit'):
                    host.user_units_clear(INTENT, 1001)



class ClientResetTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name); self.root.chmod(0o700)
        self.host_journal = Journal('fresh-host', 'anvil', root=self.root)
        self.install_journal = Journal('installation', 'anvil', root=self.root)
        self.host_state = {'version': 1, 'ssh': 'anvil', 'host_key': 'verified-key', 'intent': INTENT}
        self.host_journal.save(self.host_state)
        self.install_state = {'version': 1, 'intent': {'ssh': 'anvil'}, 'saved': 'progress'}
        self.install_journal.save(self.install_state)
        self.workflow = SimpleNamespace(ssh='anvil', key='anvil-lock', journal=self.host_journal, host=Mock())
        self.identity = {'intent': INTENT, 'instance': 'selected-instance'}
        self.report = {'status': 'reset_planned', 'identity': self.identity, 'revision': digest(self.identity)}
        self.workflow.host.rpc.side_effect = lambda *args, **kwargs: {'ok': True, 'result': dict(self.report)}
        self.reset = Reset(self.workflow)

    def test_apply_requires_previously_observed_exact_plan(self):
        with self.assertRaises(SetupError):
            self.reset.execute(apply=True, revision=self.report['revision'])
        self.workflow.host.rpc.assert_not_called()
        self.reset.execute()
        with self.assertRaises(SetupError):
            self.reset.execute(apply=True, revision='f' * 64)

    def test_journals_stay_until_verified_remote_completion_then_archive(self):
        self.reset.execute()
        self.report['status'] = 'reset_pending'
        self.reset.execute(apply=True, revision=self.report['revision'])
        self.assertEqual(self.host_journal.read(), self.host_state)
        self.assertEqual(self.install_journal.read(), self.install_state)
        self.report['status'] = 'reset_complete'
        self.reset.execute(apply=True, revision=self.report['revision'])
        self.assertIsNone(self.host_journal.read())
        self.assertIsNone(self.install_journal.read())
        self.assertEqual(Journal('reset-archive-fresh-host', INTENT['id'], root=self.root).read(), self.host_state)
        self.assertEqual(self.workflow.host.fingerprint, 'verified-key')
        self.assertEqual(self.reset.execute()['status'], 'reset_complete')

    def test_changed_local_installation_is_preserved_after_remote_completion(self):
        self.reset.execute()
        self.install_journal.save({**self.install_state, 'saved': 'new work'})
        self.report['status'] = 'reset_complete'
        with self.assertRaises(SetupError):
            self.reset.execute(apply=True, revision=self.report['revision'])
        self.assertEqual(self.install_journal.read()['saved'], 'new work')

    def test_reset_archives_old_consent_so_next_installation_can_register(self):
        consent = Journal('install-consent', 'anvil', root=self.root)
        saved = {'version': 1, 'binding': {'setup_id': INTENT['id']}, 'phase': 'registered'}
        consent.save(saved)
        self.reset.execute()
        self.report['status'] = 'reset_complete'
        self.reset.execute(apply=True, revision=self.report['revision'])
        self.assertIsNone(consent.read())
        self.assertEqual(Journal('reset-archive-install-consent', INTENT['id'], root=self.root).read(), saved)

    def test_forged_or_mismatched_remote_completion_cannot_clear_journals(self):
        self.reset.execute()
        self.report.update(status='reset_complete', identity={'intent': {**INTENT, 'id': 'd' * 32}})
        with self.assertRaises(SetupError):
            self.reset.execute(apply=True, revision=self.report['revision'])
        self.assertEqual(self.host_journal.read(), self.host_state)


class DrainTests(unittest.TestCase):
    def test_identity_client_rotation_invalidates_reset_without_changing_authorization_epoch(self):
        from forge.operator_auth import OperatorAuth
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); store = selected_store(root)
            Onboarding(root).read()
            auth = OperatorAuth(root)
            secrets = ('first-client-secret-' + 'a' * 24, 'second-client-secret-' + 'b' * 24,
                       'third-client-secret-' + 'c' * 24)
            auth.configure('https://anvil.example', 'original-client', secrets[0], [7])
            units = root / '.config/systemd/user'
            record = Mock(data={'managed': {str(units / (name + '.service')): 'hash' for name in host_update.SERVICES}})
            with patch.object(install_reset, 'Record', return_value=record), \
                 patch.object(install_reset, 'source_revision', return_value='a' * 40), \
                 patch.object(Path, 'home', return_value=root):
                before = install_reset.inspect(root)
                with store.read_snapshot() as db:
                    epoch = db.execute('SELECT authorization_epoch FROM operator_auth_configuration').fetchone()[0]
                self.assertEqual(auth.rotate_client('replacement-client', secrets[1])['status'], 'rotated')
                after = install_reset.inspect(root)
                with store.read_snapshot() as db:
                    self.assertEqual(db.execute('SELECT authorization_epoch FROM operator_auth_configuration').fetchone()[0], epoch)
                self.assertEqual(after['setup_revision'], before['setup_revision'])
                self.assertNotEqual(after['setup_fingerprint'], before['setup_fingerprint'])
                # Secret-only rotation must also fence an old plan.
                auth.rotate_client('replacement-client', secrets[2])
                rotated_secret = install_reset.inspect(root)
                self.assertNotEqual(rotated_secret['setup_fingerprint'], after['setup_fingerprint'])
                # Replaying the exact identity does not invalidate a current plan.
                self.assertEqual(auth.rotate_client('replacement-client', secrets[2])['status'], 'unchanged')
                self.assertEqual(install_reset.inspect(root)['setup_fingerprint'], rotated_secret['setup_fingerprint'])
                for secret in secrets:
                    self.assertNotIn(secret, json.dumps(rotated_secret))

    def test_inspect_fingerprint_changes_with_setup_or_active_configuration(self):
        from forge.store.configuration import Configuration
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); store = selected_store(root)
            setup = Onboarding(root); setup.read()
            units = root / '.config/systemd/user'
            record = Mock(data={'managed': {str(units / (name + '.service')): 'hash' for name in host_update.SERVICES}})
            with patch.object(install_reset, 'Record', return_value=record), \
                 patch.object(install_reset, 'source_revision', return_value='a' * 40), \
                 patch.object(Path, 'home', return_value=root):
                first = install_reset.inspect(root)
                with setup.store.transaction() as data: data['error'] = 'new operator work'
                second = install_reset.inspect(root)
                self.assertGreater(second['setup_revision'], first['setup_revision'])
                self.assertNotEqual(second['setup_fingerprint'], first['setup_fingerprint'])
                Configuration(store).replace('runtime.json', {'enabled': False}, expected_revision=0)
                third = install_reset.inspect(root)
                self.assertEqual(third['setup_revision'], second['setup_revision'])
                self.assertNotEqual(third['setup_fingerprint'], second['setup_fingerprint'])

    def test_queue_uses_existing_reset_fence_and_wrong_instance_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); store = selected_store(root)
            setup = Onboarding(root); state = setup.read()
            with patch.object(install_reset, 'inspect', return_value={'phase': 'resetting'}):
                with self.assertRaises(SetupError):
                    install_reset.drain(root, 'another-store', state['generation'])
                self.assertEqual(setup.read()['phase'], 'setup')
                install_reset.drain(root, store.instance_id, state['generation'])
                self.assertEqual(setup.read()['phase'], 'resetting')
                operation = setup.read()['operation']['id']
                install_reset.drain(root, store.instance_id, state['generation'])
                self.assertEqual(setup.read()['operation']['id'], operation)


if __name__ == '__main__':
    unittest.main()
