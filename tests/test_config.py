import tempfile
from pathlib import Path
import unittest
from forge.config import load_repositories, supervisor_records


class ConfigTests(unittest.TestCase):
    def test_clone_paths_are_literal_and_rotation_preserves_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            for name in ('a', 'b'):
                Path(directory, name + '.repo').write_text(f'REPO="o/{name}"\nOWNER_LABEL="auto:owned-by-me"\nCLONE_BUILD="/tmp/literal $(touch marker)"\n')
            records = supervisor_records(directory, '/tmp/home with spaces', 1).splitlines()
            self.assertEqual(records[0].split('\t')[0], 'o/b')
            self.assertEqual(records[0].split('\t')[2], '/tmp/literal $(touch marker)')
            self.assertEqual(records[0].split('\t')[3], '/tmp/home with spaces/b-review')
            self.assertEqual(records[1].split('\t')[0], 'o/a')

    def test_invalid_config_cannot_smuggle_record_boundaries(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory, 'a.repo')
            base = 'REPO="o/a"\nOWNER_LABEL="auto:owned-by-me"\n'
            for extra in ('CLONE_BUILD="relative"\n', 'CLONE_BUILD="/tmp/a\tb"\n', 'RECOVERY_AUTHOR="me bad"\n', 'echo unsafe\n'):
                p.write_text(base + extra)
                with self.assertRaises(ValueError):
                    supervisor_records(directory, '/tmp/home')

    def test_all_configs_validated_before_records_are_returned(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'a.repo').write_text('REPO="o/a"\nOWNER_LABEL="auto:owned-by-me"\n')
            Path(directory, 'z.repo').write_text('REPO="o/z"\nOWNER_LABEL="auto:owned-by-me"\ntouch /tmp/no\n')
            with self.assertRaises(ValueError):
                supervisor_records(directory, '/tmp/home')
