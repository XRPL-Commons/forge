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
        from forge.factory.source_inputs import create
        import json
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            note = {'id': 'attempt', 'repo': 'example/project', 'publisher': 'build', 'role': 'build',
                    'branch': 'forge/retry', 'resume_branch': 'forge/retry', 'resume_head': 'b'*40,
                    'candidate': {'head': 'a'*40}, 'provider': 'codex'}
            note['source_input'] = create(repository_id=1, canonical_repository=note['repo'], base_oid='c'*40,
                input_oids=['b'*40], manifest={'base_oid':'c'*40}, dependency_policy_digest='d'*64, notes=note)
            assignment = root/'assignment.json'; assignment.write_text(json.dumps(note))
            def paths(value):
                return root/value.removeprefix('/work/') if value.startswith('/work/') else Path(value)
            commands = []
            def run(argv, **kwargs):
                commands.append(argv)
                return Mock(returncode=0, stderr='', stdout='')
            agent = Mock(return_value=0)
            with patch.object(harness, 'Path', side_effect=paths), \
                    patch.dict(harness.os.environ, {'FORGE_ASSIGNMENT': str(assignment)}, clear=True), \
                    patch.object(harness.subprocess, 'run', side_effect=run), \
                    patch.object(harness, 'prepare_environment', return_value={}), \
                    patch.object(harness.runpy, 'run_path', return_value={'adapter': lambda _: {'run': agent}}):
                self.assertEqual(harness.main(), 0)
                agent.assert_called_once()
                self.assertEqual({c[-1] for c in commands if 'fetch' in c}, {'b'*40, 'c'*40})
                self.assertTrue(any(c[-3:] == ['checkout','--detach','b'*40] for c in commands))
                self.assertFalse(any('ls-remote' in c or 'FETCH_HEAD' in c or 'a'*40 in c for c in commands))
                # A changed remote branch cannot widen this assignment's source
                # authority. The host must approve and pin a new resume head.
                note['resume_head'] = 'e'*40
                assignment.write_text(json.dumps(note))
                commands.clear(); agent.reset_mock()
                with self.assertRaisesRegex(harness.PreparationError, 'outside the frozen source inputs'):
                    harness.main()
                agent.assert_not_called()
                self.assertFalse(any('fetch' in c or 'checkout' in c for c in commands))
