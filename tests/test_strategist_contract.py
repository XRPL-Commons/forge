"""Contract tests for the read-only strategist role (deployment tracking)."""
import unittest

from forge.factory.contracts import strategist_note
from forge.factory.harness import schema


class StrategistContractTests(unittest.TestCase):
    def test_schema_lists_the_profile_fields(self):
        note = {'id': 'job-1', 'role': 'strategist', 'candidate': None}
        value = schema(note)
        self.assertEqual(set(value['required']), {'version', 'attempt', 'role', 'outcome', 'blocker', 'summary',
                                                  'strategy', 'deploy', 'domain', 'exposure', 'commit_source', 'proposal'})
        self.assertEqual(value['properties']['strategy']['enum'], ['github_deployments', 'health', 'header', 'unobservable'])
        self.assertEqual(value['properties']['exposure']['properties']['kind']['enum'], ['health', 'header', 'none'])
        self.assertFalse(value['additionalProperties'])

    def test_note_carries_read_only_instructions_and_head(self):
        note = strategist_note({'id': 'job-1', 'repo': 'x/y', 'repo_id': 7, 'head': 'a' * 40, 'deadline': 10})
        self.assertEqual(note['role'], 'strategist'); self.assertIsNone(note['candidate'])
        self.assertIn('Read only', note['instructions']); self.assertIn('X-Build-Commit', note['instructions'])
        self.assertEqual(note['head'], 'a' * 40); self.assertEqual(note['result_file'], '/work/result.json')


if __name__ == '__main__': unittest.main()
