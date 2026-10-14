"""Real npm and pip VCS installs through the credential-free Git endpoint."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import unittest

from forge.mirror.frontend import Frontend
from forge.mirror.git_store import environment, run_git
from forge.mirror.service import SessionServer
from tests import test_mirror_service as fixtures


class RefuseExternal(BaseHTTPRequestHandler):
    def do_CONNECT(self):
        self.send_error(403, 'GitHub HTTP is unavailable; use the Git mirror')
    def log_message(self, *_):
        pass


class PackageMirrorTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.SmartHTTPTests('test_real_smart_http_clone_fetch_no_write_and_live_revocation')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        files = {'package.json': json.dumps({'name': 'forge-mirror-fixture', 'version': '1.0.0', 'main': 'index.js'}),
                 'index.js': 'module.exports = "mirrored";\n',
                 'pyproject.toml': '[build-system]\nrequires=[]\nbuild-backend="fixturebackend"\nbackend-path=["."]\n',
                 'fixturebackend.py': '''import zipfile
from pathlib import Path
def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "forge_mirror_fixture-1.0.0-py3-none-any.whl"
    files = {"forge_mirror_fixture.py": "VALUE = 'mirrored'\\n",
             "forge_mirror_fixture-1.0.0.dist-info/METADATA": "Metadata-Version: 2.1\\nName: forge-mirror-fixture\\nVersion: 1.0.0\\n",
             "forge_mirror_fixture-1.0.0.dist-info/WHEEL": "Wheel-Version: 1.0\\nGenerator: fixture\\nRoot-Is-Purelib: true\\nTag: py3-none-any\\n"}
    files["forge_mirror_fixture-1.0.0.dist-info/RECORD"] = "".join(path+",,\\n" for path in files)
    with zipfile.ZipFile(Path(wheel_directory)/name, "w") as wheel:
        for path, content in files.items(): wheel.writestr(path, content)
    return name
'''}
        entries = []
        for name, content in sorted(files.items()):
            oid = run_git(['--git-dir', str(self.fixture.gitdir), 'hash-object', '-w', '--stdin'], data=content.encode()).decode().strip()
            entries.append(f'100644 blob {oid}\t{name}\n')
        tree = run_git(['--git-dir', str(self.fixture.gitdir), 'mktree'], data=''.join(entries).encode()).decode().strip()
        commit_env = {**environment(), 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.test',
                      'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.test'}
        self.head = run_git(['--git-dir', str(self.fixture.gitdir), 'commit-tree', tree, '-m', 'packages'], env=commit_env).decode().strip()
        run_git(['--git-dir', str(self.fixture.gitdir), 'update-ref', 'refs/heads/main', self.head])
        socket = self.root / 'packages.sock'
        broker = self.fixture.broker(self.fixture.cache)
        service = SessionServer(socket, broker)
        self.serve(service)
        frontend = Frontend(('127.0.0.1', 0), str(socket))
        self.serve(frontend)
        proxy = ThreadingHTTPServer(('127.0.0.1', 0), RefuseExternal)
        self.serve(proxy)
        home = self.root / 'home'; home.mkdir()
        config = home / 'gitconfig'
        config.write_text(f'[url "http://127.0.0.1:{frontend.server_port}/github/"]\n'
                          ' insteadOf = https://github.com/\n insteadOf = ssh://git@github.com/\n'
                          ' insteadOf = git@github.com:\n[protocol "file"]\n allow = never\n')
        self.env = {**environment(), 'HOME': str(home), 'GIT_CONFIG_GLOBAL': str(config),
                    'HTTP_PROXY': f'http://127.0.0.1:{proxy.server_port}', 'HTTPS_PROXY': f'http://127.0.0.1:{proxy.server_port}',
                    'NO_PROXY': '127.0.0.1,localhost', 'npm_config_cache': str(home / 'npm-cache'),
                    'npm_config_update_notifier': 'false', 'PIP_DISABLE_PIP_VERSION_CHECK': '1', 'PIP_NO_INDEX': '1'}
        self.env.update(http_proxy=self.env['HTTP_PROXY'], https_proxy=self.env['HTTPS_PROXY'], no_proxy=self.env['NO_PROXY'])

    def serve(self, server):
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        def close():
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        self.addCleanup(close)

    def command(self, argv, cwd):
        result = subprocess.run(argv, env=self.env, cwd=cwd, capture_output=True, text=True, timeout=90)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
        return result.stdout

    def assert_receipts(self):
        rows = self.fixture.store.receipts('aux:triage:1')
        self.assertTrue(any(row['details'].get('request_kind') == 'upload' and row['details']['outcome'] == 'served' for row in rows))
        self.assertNotIn('GH_TOKEN', self.env)

    @unittest.skipUnless(shutil.which('npm'), 'npm is required for the package compatibility canary')
    def test_npm_hosted_git_dependency_falls_back_to_the_mirror(self):
        project = self.root / 'npm-project'; project.mkdir()
        (project / 'package.json').write_text(json.dumps({'name': 'consumer', 'version': '1.0.0',
            'dependencies': {'forge-mirror-fixture': 'github:acme/project#' + self.head}}))
        self.command(['npm', 'install', '--ignore-scripts', '--no-audit', '--no-fund'], project)
        self.assertEqual((project / 'node_modules/forge-mirror-fixture/index.js').read_text(), 'module.exports = "mirrored";\n')
        self.assert_receipts()

    def test_pip_git_dependency_uses_the_same_unmodified_git_transport(self):
        python = os.environ.get('FORGE_PACKAGE_PYTHON', sys.executable)
        if subprocess.run([python, '-m', 'pip', '--version'], capture_output=True).returncode:
            self.skipTest('pip is required for the package compatibility canary')
        target = self.root / 'python-target'
        self.command([python, '-m', 'pip', 'install', '--no-deps', '--no-build-isolation', '--target', str(target),
                      'git+https://github.com/acme/project.git@' + self.head], self.root)
        self.assertEqual((target / 'forge_mirror_fixture.py').read_text(), "VALUE = 'mirrored'\n")
        self.assert_receipts()
