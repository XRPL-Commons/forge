"""Initial cutover installs assets without changing frozen JSON authority."""
from contextlib import ExitStack
import io
import json
import subprocess
import sys
import textwrap
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge import host_initial_update as initial, host_update
from forge.store.configuration import Configuration
from forge.store.db import Store
from forge.store.migration.snapshot import MigrationError
from tests.test_host_update import FakeHost


class InitialHostUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.host = FakeHost(str(Path(self.temporary.name).resolve()))
        self.host.reconcile()
        self.host.revision = 'b' * 40
        self.operation = 'e4e9ffb8-2d5f-4363-ac3d-439efb76d4fd'
        self.journal = dict(protocol=1, authorized=True, id=self.operation, root=str(self.host.root),
            source=str(host_update.SOURCE), source_revision=self.host.revision, stage='source_preparing',
            initial_source=dict(from_revision='a' * 40, to_revision=self.host.revision, prepared=None))
        self.save_journal()
        (self.host.root / 'runtime.json').write_text(json.dumps(dict(enabled=False, merge_enabled=False, image='old', slots={'build': 2})))
        (self.host.root / 'setup').mkdir()
        (self.host.root / 'setup/runtime.json').write_text(json.dumps(dict(enabled=False, image='old', memory='2g')))
        self.frozen = {path: path.read_bytes() for path in self.host.root.rglob('*.json')
                       if path.name != '.migration-cutover.json'}
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        for item in self.host.patches(): self.stack.enter_context(item)
        self.stack.enter_context(patch.object(initial.Host, 'quiet'))
        self.stack.enter_context(patch('sys.stdout', io.StringIO()))
        self.host.calls.clear()
        # The fake Docker inventory resolves immutable image IDs as real Docker does.
        self.host.images.update({image: image for image in self.host.images.values()})

    def save_journal(self):
        initial._atomic_json(self.host.root / '.migration-cutover.json', self.journal)

    def prepare(self):
        return initial.prepare_initial(self.host.root, operation_id=self.operation, source_revision=self.host.revision)

    def select(self, receipt):
        store = Store.create(self.host.root / 'sqlite')
        with store.write() as db:
            db.executemany('INSERT INTO store_metadata(key,value) VALUES(?,?)',
                           [('runtime_root', str(self.host.root)), ('runtime_active', 'true')])
        config = Configuration(store)
        for name in ('host.json', *host_update.RUNTIME_DOMAINS):
            path = self.host.root / name
            if path.exists(): config.replace(name, json.loads(path.read_text()), expected_revision=0)
        marker = self.host.root / 'state/storage'
        marker.write_text('forge-sqlite-v1\n' + str(store.root) + '\n' + store.instance_id + '\n')
        marker.chmod(0o600)
        self.journal['stage'] = 'selected'
        self.journal['initial_source']['prepared'] = receipt
        self.save_journal()
        return store

    def test_prepare_keeps_frozen_configuration_and_never_starts_writers(self):
        receipt = self.prepare()
        self.assertEqual(receipt['source_revision'], 'b' * 40)
        self.assertEqual(self.host.restarted(), [])
        self.assertFalse(any(argv[:3] in (['systemctl', '--user', 'start'], ['systemctl', '--user', 'enable']) for argv in self.host.calls))
        for path, content in self.frozen.items(): self.assertEqual(path.read_bytes(), content, str(path))
        self.assertIn('forge.launch', self.host.unit('forge-status'))
        self.assertTrue((self.host.root / 'ui/current/server/index.mjs').exists())
        self.assertEqual((self.host.root / initial.PREPARED).stat().st_mode & 0o777, 0o600)
        self.host.calls.clear()
        self.assertEqual(self.prepare(), receipt)
        self.assertFalse(any(argv[:3] == ['systemctl', '--user', 'daemon-reload'] for argv in self.host.calls))

    def test_interrupted_asset_install_replays_without_touching_frozen_configuration(self):
        command = host_update.command
        def interrupted(argv, **kwargs):
            if argv[:3] == ['systemctl', '--user', 'daemon-reload']:
                raise RuntimeError('lost connection after unit writes')
            return command(argv, **kwargs)
        with patch.object(host_update, 'command', side_effect=interrupted):
            with self.assertRaisesRegex(RuntimeError, 'lost connection'):
                self.prepare()
        state = json.loads((self.host.root / initial.PREPARED).read_text())
        self.assertFalse(state['ready'])
        receipt = self.prepare()
        self.assertEqual(receipt['source_revision'], self.host.revision)
        for path, content in self.frozen.items(): self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual(self.host.restarted(), [])

    def test_commit_is_atomic_canonical_and_replay_preserves_later_operator_changes(self):
        receipt = self.prepare(); store = self.select(receipt)
        self.assertEqual(initial.commit_initial(self.host.root, receipt, operation_id=self.operation), receipt)
        config = Configuration(store)
        for domain in host_update.RUNTIME_DOMAINS:
            row = config.get(domain)
            self.assertEqual(row['settings']['image'], receipt['image'])
            self.assertEqual(row['settings']['apparmor_profile'], host_update.APPARMOR_PROFILE)
        current = config.get('runtime.json')
        self.assertEqual(current['settings']['slots'], {'build': 2})
        self.assertFalse(current['settings']['enabled'])
        config.replace('runtime.json', {**current['settings'], 'enabled': True}, expected_revision=current['revision'])
        version = config.get('runtime.json')['revision']
        self.assertEqual(config.get('host.json')['settings']['source'], 'b' * 40)
        # Lost response after commit: replay doesn't rewrite enabled/image or add revisions.
        initial.commit_initial(self.host.root, receipt, operation_id=self.operation)
        self.assertEqual(config.get('runtime.json')['revision'], version)
        self.assertTrue(config.get('runtime.json')['settings']['enabled'])
        for path, content in self.frozen.items(): self.assertEqual(path.read_bytes(), content, str(path))
        self.assertEqual(self.host.restarted(), [])

    def test_failure_mid_commit_rolls_back_all_configuration_and_receipt(self):
        receipt = self.prepare(); store = self.select(receipt)
        replace = Configuration.replace_in
        def fail(config, db, domain, *args, **kwargs):
            if domain == 'runtime.json': raise RuntimeError('interrupted commit')
            return replace(config, db, domain, *args, **kwargs)
        with patch.object(Configuration, 'replace_in', fail):
            with self.assertRaisesRegex(RuntimeError, 'interrupted commit'):
                initial.commit_initial(self.host.root, receipt, operation_id=self.operation)
        self.assertEqual(Configuration(store).get('host-runtime.json')['revision'], 1)
        with store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT value FROM store_metadata WHERE key=?', (initial.COMMITTED,)).fetchone())
        initial.commit_initial(self.host.root, receipt, operation_id=self.operation)
        self.assertEqual(Configuration(store).get('host-runtime.json')['revision'], 2)

    def test_commit_requires_selection_matching_receipt_and_unchanged_assets(self):
        receipt = self.prepare()
        self.journal['stage'] = 'validated'; self.save_journal()
        with self.assertRaisesRegex(MigrationError, 'initial_host_update_not_selected'):
            initial.commit_initial(self.host.root, receipt, operation_id=self.operation)
        self.select(receipt)
        wrong = {**receipt, 'image': 'sha256:' + 'f' * 64}
        with self.assertRaisesRegex(MigrationError, 'initial_host_update_receipt_changed'):
            initial.commit_initial(self.host.root, wrong, operation_id=self.operation)
        (self.host.root / 'runner-seccomp.json').write_text('{}')
        with self.assertRaisesRegex(MigrationError, 'initial_host_update_assets_changed'):
            initial.commit_initial(self.host.root, receipt, operation_id=self.operation)

    def test_prepare_requires_exact_journal_source_and_quiet_writers(self):
        self.journal['initial_source']['to_revision'] = 'c' * 40; self.save_journal()
        with self.assertRaisesRegex(MigrationError, 'initial_host_update_intent_changed'): self.prepare()
        self.assertTrue(all(argv[:2] == ['git', '-C'] for argv in self.host.calls))
        self.journal['initial_source']['to_revision'] = self.host.revision; self.save_journal()
        with patch.object(initial.Host, 'quiet', side_effect=MigrationError('writers_not_stopped')):
            with self.assertRaisesRegex(MigrationError, 'writers_not_stopped'): self.prepare()
        self.assertTrue(all(argv[:2] == ['git', '-C'] for argv in self.host.calls))


