import hashlib
import io
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge import host_update
from forge import release_artifacts as artifacts

REVISION = 'a' * 40
INPUTS = {name: str(index) * 64 for index, name in enumerate(artifacts.ASSETS)}
BASE, FULL = 'sha256:' + 'b' * 64, 'sha256:' + 'c' * 64


def manifest(payload=b'archive'):
    return {'version': 1, 'source': REVISION, 'platform': 'linux/amd64', 'node_image': 'node@sha256:fixed',
            'python_image': 'python@sha256:fixed', 'artifacts': {
                kind: {'filename': filename, 'sha256': hashlib.sha256(payload).hexdigest(), 'size': len(payload),
                       'inputs': INPUTS[kind], **({'image_id': BASE if kind == 'runner' else FULL} if kind != 'ui' else {})}
                for kind, filename in artifacts.ASSETS.items()}}


def validated(value):
    return artifacts.validate_manifest(value, REVISION, INPUTS, 'node@sha256:fixed', 'python@sha256:fixed')


class ManifestTests(unittest.TestCase):
    def test_exact_source_platform_pins_and_inputs_are_required(self):
        self.assertEqual(validated(manifest())['source'], REVISION)
        for field, value in [('source', 'd' * 40), ('platform', 'linux/arm64'), ('node_image', 'node:latest')]:
            candidate = manifest(); candidate[field] = value
            with self.subTest(field=field), self.assertRaises(artifacts.ArtifactError):
                validated(candidate)
        candidate = manifest(); candidate['artifacts']['runner']['inputs'] = 'e' * 64
        with self.assertRaisesRegex(artifacts.ArtifactError, 'build inputs differ'):
            validated(candidate)

    def test_descriptors_cannot_choose_another_download_source_or_unpinned_image(self):
        for field, value in [('filename', 'https://attacker.invalid/archive'), ('image_id', 'forge:latest'),
                             ('size', True), ('size', 2 * 1024**3)]:
            candidate = manifest(); candidate['artifacts']['runner'][field] = value
            with self.subTest(field=field), self.assertRaises(artifacts.ArtifactError):
                validated(candidate)

    def test_source_fallback_is_explicit_and_skips_downloads(self):
        with patch.dict('os.environ', {'FORGE_BUILD_FROM_SOURCE': '1'}), patch.object(artifacts, 'prepared') as prepared:
            self.assertIsNone(host_update.prepared_release())
            prepared.assert_not_called()
        with patch.dict('os.environ', {'FORGE_BUILD_FROM_SOURCE': 'yes'}):
            with self.assertRaises(artifacts.ArtifactError): artifacts.source_build()

    def test_missing_manifest_does_not_build_from_source(self):
        with patch.object(artifacts.urllib.request, 'urlopen', side_effect=artifacts.urllib.error.URLError('release not published')):
            with self.assertRaisesRegex(artifacts.ArtifactError, 'Wait for its Prepared release workflow'):
                artifacts.read_manifest(REVISION)

    def test_unsupported_platform_is_explicit(self):
        artifacts.prepared.cache_clear()
        with patch.object(artifacts.platform, 'machine', return_value='aarch64'), patch.object(artifacts, 'read_manifest') as fetch:
            with self.assertRaisesRegex(artifacts.ArtifactError, 'Linux amd64'):
                artifacts.prepared(REVISION, INPUTS['runner'], INPUTS['full'], INPUTS['ui'], 'node', 'python')
            fetch.assert_not_called()


