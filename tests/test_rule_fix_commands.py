import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge_client.rule_fixes import ruleset_command, classic_command, fixes
from forge_client.cli import main, repository_commands

CASES = json.loads((Path(__file__).parent / 'fixtures/ruleset-fix-commands.json').read_text())


class RuleFixTests(unittest.TestCase):
    def test_ui_cli_command_fixtures_and_visibility(self):
        for case in CASES:
            self.assertEqual(ruleset_command(case['repo'], case['source'], case['app_id']), case['command'])
            self.assertEqual(ruleset_command(case['repo'], {**case['source'], 'bypass_actors_visible': False}, case['app_id']), '')
        self.assertEqual(ruleset_command('example/project', dict(id=42, source_type='Enterprise', bypass_actors_visible=True), 123), '')
        self.assertEqual(ruleset_command('example/project;bad', CASES[0]['source'], 123), '')
        self.assertEqual(classic_command('example/project', 'main', 'controller', None), '')

    @unittest.skipUnless(shutil.which('jq'), 'Printed ruleset command requires jq')
    def test_printed_command_reads_current_rules_and_preserves_existing_actors(self):
        current = dict(name='Required reviews', target='branch', enforcement='active',
                       conditions={'ref_name': {'include': ['~DEFAULT_BRANCH'], 'exclude': []}},
                       rules=[{'type': 'pull_request', 'parameters': {'required_approving_review_count': 3}}],
                       bypass_actors=[{'actor_type': 'Team', 'actor_id': 77, 'bypass_mode': 'always'}])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = root / 'gh'
            fake.write_text('#!/usr/bin/env python3\nimport os,sys,pathlib\np=pathlib.Path(os.environ["FIXTURE_DIR"])\nif "PUT" in sys.argv:\n (p/"written.json").write_text(sys.stdin.read())\nelse:\n print((p/"current.json").read_text())\n')
            fake.chmod(0o700)
            environment = {**os.environ, 'PATH': directory + os.pathsep + os.environ['PATH'], 'FIXTURE_DIR': directory}
            for actors in [current['bypass_actors'], current['bypass_actors'] + [{'actor_type': 'Integration', 'actor_id': 123, 'bypass_mode': 'pull_request'}]]:
                observed = {**current, 'bypass_actors': actors}
                (root / 'current.json').write_text(json.dumps(observed))
                result = subprocess.run(['bash', '-c', CASES[0]['command']], env=environment, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode())
                written = json.loads((root / 'written.json').read_text())
                self.assertEqual({key: value for key, value in written.items() if key != 'bypass_actors'},
                                 {key: value for key, value in current.items() if key != 'bypass_actors'})
                self.assertEqual(written['bypass_actors'], current['bypass_actors'] + [{'actor_type': 'Integration', 'actor_id': 123, 'bypass_mode': 'pull_request'}])
            (root / 'written.json').unlink()
            (root / 'current.json').write_text(json.dumps({**current, 'bypass_actors': None}))
            result = subprocess.run(['bash', '-c', CASES[0]['command']], env=environment, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((root / 'written.json').exists(), 'hidden list must never cause a replacement write')

    def test_cli_prints_fixes_using_only_a_scoped_read(self):
        source = {**CASES[0]['source'], 'kind': 'ruleset', 'name': 'Required reviews', 'url': 'https://github.com/example/project/rules/42'}
        report = {'branch': 'main', 'bypass': {'status': 'missing', 'sources': [source], 'controller_app_id': 123}, 'requirements': []}
        client = Mock(); client.read.return_value = {'repository': {'name': 'example/project', 'protection': report}, 'connections': {}}
        github = Mock(); github.repository.return_value = {'id': 12, 'name': 'example/project'}
        with patch('forge_client.cli.Client', return_value=client), patch('forge_client.cli.GitHub', return_value=github), patch('forge_client.cli.connection_lock'), patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['repo', 'fix-rules', 'example/project', '--server', 'https://forge.example', '--json']), 0)
        client.read.assert_called_once_with({'id': 12, 'name': 'example/project'}, 'repository')
        client.operation.assert_not_called()
        github.create_baseline.assert_not_called()
        payload = json.loads(output.getvalue())
        self.assertEqual(payload['fixes'][0]['command'], CASES[0]['command'])
        self.assertTrue(payload['read_only'])
        self.assertEqual(repository_commands()['fix-rules']['capability'], 'repositories:read')
