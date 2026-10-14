import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('check_public_source', Path(__file__).resolve().parents[1] / 'tools/check_public_source.py')
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


class PublicSourceCheckTests(unittest.TestCase):
    def scan(self, text):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'doc.md').write_text(text)
            return check.content_findings(Path(directory), ['doc.md'])

    def test_private_host_details_are_rejected_without_printing_contents(self):
        secret = 'tail' + '1234ab'
        for text in ('/home/' + 'alice/src', 'https://forge.' + secret + '.ts.net', '/Users/' + 'alice/Library'):
            findings = self.scan('ok\n' + text + '\n')
            self.assertEqual(len(findings), 1, text)
            self.assertTrue(findings[0].startswith('doc.md:2: '))
            self.assertNotIn('alice', findings[0])
            self.assertNotIn(secret, findings[0])

    def test_documented_placeholders_are_allowed(self):
        self.assertEqual(self.scan('/home/forge/forge\nhttps://forge.example.ts.net\n`*.ts.net`\n'
                                   '/Users/you/Library\n'), [])

    def test_tracked_runtime_artifacts_are_rejected(self):
        self.assertEqual(len(check.path_findings(['state/x.json', 'a.pem', 'ui/tests/e2e/fixtures/state.json', 'README.md'])), 2)


if __name__ == '__main__':
    unittest.main()
