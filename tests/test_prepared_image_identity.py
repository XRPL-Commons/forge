import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
import tarfile

from tools.build_release_artifacts import image_identity


class PreparedImageIdentityTests(unittest.TestCase):
    def archive(self, directory, *, architecture='amd64', wrong_config=False, corrupt_manifest=False):
        config = json.dumps({'os': 'linux', 'architecture': architecture}).encode()
        config_digest = hashlib.sha256(config).hexdigest()
        manifest = json.dumps({'schemaVersion': 2, 'config': {'digest': 'sha256:' + ('0'*64 if wrong_config else config_digest)}, 'layers': []}).encode()
        manifest_digest = hashlib.sha256(manifest).hexdigest()
        members = {
            'blobs/sha256/' + config_digest: config,
            'blobs/sha256/' + manifest_digest: manifest + (b' ' if corrupt_manifest else b''),
            'manifest.json': json.dumps([{'Config': 'blobs/sha256/' + config_digest}]).encode(),
            'index.json': json.dumps({'manifests': [{'digest': 'sha256:' + manifest_digest}]}).encode(),
        }
        path = Path(directory) / 'worker.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, raw in members.items():
                info = tarfile.TarInfo(name); info.size = len(raw)
                archive.addfile(info, io.BytesIO(raw))
        return path, 'sha256:' + config_digest, 'sha256:' + manifest_digest

    def test_identity_is_portable_across_builder_stores(self):
        with tempfile.TemporaryDirectory() as directory:
            path, config, manifest = self.archive(directory)
            expected = {'image_id': config, 'image_manifest_id': manifest}
            self.assertEqual(image_identity(path, config), expected)
            self.assertEqual(image_identity(path, manifest), expected)

    def test_rejects_export_from_another_built_image(self):
        with tempfile.TemporaryDirectory() as directory:
            path, _, _ = self.archive(directory)
            with self.assertRaises(ValueError): image_identity(path, 'sha256:' + 'f'*64)

    def test_rejects_wrong_platform_or_unbound_metadata(self):
        for change in ({'architecture': 'arm64'}, {'wrong_config': True}, {'corrupt_manifest': True}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                path, config, _ = self.archive(directory, **change)
                with self.assertRaises(ValueError): image_identity(path, config)


if __name__ == '__main__':
    unittest.main()
