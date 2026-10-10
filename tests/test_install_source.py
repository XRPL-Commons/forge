"""Real Git object transfer, with a local fixture standing in for GitHub."""
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from forge_client import _install_host as remote
from forge_client.source import export
from forge_client.github import run_bounded


class SourceTransferTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.original = self.directory / 'original'
        self.original.mkdir()
        self.git(self.original, 'init', '--quiet')
        (self.original / 'old-secret').write_text('synthetic-old-source-history')
        self.git(self.original, 'add', '.')
        self.commit('old')
        self.parent = self.git(self.original, 'rev-parse', 'HEAD')
        (self.original / 'old-secret').unlink()
        (self.original / 'README').write_text('Current pinned source\n')
        self.git(self.original, 'add', '-A')
        self.commit('current')
        self.revision = self.git(self.original, 'rev-parse', 'HEAD')
        self.git(self.original, 'config', 'fixture.operator-secret', 'synthetic-github-admin-only-local')

    def git(self, directory, *args):
        # Fixture commits must finish their own maintenance before temporary cleanup.
        return subprocess.run(['git', '-c', 'maintenance.autoDetach=false', '-C', str(directory), *args], check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, message):
        self.git(self.original, '-c', 'commit.gpgsign=false', '-c', 'user.name=Fixture',
                 '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', message)

    def local_run(self, argv, **kwargs):
        if 'fetch' in argv:
            argv = [str(self.original) if item == 'origin' else item for item in argv]
        return run_bounded(argv, **kwargs)

    def test_only_selected_commit_tree_reaches_new_host_without_config_or_history(self):
        value = export(self.revision, run=self.local_run)
        pack = base64.b64decode(value['data'])
        self.assertEqual(hashlib.sha256(pack).hexdigest(), value['sha256'])
        target = self.directory / 'installed'
        target.mkdir()
        self.git(target, 'init', '--quiet')
        subprocess.run(['git', '-C', str(target), 'index-pack', '--stdin'], input=pack, check=True, capture_output=True)
        (target / '.git/shallow').write_text(self.revision + '\n')
        self.git(target, 'checkout', '--detach', self.revision)
        self.assertEqual((target / 'README').read_text(), 'Current pinned source\n')
        self.assertFalse((target / 'old-secret').exists())
        self.assertEqual(self.git(target, 'rev-list', 'HEAD'), self.revision)
        self.assertNotEqual(subprocess.run(['git', '-C', str(target), 'cat-file', '-e', self.parent],
                                          capture_output=True).returncode, 0)
        self.assertNotIn('synthetic-github-admin-only-local', (target / '.git/config').read_text())

    def test_host_source_stage_imports_pack_without_a_network_fetch(self):
        value = export(self.revision, run=self.local_run)
        state = self.directory / 'state'; state.mkdir()
        (state / 'source.pack').write_bytes(base64.b64decode(value['data']))
        target = self.directory / 'installed'
        commands = []
        def service(argv, *, capture=False, input_file=None):
            commands.append(argv)
            self.assertNotIn('fetch', argv)
            result = subprocess.run(argv, stdin=input_file, capture_output=True, check=True)
            return result.stdout.decode().strip() if capture else None
        with patch.object(remote, 'SOURCE', target), patch.object(remote, 'STATE', state), \
                patch.object(remote, 'service', side_effect=service), patch.object(remote.os, 'fchown'):
            with patch.object(remote.pwd, 'getpwnam', return_value=type('Account', (), {'pw_uid': os.getuid(), 'pw_gid': os.getgid()})()):
                remote.install_step('source', {'intent': {'revision': self.revision}, 'source_sha256': value['sha256']})
        self.assertEqual(self.git(target, 'rev-parse', 'HEAD'), self.revision)
        self.assertEqual(self.git(target, 'remote', 'get-url', 'origin'), remote.REPOSITORY)
        self.assertFalse((target / 'old-secret').exists())

    def test_upgrade_transfers_forward_ancestry_without_remote_credentials_or_fetch(self):
        from forge.store.migration.host import Host
        from forge.store.migration.cutover import package_version
        module=self.original/'forge';module.mkdir()
        (module/'__init__.py').write_text('# public source fixture\n')
        self.git(self.original,'add','forge')
        self.commit('qualified target')
        target_revision=self.git(self.original,'rev-parse','HEAD')
        installed=self.directory/'upgrade';installed.mkdir()
        self.git(installed,'init','--quiet')
        old=export(self.revision,run=self.local_run)
        subprocess.run(['git','-C',str(installed),'index-pack','--stdin'],input=base64.b64decode(old['data']),check=True,capture_output=True)
        (installed/'.git/shallow').write_text(self.revision+'\n')
        self.git(installed,'checkout','--detach',self.revision)
        trace=self.directory/'source-transfer-trace.jsonl'
        pack=export(target_revision,previous=self.revision,run=self.local_run)
        commands=[]
        def run(argv,**kwargs):
            commands.append(argv)
            self.assertNotIn('fetch',argv)
            return subprocess.run(argv,env={**os.environ,'GIT_TRACE2_EVENT':str(trace)},**kwargs)
        host=Host(self.directory/'runtime',installed,run=run)
        host.identity=lambda:self.git(installed,'rev-parse','HEAD')
        host.prepare_source_update(self.revision,target_revision,package_version(self.original),source_pack=pack)
        self.assertEqual(host.identity(),self.revision)
        host.install_source_update(self.revision,target_revision)
        self.assertEqual(host.identity(),target_revision)
        self.assertEqual((installed/'forge/__init__.py').read_text(),'# public source fixture\n')
        self.assertNotIn('synthetic-github-admin-only-local',(installed/'.git/config').read_text())
        self.assertFalse((installed/'old-secret').exists())
        events=[json.loads(line) for line in trace.read_text().splitlines()]
        started=[event['argv'] for event in events if event['event']=='start']
        self.assertTrue(any('merge' in argv for argv in started))
        children=[event['argv'] for event in events if event['event']=='child_start']
        self.assertFalse(any('maintenance' in argv or 'gc' in argv for argv in children), children)

    def test_upgrade_export_rejects_unrelated_source_history(self):
        from forge_client.contracts import SetupError
        with self.assertRaisesRegex(SetupError,'Could not export'):
            export(self.revision,previous='f'*40,run=self.local_run)


if __name__ == '__main__':
    unittest.main()