class ConsumptionTests(unittest.TestCase):
    def test_corrupt_download_is_never_loaded_into_docker(self):
        release = artifacts.Release(REVISION, manifest())
        run = Mock(return_value=subprocess.CompletedProcess([], 1, '', ''))
        with patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(b'corrupt')):
            with self.assertRaisesRegex(artifacts.ArtifactError, 'checksum mismatch'):
                release.image('runner', run=run)
        self.assertFalse(any('load' in call.args[0] for call in run.call_args_list))

    def test_prepared_image_is_downloaded_once_then_checked_by_full_image_id_and_platform(self):
        release = artifacts.Release(REVISION, manifest())
        run = Mock(side_effect=[subprocess.CompletedProcess([], 1, '', ''), subprocess.CompletedProcess([], 0, '', ''),
                              subprocess.CompletedProcess([], 0, BASE + ' linux/amd64\n', '')])
        with patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(b'archive')) as fetch:
            self.assertEqual(release.image('runner', run=run), BASE)
        self.assertEqual(fetch.call_args.args[0], artifacts.asset_url(REVISION, artifacts.ASSETS['runner']))
        self.assertEqual(run.call_args_list[1].args[0][:3], ['docker', 'image', 'load'])
        cached = Mock(return_value=subprocess.CompletedProcess([], 0, BASE + ' linux/amd64\n', ''))
        with patch.object(release, 'download') as download:
            self.assertEqual(release.image('runner', run=cached), BASE)
            download.assert_not_called()
        wrong = Mock(return_value=subprocess.CompletedProcess([], 0, BASE + ' linux/arm64\n', ''))
        with self.assertRaisesRegex(artifacts.ArtifactError, 'image ID or platform'):
            release.image('runner', run=wrong)

    def ui_archive(self, *, traversal=False):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:gz') as bundle:
            entry = tarfile.TarInfo('../escape' if traversal else 'server/index.mjs')
            payload = b'export default {}'
            entry.size = len(payload)
            bundle.addfile(entry, io.BytesIO(payload))
        return stream.getvalue()

    def test_ui_is_extracted_only_after_checksum_validation(self):
        payload = self.ui_archive(); release = artifacts.Release(REVISION, manifest(payload))
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / 'ui'
            with patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
                release.ui(target)
            self.assertEqual((target / 'server/index.mjs').read_bytes(), b'export default {}')
            with patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaises(artifacts.ArtifactError): release.ui(target)
            self.assertEqual((target / 'server/index.mjs').read_bytes(), b'export default {}')

    def test_ui_archive_cannot_write_outside_its_release(self):
        payload = self.ui_archive(traversal=True); release = artifacts.Release(REVISION, manifest(payload))
        with tempfile.TemporaryDirectory() as temporary, patch.object(artifacts.urllib.request, 'urlopen', return_value=io.BytesIO(payload)):
            target = Path(temporary) / 'ui'
            with self.assertRaises(tarfile.FilterError): release.ui(target)
            self.assertFalse(target.exists())
            self.assertFalse((Path(temporary) / 'escape').exists())

    def test_host_uses_prepared_images_and_ui_without_building(self):
        release = Mock(manifest=manifest())
        release.image.side_effect = [BASE, FULL]
        def unpack(target):
            (target / 'server').mkdir(parents=True)
            (target / 'server/index.mjs').write_text('prepared')
        release.ui.side_effect = unpack
        with tempfile.TemporaryDirectory() as temporary, patch.object(host_update, 'prepared_release', return_value=release), patch.object(host_update, 'command') as command:
            self.assertEqual(host_update.runner_image(), BASE)
            self.assertEqual(host_update.full_runner_image(BASE), FULL)
            key = host_update.ui_release(Path(temporary))
            self.assertEqual((Path(temporary) / 'ui/releases' / key / 'server/index.mjs').read_text(), 'prepared')
            command.assert_not_called()
            self.assertEqual([call.args[0] for call in release.image.call_args_list], ['runner', 'full'])

    def test_full_image_is_lazy_and_requires_the_release_base(self):
        release = Mock(manifest=manifest())
        with patch.object(host_update, 'prepared_release', return_value=release):
            host_update.runner_image()
            release.image.assert_called_once()
            with self.assertRaisesRegex(ValueError, 'selected base image'):
                host_update.full_runner_image('sha256:' + 'd' * 64)
            release.image.assert_called_once()
