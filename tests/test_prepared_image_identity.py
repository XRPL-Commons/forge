"""New releases qualify workers but never export or publish their binaries."""
import io
import json
from pathlib import Path
import tempfile
import unittest
import tarfile
from unittest.mock import patch

from tools import build_release_artifacts as tool


class PreparedBuildTests(unittest.TestCase):
    def test_v2_only_archives_ui_with_notices_and_records_worker_recipes(self):
        inputs = {name: str(index) * 64 for index, name in enumerate(('ui', 'runner', 'full'))}
        def ui_release(root):
            ui = root / 'ui/releases/test/server'
            ui.mkdir(parents=True)
            (ui / 'index.mjs').write_text('server')
            return 'test'
        def notices(root, target, **kwargs):
            target.parent.mkdir(parents=True)
            target.write_text('dependency attribution')
        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(tool.platform, 'system', return_value='Linux'), \
                patch.object(tool.platform, 'machine', return_value='x86_64'), \
                patch.object(tool.host_update, 'source_revision', return_value='a' * 40), \
                patch.object(tool.subprocess, 'check_output', return_value=b''), \
                patch.object(tool.subprocess, 'run') as run, \
                patch.object(tool.host_update, 'runner_image', return_value='runner'), \
                patch.object(tool.host_update, 'full_runner_image', return_value='full'), \
                patch.object(tool.host_update, 'qualify_runner') as qualify, \
                patch.object(tool.host_update, 'ui_release', side_effect=ui_release), \
                patch.object(tool.host_update, 'artifact_inputs', return_value=inputs), \
                patch.object(tool.urllib.request, 'urlopen', return_value=io.BytesIO(b'{}')), \
                patch.object(tool.runpy, 'run_path', return_value={'generate': lambda *args: {}}), \
                patch.object(tool, 'write_notices', side_effect=notices), \
                patch.dict(tool.os.environ, {}):
            destination = Path(temporary) / 'assets'
            tool.build(destination)
            manifest = json.loads((destination / 'manifest.json').read_text())
            self.assertEqual(manifest['version'], 2)
            self.assertEqual(set(manifest['artifacts']), {'ui'})
            self.assertEqual(manifest['workers'], {kind: {'delivery': 'local-build', 'inputs': inputs[kind]}
                                                  for kind in ('runner', 'full')})
            self.assertEqual({p.name for p in destination.iterdir()}, {'manifest.json', tool.ASSETS['ui']})
            self.assertEqual(qualify.call_count, 2)
            self.assertFalse(any('save' in call.args[0] for call in run.call_args_list))
            with tarfile.open(destination / tool.ASSETS['ui']) as archive:
                self.assertEqual(archive.extractfile('public/third-party-notices.txt').read(), b'dependency attribution')
