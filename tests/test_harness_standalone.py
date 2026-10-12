import runpy
import unittest
from pathlib import Path

from forge.factory.delegation_contracts import PLANNING_OUTCOMES

HARNESS = Path(__file__).resolve().parent.parent / 'forge' / 'factory' / 'harness.py'


class StandaloneHarnessTests(unittest.TestCase):
    """The worker image copies harness.py alone, without the forge package."""

    def test_planning_schema_builds_without_the_package(self):
        harness = runpy.run_path(str(HARNESS))
        note = {'id': 'a', 'role': 'build', 'builder_stage': 'planning', 'input_manifest': {'manifest_version': 1}}
        schema = harness['schema'](note)
        self.assertEqual(schema['properties']['outcome']['enum'], list(PLANNING_OUTCOMES))

    def test_planning_outcomes_match_the_contract(self):
        self.assertEqual(runpy.run_path(str(HARNESS))['PLANNING_OUTCOMES'], PLANNING_OUTCOMES)


if __name__ == '__main__':
    unittest.main()