class InitialHostUpdateProtocolTests(unittest.TestCase):
    def invoke(self, action, *, fail=False):
        # A real child uses inherited FD 1 just like npm, Docker and systemctl.
        script = textwrap.dedent("""
            import json, os, sys
            from forge import host_initial_update as initial, host_update
            def reconcile(*args, **kwargs):
                print('python-progress')
                host_update.command([sys.executable, '-c',
                    r"import os; os.write(1, b'child-stdout\\n'); os.write(2, b'child-stderr\\n')"])
                if sys.argv[1] == 'fail':
                    raise RuntimeError('fixture failure')
                return {'version': 1, 'prepared_digest': 'qualified-fixture'}
            initial.prepare_initial = reconcile
            initial.commit_initial = reconcile
            try:
                initial.main()
            except RuntimeError:
                os.write(1, b'restored-protocol-fd\\n')
                sys.exit(3)
        """)
        request = dict(action=action, root='/unused', operation_id='fixture', source_revision='a' * 40)
        if action == 'commit_initial': request['receipt'] = {'fixture': True}
        return subprocess.run([sys.executable, '-c', script, 'fail' if fail else 'pass'],
                              input=json.dumps(request), capture_output=True, text=True, timeout=15)

    def test_prepare_and_commit_return_one_json_receipt_with_real_child_diagnostics_on_stderr(self):
        for action in ('prepare_initial', 'commit_initial'):
            with self.subTest(action=action):
                result = self.invoke(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout), {'version': 1, 'prepared_digest': 'qualified-fixture'})
                self.assertEqual(len(result.stdout.splitlines()), 1)
                self.assertEqual(set(result.stderr.splitlines()), {'python-progress', 'child-stdout', 'child-stderr'})

    def test_failed_preparation_emits_no_receipt_and_restores_protocol_descriptor(self):
        result = self.invoke('prepare_initial', fail=True)
        self.assertEqual(result.returncode, 3, result.stderr)
        self.assertEqual(result.stdout, 'restored-protocol-fd\n')
        self.assertEqual(set(result.stderr.splitlines()), {'python-progress', 'child-stdout', 'child-stderr'})
