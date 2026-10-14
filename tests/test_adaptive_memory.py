"""Memory tier configuration and resumed worker branches."""
import unittest
from unittest.mock import patch, Mock

from forge.factory.config import validate


class MemoryConfigTests(unittest.TestCase):
    def test_invalid_tiers_are_rejected(self):
        for tiers in [['4g','2g'], ['2g','2048m'], ['4g'], ['2g','bad'], '2g', [2,4]]:
            with self.subTest(tiers=tiers), self.assertRaises(ValueError):
                validate({'memory':'2g','memory_tiers':tiers})
        self.assertEqual(validate({'memory':'2g','memory_tiers':['2g','4g','6g']})['memory_tiers'], ['2g','4g','6g'])


class ResumeHarnessTests(unittest.TestCase):
    def test_memory_retry_prefers_published_branch_over_old_candidate(self):
        from forge.factory import harness
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            note = {'id': 'attempt', 'repo': 'example/project', 'publisher': 'build', 'role': 'build',
                    'branch': 'forge/retry', 'resume_branch': 'forge/retry',
                    'candidate': {'head': 'a'*40}, 'provider': 'codex'}
            assignment = root/'assignment.json'; assignment.write_text(json.dumps(note))
            def paths(value):
                return root/value.removeprefix('/work/') if value.startswith('/work/') else Path(value)
            commands = []
            def run(argv, **kwargs):
                commands.append(argv)
                return Mock(returncode=0, stderr='', stdout=('b'*40+' refs/heads/forge/retry') if 'ls-remote' in argv else '')
            with patch.object(harness, 'Path', side_effect=paths), \
                    patch.dict(harness.os.environ, {'FORGE_ASSIGNMENT': str(assignment), 'GH_TOKEN':'fixture'}), \
                    patch.object(harness.subprocess, 'run', side_effect=run), \
                    patch.object(harness, 'prepare_environment', return_value={}), \
                    patch.object(harness.runpy, 'run_path', return_value={'adapter': lambda _: {'run': lambda *args: 0}}):
                self.assertEqual(harness.main(), 0)
            self.assertTrue(any(c[-3:] == ['fetch','origin','refs/heads/forge/retry'] for c in commands))
            self.assertTrue(any(c[-3:] == ['checkout','--detach','FETCH_HEAD'] for c in commands))
            self.assertFalse(any('a'*40 in c for c in commands))
