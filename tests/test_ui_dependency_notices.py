import json
import hashlib
from pathlib import Path
import tempfile
import unittest

from tools.ui_dependency_notices import write_notices


class NoticeTests(unittest.TestCase):
    def fixture(self, root):
        package = root / 'node_modules/example'
        package.mkdir(parents=True)
        (package / 'package.json').write_text(json.dumps({'name': 'example', 'version': '1.0.0',
                                                        'license': 'MIT', 'repository': 'https://example.com/source'}))
        (root / 'package-lock.json').write_text(json.dumps({'packages': {'': {}, 'node_modules/example': {
            'version': '1.0.0', 'resolved': 'https://registry.npmjs.org/example/-/example-1.0.0.tgz', 'integrity': 'sha512-x'},
            'node_modules/other-platform': {'version': '1.0.0', 'optional': True}}}))
        return package

    def test_collects_actual_notices_and_source_without_other_platforms(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.fixture(root)
            (package / 'LICENSE').write_text('Copyright Example. Permission granted.')
            (package / 'NOTICE').write_text('Embedded attribution')
            output = root / 'public/third-party-notices.txt'
            self.assertEqual(write_notices(root, output), [])
            text = output.read_text()
            for expected in ('example@1.0.0', 'Copyright Example', 'Embedded attribution',
                             'https://example.com/source', 'sha512-x'):
                self.assertIn(expected, text)
            self.assertNotIn('other-platform', text)

    def test_missing_text_is_explicit_and_keeps_readme_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.fixture(root)
            (package / 'README.md').write_text('Licensed under MIT by Example')
            output = root / 'notices.txt'
            self.assertEqual(write_notices(root, output), ['example@1.0.0'])
            self.assertIn('upstream review required', output.read_text())
            self.assertIn('Licensed under MIT by Example', output.read_text())

    def test_different_dependency_bytes_are_not_described_as_locked_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = self.fixture(root)
            (package / 'package.json').write_text('{"name":"example","version":"2.0.0"}')
            with self.assertRaisesRegex(ValueError, 'differs from its lock'):
                write_notices(root, root / 'notices.txt')

    def test_supplement_matches_version_and_hash_and_fills_missing_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            supplemental = root / 'third-party-licenses'
            supplemental.mkdir()
            text = b'Copyright and permission from exact upstream revision.'
            (supplemental / 'example.txt').write_bytes(text)
            entry = {'file': 'example.txt', 'sha256': hashlib.sha256(text).hexdigest(),
                     'packages': {'example': '1.0.0'}, 'source': 'https://example.com/pinned/LICENSE'}
            provenance = supplemental / 'provenance.json'
            provenance.write_text(json.dumps([entry]))
            output = root / 'notices.txt'
            self.assertEqual(write_notices(root, output), [])
            self.assertIn(text.decode(), output.read_text())
            self.assertIn(entry['source'], output.read_text())
            entry['packages']['example'] = '2.0.0'
            provenance.write_text(json.dumps([entry]))
            with self.assertRaisesRegex(ValueError, 'needs review'):
                write_notices(root, output)
            (supplemental / 'example.txt').write_text('changed')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                write_notices(root, output)

    def test_missing_redistributed_server_license_stops_publication(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.fixture(root)
            runtime = root / 'output/server/node_modules/example'
            runtime.mkdir(parents=True)
            (runtime / 'package.json').write_text('{"name":"example","version":"1.0.0"}')
            with self.assertRaisesRegex(ValueError, 'Redistributed server package'):
                write_notices(root, root / 'notices.txt', runtime=runtime.parent)
