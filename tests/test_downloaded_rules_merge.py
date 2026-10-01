"""Generated rules are enforced by mock GitHub through a full factory cycle."""
import unittest

from forge.repository_setup import ruleset
from tests.test_factory_cycle import CycleTest
from tests.test_repository_rules import RepositoryRuleTests


class DownloadedRulesMergeTests(unittest.TestCase):
    def test_imported_rules_allow_controller_merge_and_missing_bypass_pauses(self):
        cycle = CycleTest()
        cycle.setUp()
        self.addCleanup(cycle.doCleanups)
        downloaded = ruleset('main', 4959034)
        effective = [{**r, 'ruleset_id': 12, 'ruleset_source': 'example/project',
                      'ruleset_source_type': 'Repository'} for r in downloaded['rules']]
        reader = RepositoryRuleTests().reader(None, effective, bypass='never')
        read = reader.call.side_effect
        applied_actors = []
        def github_read(role, item, method, suffix, payload=None):
            if '/rulesets/' in suffix:
                can_bypass = any(a == {'actor_id': 4959034, 'actor_type': 'Integration',
                                      'bypass_mode': 'pull_request'} for a in applied_actors)
                return {'id': 12, 'current_user_can_bypass': 'pull_requests_only' if can_bypass else 'never'}
            return read(role, item, method, suffix, payload)
        reader.call.side_effect = github_read
        cycle.github.protections = reader.protections
        cycle.cycle()
        self.assertEqual(cycle.state(), 'hold')
        self.assertFalse(cycle.engine.jobs)
        applied_actors.extend(downloaded['bypass_actors'])
        self.assertFalse(cycle.cycle()['errors'])
        cycle.github.publish(cycle.active())
        cycle.finish('build')
        cycle.cycle(); cycle.finish('review')
        cycle.cycle(); cycle.finish('audit')
        self.assertFalse(cycle.cycle()['errors'])
        self.assertEqual(cycle.state(), 'merged')
        self.assertTrue(cycle.github.pull['merged'])
        self.assertEqual(cycle.ledger.read()['issues'][cycle.key]['failures'], [])
