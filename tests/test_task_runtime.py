"""Task execution boundaries do not grant delivery publication authority."""
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from forge.factory import harness
from forge.factory.config import validate
from forge.factory.contracts import result
from forge.factory.delegation_contracts import assignment, read_only_build
from forge.factory.dispatch_controller import combined_environment
from forge.factory.execution_controller import ExecutionController
from forge.factory.delegation_artifacts import verify_bundle
from forge.factory.github import GitHub
from forge.factory.runner import ContainerRunner
from forge.factory.result_controller import ResultController


class TaskContractTests(unittest.TestCase):
    def job(self, executor='owner'):
        return {'id': 'task-job', 'role': 'build', 'builder_id': 'owner',
            'builder_stage': 'work' if executor == 'owner' else 'child',
            'task': {'id': 'task', 'executor': executor, 'scope': 'Shared implementation'},
            'input_manifest': {'manifest_version': 2, 'scope': 'Shared implementation'},
            'members': [{'member_id': 'a'}, {'member_id': 'b'}]}

    def test_versioned_schema_describes_overlapping_and_owner_task_scopes(self):
        schema = harness.schema({**self.job(), 'builder_stage': 'planning'})
        plan = schema['properties']['delegation_plan']['anyOf'][1]
        self.assertEqual(plan['properties']['version']['enum'], [2])
        self.assertEqual(set(plan['required']), {'version', 'retained_task_ids', 'tasks', 'owner_final'})
        task = plan['properties']['tasks']['items']
        self.assertEqual(task['properties']['executor']['enum'], ['owner', 'child'])
        self.assertIn('member_ids', task['required'])
        self.assertNotIn('maxItems', plan['properties']['tasks'])
        self.assertNotIn('minItems', plan['properties']['tasks'])  # owner-only is valid

    def test_owner_and_child_outputs_require_exact_commit_and_cannot_publish(self):
        github = object.__new__(GitHub)
        github.pulls = Mock(side_effect=AssertionError('task attempted publication lookup'))
        for executor in ('owner', 'child'):
            job = self.job(executor)
            report = {'version': 1, 'role': 'build', 'attempt': job['id'], 'outcome': 'pass',
                'summary': 'Implemented', 'delegation_plan': None, 'output_commit': 'a' * 40}
            self.assertEqual(result(report, job)['output_commit'], 'a' * 40)
            with self.assertRaises(ValueError):
                result({**report, 'output_commit': None}, job)
            self.assertTrue(read_only_build(job))
            self.assertIsNone(github.publication(job, [], 'policy'))
            verification = {'what': 'Check the device', 'why': 'Needs hardware', 'checks': []}
            held = result({**report, 'outcome': 'needs_human_verification', 'verification': verification}, job)
            self.assertEqual(held['output_commit'], 'a' * 40)
        github.pulls.assert_not_called()
        self.assertFalse(read_only_build({'builder_stage': 'integration'}))

    def test_planning_advertises_and_validates_plans_instead_of_implementation_results(self):
        planning = {**self.job(), 'task': None, 'builder_stage': 'planning'}
        outcomes = harness.schema(planning)['properties']['outcome']['enum']
        self.assertEqual(set(outcomes), {'delegate', 'needs_decision', 'waiting_on_issues',
                                        'blocked_environment', 'unable', 'wait', 'reject'})
        report = dict(version=1, role='build', attempt=planning['id'], summary='Planned',
                      outcome='delegate', delegation_plan={'version': 2, 'tasks': [], 'retained_task_ids': [],
                      'owner_final': {'member_ids': ['a', 'b'], 'scope': 'Implement', 'internalizes': []}}, output_commit=None)
        self.assertEqual(result(report, planning)['outcome'], 'delegate')
        for invalid in ('pass', 'needs_human_verification'):
            with self.subTest(outcome=invalid), self.assertRaises(ValueError):
                result({**report, 'outcome': invalid, 'delegation_plan': None}, planning)
        for stage in ('work', 'child', 'integration'):
            note = {**self.job(), 'builder_stage': stage}
            self.assertIn('pass', harness.schema(note)['properties']['outcome']['enum'])
            self.assertIn('needs_human_verification', harness.schema(note)['properties']['outcome']['enum'])

    def test_integration_cannot_drop_task_verification_or_replace_human_check_with_ci(self):
        human = {'what': 'Verify physical device', 'why': 'Needs hardware', 'checks': []}
        ci = {'what': 'Run container check', 'why': 'Needs container host', 'checks': ['image-check']}
        job = {'id': 'integration', 'input_manifest': {'verification_requests': [
            {'output_id': 'owner-output', 'verification': human}, {'output_id': 'child-output', 'verification': ci}]},
            'verification_checks': [{'name': 'image-check', 'app_id': 7}]}
        job['verification_requests'] = job['input_manifest']['verification_requests']
        job['input_manifest']['verification_requests'] = [
            {'output_id': row['output_id'], 'result_digest': 'digest'} for row in job['verification_requests']]
        gate = ResultController._verification(job, {'outcome': 'pass'})
        self.assertEqual(gate['code'], 'verification_human')
        self.assertEqual(gate['details']['verification_job_id'], 'integration')
        self.assertNotIn('what', gate['details'])
        self.assertEqual(gate['details']['ci'], [])
        job['input_manifest']['verification_requests'] = [{'output_id': 'child-output', 'result_digest': 'digest'}]
        job['verification_requests'] = [{'output_id': 'child-output', 'verification': ci}]
        self.assertEqual(ResultController._verification(job, None)['code'], 'verification_ci')

    def test_task_prompt_and_owner_only_planning_have_distinct_authority(self):
        job = self.job()
        note = assignment({'instructions': 'Builders must commit their complete local implementation before requesting verification.'}, job)
        self.assertIn('this task cannot publish.', note['instructions'])
        self.assertIn('Do not push, open or edit a pull request', note['instructions'])
        self.assertIn('every pinned member', note['instructions'])
        self.assertIn('read-only', note['instructions'])
        planning = assignment({'instructions': ''}, {**job, 'task': None, 'builder_stage': 'planning'})
        self.assertIn('zero tasks', planning['instructions'])
        self.assertIn('owner_final', planning['instructions'])
        self.assertIn('internalize', planning['instructions'])

    @staticmethod
    def archive(name, content):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w') as archive:
            entry = tarfile.TarInfo(name); entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
        return stream.getvalue()

    def test_owner_task_capture_retains_harness_owned_bundle(self):
        runner = ContainerRunner(validate({}))
        runner.inspect = Mock(return_value={'id': 'owned-container', 'running': False})
        runner._scrub_values = Mock(return_value=[])
        bundle = b'bounded-git-bundle'
        runner.call = Mock(side_effect=[Mock(stdout=self.archive('result.json', b'{"outcome":"pass"}')),
            Mock(stdout=b''), Mock(stdout=self.archive('output.bundle', bundle))])
        capture = runner.capture(self.job())
        self.assertEqual(capture['worktree_bundle'], bundle)
        self.assertEqual(runner.call.call_args.args[0], ['cp', 'owned-container:/work/output.bundle', '-'])

    def test_real_execution_driver_keeps_github_credentials_out_of_both_task_executors(self):
        for executor in ('owner', 'child'):
            runner, github = Mock(), Mock()
            job = {**self.job(executor), 'cleanup': False, 'fenced': False, 'phase': 'starting',
                'launch_allowed': True, 'outcome': None, 'assignment_generation': 1, 'fencing_generation': 1,
                'assigned_membership': 1, 'membership_generation': 1, 'assigned_budget': 1, 'budget_generation': 1,
                'deadline': 1000, 'container_id': 'owned', 'execution_fence': 'lease', 'repo_id': 1,
                'captured_at': None}
            driver = ExecutionController(Mock(), runner, github, admission=lambda _: True, enabled=lambda: True, clock=lambda: 100)
            driver.queries = Mock(); driver.queries.job.return_value = job
            driver.queries.input_refs.return_value = []
            driver.commands = Mock(); driver._progress = Mock(return_value=job)
            runner.inspect.side_effect = [{'id': 'owned', 'status': 'created'},
                {'id': 'owned', 'status': 'running'}, {'id': 'owned', 'status': 'running'}]
            self.assertFalse(driver._step(job['id'])['cleanup'])
            github.token.assert_not_called()
            runner.prepare.assert_called_once_with(job)
            runner.start.assert_called_once()

    def test_image_schema_remains_standalone_under_isolated_python(self):
        source = Path(harness.__file__).resolve()
        run = subprocess.run(['python3', '-I', '-c',
            'import runpy; m=runpy.run_path(' + repr(str(source)) + '); '
            'assert m["plan_schema"]()["properties"]["version"]["enum"] == [2]'],
            capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stderr)

    def test_combined_environment_preserves_explicit_tiers_and_all_services(self):
        choices = {'a': {'tier': 'full', 'services': ['postgres'], 'automatic': False},
                   'b': {'tier': 'full', 'services': ['redis'], 'automatic': True},
                   'c': {'tier': 'standard', 'services': [], 'automatic': True}}
        self.assertEqual(combined_environment(choices), {'tier': 'full', 'services': ['postgres', 'redis']})
        choices['c']['automatic'] = False
        self.assertIsNone(combined_environment(choices))
        self.assertEqual(combined_environment({'c': choices['c']}), {'tier': 'standard', 'services': []})


