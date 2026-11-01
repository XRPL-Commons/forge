import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import Mock

from tools.prepare_release_publication import prepare

REVISION = 'a' * 40


def response(value, status=0):
    return subprocess.CompletedProcess([], status, json.dumps(value), '')


def draft(**changes):
    return {'tag_name': 'forge-build-' + REVISION, 'draft': True,
            'target_commitish': REVISION, 'author': {'login': 'github-actions[bot]'}, **changes}


class PublicationTests(unittest.TestCase):
    def test_missing_release_can_build_but_network_failure_cannot(self):
        self.assertEqual(prepare(REVISION, run=Mock(return_value=response({'status': '404'}, 1))), 'build')
        for value in ({'status': '503'}, {'message': 'Bad credentials'}):
            run = Mock(return_value=response(value, 1))
            with self.assertRaisesRegex(ValueError, 'lookup failed'):
                prepare(REVISION, run=run)
            self.assertEqual(run.call_count, 1)

    def test_published_release_is_never_modified_or_rebuilt(self):
        run = Mock(return_value=response(draft(draft=False)))
        self.assertEqual(prepare(REVISION, run=run), 'published')
        self.assertEqual(run.call_count, 1)

    def test_partial_owned_draft_is_replaced_without_deleting_tag(self):
        run = Mock(side_effect=[response(draft()), response({})])
        self.assertEqual(prepare(REVISION, run=run), 'build')
        self.assertEqual(run.call_args.args[0], ['gh', 'release', 'delete', 'forge-build-' + REVISION,
                                               '--repo', 'XRPL-Commons/forge', '--yes'])
        self.assertTrue(run.call_args.kwargs['capture_output'])

    def test_draft_requires_exact_tag_target_and_workflow_owner(self):
        for change in ({'tag_name': 'forge-build-' + 'b' * 40}, {'target_commitish': 'main'},
                       {'author': {'login': 'maintainer'}}, {'author': None}, {'draft': None}):
            run = Mock(return_value=response(draft(**change)))
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare(REVISION, run=run)
            self.assertEqual(run.call_count, 1)

    def test_runner_context_contains_runtime_startup_and_build_helper(self):
        root = Path(__file__).resolve().parents[1]
        rules = (root / '.dockerignore').read_text().splitlines()
        # These files are needed after COPY: Docker's successful source tests
        # cannot reveal that an allowlisted build context omitted them.
        for filename in ('forge/factory/startup.py', 'forge/harnesses/claude/prepare_launch.py'):
            self.assertIn('!' + filename, rules)
            self.assertTrue((root / filename).is_file())


if __name__ == '__main__':
    unittest.main()
