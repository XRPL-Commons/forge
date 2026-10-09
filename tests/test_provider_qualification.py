"""Checksums gate executable use before Docker starts the offline spike."""
import base64
import hashlib
import tempfile
from pathlib import Path
import unittest

from deploy.qualify_optional_harnesses import verify_artifacts


class QualificationArtifactTests(unittest.TestCase):
    def test_both_artifacts_must_match_the_platform_pin(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            opencode, muse = directory / 'opencode', directory / 'muse'
            opencode.write_bytes(b'synthetic-opencode-archive')
            muse.write_bytes(b'synthetic-muse-binary')
            pins = {'opencode': {'platforms': {'linux-arm64': {
                'integrity': 'sha512-' + base64.b64encode(hashlib.sha512(opencode.read_bytes()).digest()).decode()}}},
                    'muse': {'platforms': {'linux-arm64': {
                'sha256': hashlib.sha256(muse.read_bytes()).hexdigest()}}}}
            verify_artifacts(opencode, muse, 'linux-arm64', pins)
            for artifact, provider in [(opencode, 'OpenCode'), (muse, 'Muse')]:
                original = artifact.read_bytes()
                artifact.write_bytes(original + b'changed')
                with self.subTest(provider=provider), self.assertRaisesRegex(ValueError, provider):
                    verify_artifacts(opencode, muse, 'linux-arm64', pins)
                artifact.write_bytes(original)