class TaskBundleTests(unittest.TestCase):
    def test_diamond_join_preserves_every_exact_output_and_rejects_omission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), '-c', 'user.name=Test',
                    '-c', 'user.email=test@example.test', *args], env=env, stderr=subprocess.DEVNULL).decode().strip()
            def commit(name):
                (root / name).write_text(name)
                git('add', name); git('commit', '-qm', name)
                return git('rev-parse', 'HEAD')
            git('init', '--quiet')
            base = commit('base')
            owner = commit('owner-checkpoint')
            left = commit('left-contribution')
            git('checkout', '--quiet', '--detach', owner)
            right = commit('right-contribution')
            partial = root / 'partial.bundle'
            git('bundle', 'create', str(partial), 'HEAD', '^' + base)
            with self.assertRaises(ValueError):
                verify_bundle(partial.read_bytes(), right, [base, owner, left, right])
            git('merge', '--quiet', '--no-edit', left)
            final = git('rev-parse', 'HEAD')
            path = root / 'joined.bundle'
            git('bundle', 'create', str(path), 'HEAD', '^' + base)
            verified = verify_bundle(path.read_bytes(), final, [base, owner, left, right])
            self.assertEqual(verified['head_oid'], final)
            self.assertEqual(verified['size'], len(path.read_bytes()))
