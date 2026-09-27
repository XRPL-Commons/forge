"""Memory failures across real ledger restarts and simulated worker lifecycles."""
import copy
import unittest
from unittest.mock import patch, Mock

from forge.factory.config import validate
from forge.factory.contracts import assignment_note
from forge.factory.ledger import Ledger
from forge.factory.memory import allocation, confirmed, pending
from forge.factory.supervisor import Supervisor
from tests.test_factory_cycle import CycleTest, Settings


class AdaptiveMemoryTests(CycleTest):
    def setUp(self):
        super().setUp()
        self.config.update(memory='2g', memory_tiers=['2g', '4g', '6g'])

    def exhaust(self, job, detail='JavaScript heap out of memory'):
        report = {'version': 1, 'attempt': job['id'], 'role': job['role'],
                  'outcome': 'blocked_environment', 'blocker': 'resources',
                  'summary': detail, 'headline': 'Memory exhausted', 'plain': 'Build exceeded its heap limit.'}
        if job['candidate'] and job['role'] in ('review', 'audit'):
            report['candidate'] = job['candidate']['key']
        self.engine.finish(job, report)

    def test_two_escalations_then_hold_without_investigation_or_repeated_retries(self):
        self.cycle()
        first = self.active()
        branch = first['branch']
        for size in ['2g', '4g', '6g']:
            job = self.active()
            self.assertEqual(job['runner_config']['memory'], size)
            self.assertEqual(job['branch'], branch)
            self.exhaust(job)
            self.assertFalse(self.cycle()['errors'])
        for _ in range(3): self.cycle()
        data = self.ledger.read()
        self.assertEqual(len(data['jobs']), 3)
        self.assertEqual(data['issues'][self.key]['state'], 'hold')
        self.assertEqual(data['issues'][self.key]['failures'], [])
        self.assertFalse(data['slots'])

    def test_oom_kill_without_report_escalates(self):
        self.cycle(); job = self.active()
        original = self.engine.capture
        self.engine.capture = lambda j: {**original(j), 'execution': {'oom_killed': j['id'] == job['id']}}
        self.engine.finish(job)
        self.cycle()
        self.assertEqual(self.active()['runner_config']['memory'], '4g')
        self.assertEqual(assignment_note(self.active())['resume_branch'], job['branch'])

    def test_generic_resources_and_log_text_do_not_trigger_memory_retry(self):
        self.assertFalse(confirmed({'logs': 'JavaScript heap out of memory'}))
        self.cycle(); self.exhaust(self.active(), 'Dependency storage unavailable')
        self.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['state'], 'hold')
        self.assertEqual(len(self.ledger.read()['jobs']), 1)

    def test_existing_memory_hold_and_ready_issue_use_larger_allocation(self):
        self.config['memory_tiers'] = []
        self.cycle(); self.exhaust(self.active()); self.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['state'], 'hold')
        self.config['memory_tiers'] = ['2g', '4g', '6g']
        self.cycle()
        self.assertEqual(self.active()['runner_config']['memory'], '4g')

    def test_success_remembers_repo_and_role_and_keeps_review_at_default(self):
        self.cycle(); self.exhaust(self.active()); self.cycle()
        bigger = self.active()
        self.github.publish(bigger); self.finish('build'); self.cycle()
        self.assertEqual(self.active()['role'], 'review')
        self.assertEqual(self.active()['runner_config']['memory'], '2g')
        data = Ledger(self.temp.name).read()
        item = data['issues'][self.key]
        self.assertNotIn('memory_retry', item)
        other = {**item, 'key': '1:99', 'number': 99}
        self.assertEqual(allocation(self.config, data, other, 'build'), '4g')
        self.assertEqual(allocation(self.config, data, {**other, 'repo_id': 2}, 'build'), '2g')

    def test_review_retry_preserves_candidate_and_stage(self):
        self.cycle(); self.github.publish(self.active()); self.finish('build'); self.cycle()
        review = self.active(); self.exhaust(review); self.cycle()
        retry = self.active()
        self.assertEqual((retry['role'], retry['runner_config']['memory']), ('review', '4g'))
        self.assertEqual(retry['candidate'], review['candidate'])
        self.assertFalse(self.ledger.read()['issues'][self.key]['failures'])

    def test_escalation_waits_for_capacity_without_backfilling(self):
        self.cycle(); first = self.active(); self.exhaust(first)
        self.engine.capacity = lambda job=None: {'allowed': False, 'reason': 'memory_headroom'}
        self.cycle()
        self.ledger.discover({'id': 2, 'name': 'example/other'}, 2, 'requirements')
        for _ in range(2): self.cycle()
        self.assertEqual(len(self.ledger.read()['jobs']), 1)
        self.assertEqual(len(pending(self.ledger.read(), self.config)), 1)
        self.engine.capacity = lambda job=None: {'allowed': True}
        self.cycle()
        jobs = sorted((j for j in self.ledger.read()['jobs'].values() if j['id'] != first['id']), key=lambda j: j['created_at'])
        self.assertEqual(jobs[0]['issue_key'], self.key)
        self.assertEqual(jobs[0]['runner_config']['memory'], '4g')

    def test_oversized_escalation_holds_without_starving_other_issues_forever(self):
        self.cycle(); self.exhaust(self.active())
        self.engine.capacity = lambda job=None: {'allowed': False, 'reason': 'memory_budget', 'total_bytes': 5*1024**3}
        self.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['state'], 'hold')
        self.assertFalse(pending(self.ledger.read(), self.config))

    def test_cancelled_and_operator_held_issues_are_not_escalated(self):
        self.cycle(); job = self.active()
        self.ledger.hold(self.key, 'Operator paused', fence_active=True)
        self.assertFalse(self.ledger.retry_memory(job['id'], self.config, 'OOM'))
        self.assertFalse(pending(self.ledger.read(), self.config))

    def test_known_failure_floor_survives_manual_environment_resume(self):
        self.config['memory_tiers'] = []
        self.cycle(); self.exhaust(self.active()); self.cycle()
        self.assertTrue(self.ledger.resume_environment(self.key, 'Operator repaired resources'))
        self.config['memory_tiers'] = ['2g','4g','6g']
        data = self.ledger.read()
        self.assertEqual(allocation(self.config, data, data['issues'][self.key], 'build'), '4g')
        self.cycle()
        self.assertEqual(self.active()['resume_branch'], next(iter(data['jobs'].values()))['branch'])

    def test_deployment_investigation_defers_to_larger_pipeline_retry(self):
        from forge.deployments import Deployments
        from pathlib import Path
        self.cycle(); self.exhaust(self.active())
        self.engine.capacity = lambda job=None: {'allowed': False, 'reason':'memory_headroom'}
        self.cycle()
        root = Path(self.temp.name)
        # Deployments normally owns the parent of state/factory.
        fake_path = Mock()
        fake_path.__truediv__ = Mock(return_value=fake_path)
        fake_path.exists.return_value = True
        store = Deployments(root, runner=Mock(), github=Mock(), settings=Settings())
        with patch.object(store, 'root', fake_path), patch('forge.factory.ledger.Ledger', return_value=self.ledger), \
                patch.object(store, 'leased') as leased:
            store.start(self.config, {})
        leased.assert_not_called()

    def test_policy_disabled_does_not_escalate(self):
        self.config['memory_tiers'] = []
        self.cycle(); self.exhaust(self.active()); self.cycle()
        self.assertEqual(self.ledger.read()['issues'][self.key]['state'], 'hold')


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
