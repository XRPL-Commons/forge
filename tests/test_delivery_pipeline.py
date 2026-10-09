"""The ordinary controllers execute one builder plan, children and candidate."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from forge.factory.config import validate, policy
from forge.factory.dispatch_controller import DispatchController, PreparedDispatch
from forge.factory.result_controller import ResultController
from forge.factory.recovery_controller import RecoveryController
from forge.factory.delegation_artifacts import verify_bundle
from forge.store.db import Store
from forge.store.delivery import Delivery, DeliveryQueries
from forge.store.executions import Executions, ExecutionQueries
from forge.store.models import Command
from forge.store.transitions import Lifecycle


class DeliveryPipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        self.repo = self.root / 'repo'
        self.git('init', '--quiet', str(self.repo), root=True)
        (self.repo / 'base').write_text('base\n')
        self.git('add', '.'); self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.store = Store.create(self.root / 'state')
        self.life = Lifecycle(self.store, clock=lambda: 100)
        self.delivery = Delivery(self.store, clock=lambda: 100)
        self.executions = Executions(self.store, clock=lambda: 100)
        self.jobs = ExecutionQueries(self.store)
        self.serial = 0
        pins = []
        for number in (1, 2):
            row = self.life.admit_issue(request_id='admit-' + str(number), actor_id='operator', unit_id='source-' + str(number),
                issue_id='issue-' + str(number), repository_id=1, repository_name='example/repo', issue_number=number,
                title='Issue', body='Scope', requirements='req-' + str(number), observed_at=90)
            pins.append(dict(issue_id='issue-' + str(number), issue_version=1, requirements_revision_id=row['revision_id'],
                             unit_id='source-' + str(number), unit_version=1, member_id=row['member_id']))
        proposal = dict(repository_id=1, base_branch='main', members=pins, dependencies=[], existing_prs=[], rationale='One change')
        self.delivery.compose(Command('compose', 'operator', 'batch', 1), **proposal,
            approval={'approved': True, 'composition_hash': self.delivery.composition_hash(**proposal)})
        self.config = validate({'enabled': True, 'repository_merge_policies': {'1': 'auto'}, 'image': 'sha256:' + 'a' * 64, 'slots': {'build': 2, 'review': 1, 'audit': 1, 'recovery': 1}})
        self.runner = Mock(); self.runner.capacity.return_value = {'allowed': True}
        self.github = Mock()
        self.github.issue.side_effect = lambda item: ({'state': 'open', 'body': 'Scope', 'labels': []}, 'req-' + str(item['number']))
        self.github.publication.return_value = None
        self.github.pulls.return_value = []
        self.github.publisher.side_effect = lambda role: role + '[bot]'
        self.verdicts = Mock(); self.verdicts.ensure.return_value = {'id': 7}
        self.results = ResultController(self.store, self.github, policy=lambda job: policy(self.config, job['repo_id'], repository_name=job['repo']), verdicts=self.verdicts, clock=lambda: 100)
        self.dispatch = self.dispatcher()

    def git(self, *args, root=False):
        argv = ['git'] + ([] if root else ['-C', str(self.repo)])
        return subprocess.check_output(argv + ['-c', 'user.name=Test', '-c', 'user.email=test@example.test', *args], env=self.env).decode()

    def dispatcher(self):
        return DispatchController(self.store, self.runner, config=self.config,
            prepare=lambda snapshot, role: PreparedDispatch(self.base, role + '[bot]', self.config,
                snapshot['candidate']['branch'] if snapshot['candidate'] else None), admission=lambda _: True, clock=lambda: 100)

    def command(self):
        self.serial += 1
        with self.store.read_snapshot() as db:
            version = db.execute("SELECT version FROM units WHERE id='batch'").fetchone()[0]
        return Command('test-' + str(self.serial), 'supervisor:test', 'batch', version)

    def capture(self, identifier, *, outcome='pass', bundle=None, classifier=None, **fields):
        job = self.jobs.job(identifier)
        report = dict(version=1, attempt=identifier, role=job['role'], outcome=outcome, summary='Complete', **fields)
        if job.get('builder_stage') or job.get('delegation'):
            report.setdefault('delegation_plan', None); report.setdefault('output_commit', None)
        capture = {'result': report, 'logs': '', 'execution': {'exit_code': 0}}
        if bundle is not None: capture['worktree_bundle'] = bundle
        self.executions.collect(self.command(), identifier, execution_fence=job['execution_fence'], capture=capture)
        self.life.verify_cleanup(self.command(), identifier, execution_fence=job['execution_fence'],
                                 evidence={'process_stopped': True, 'resources_released': True})
        return (classifier or self.results.step)(identifier)

    def child_bundle(self, index):
        self.git('checkout', '--quiet', '--detach', self.base)
        (self.repo / ('part-' + str(index))).write_text('part ' + str(index) + '\n')
        self.git('add', '.'); self.git('commit', '-qm', 'part ' + str(index))
        head = self.git('rev-parse', 'HEAD').strip()
        path = self.root / ('part-' + str(index) + '.bundle')
        self.git('bundle', 'create', str(path), 'HEAD', '^' + self.base)
        return head, path.read_bytes()

    def integrate(self):
        parent = self.dispatch.step('batch')['job_id']
        planning = self.jobs.job(parent)
        self.assertIsNone(planning['issue_number'])
        self.assertEqual(len(planning['members']), 2)
        children = [dict(key='part-' + str(index), member_id=member['member_id'], scope='Implement member', dependencies=[])
                    for index, member in enumerate(planning['members'])]
        self.assertTrue(self.capture(parent, outcome='delegate', delegation_plan={'children': children})['accepted'])
        # Restarting the coordinator retains the plan and never reserves it twice.
        self.dispatch = self.dispatcher()
        first = self.dispatch.step('batch')['job_id']; second = self.dispatch.step('batch')['job_id']
        self.assertEqual(self.jobs.job(first)['delegation']['parent_job_id'], parent)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0], 2)
        self.assertFalse(self.dispatch.step('batch')['reserved'])
        for index, identifier in enumerate((first, second)):
            head, bundle = self.child_bundle(index)
            self.assertTrue(self.capture(identifier, output_commit=head, bundle=bundle)['accepted'])
        resumed = self.dispatch.step('batch')['job_id']; integration = self.jobs.job(resumed)
        self.assertEqual(integration['builder_id'], planning['builder_id'])
        self.assertEqual(integration['builder_stage'], 'integration')
        self.assertEqual(len(list(self.jobs.input_bundles(self.jobs.input_refs(integration)))), 2)
        return integration

    def test_two_parallel_children_resume_owner_and_publish_one_reviewed_candidate(self):
        integration = self.integrate()
        resumed = integration['id']
        self.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40, 'base': self.base}
        self.assertTrue(self.capture(resumed)['accepted'])
        for role in ('review', 'audit'):
            identifier = self.dispatch.step('batch')['job_id']; job = self.jobs.job(identifier)
            self.assertEqual(job['role'], role); self.assertEqual(len(job['members']), 2)
            selected = job['candidate']
            self.github.pr.return_value = {'number': selected['pr'], 'state': 'open', 'draft': False, 'merged': False,
                'head': {'sha': selected['head'], 'repo': {'id': 1}}, 'base': {'sha': selected['base'], 'ref': 'main', 'repo': {'id': 1}}}
            extra = {'candidate': selected['key']}
            if role == 'audit': extra.update(human_required='not_required', human_reason='Policy')
            self.assertTrue(self.capture(identifier, **extra)['accepted'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='batch'").fetchone()[0], 'merging')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE role IN ('review','audit')").fetchone()[0], 2)

    def test_late_combined_publication_corrects_only_its_parent_failure(self):
        integration = self.integrate()
        failed = self.capture(integration['id'], outcome='unable')
        self.assertTrue(failed['charged'])
        recovery = RecoveryController(self.store, self.runner, self.github, config=self.config,
                                      admission=lambda _: True, clock=lambda: 100)
        investigation = recovery.schedule(integration['id'])['job_id']
        self.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40, 'base': self.base}
        receipt = self.capture(investigation, classifier=recovery.finish,
                               pattern='Publication response was delayed', evidence=[], uncertainty='', alternative=None)
        self.assertTrue(receipt['corrected'])
        self.assertEqual(DeliveryQueries(self.store).tree('batch')['charged_failures'], 0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='batch'").fetchone()[0], 'reviewing')

    def test_malformed_child_plan_charges_shared_budget_once(self):
        parent = self.dispatch.step('batch')['job_id']
        failed = self.capture(parent, outcome='delegate', delegation_plan={'children': []})
        self.assertTrue(failed['charged'])
        self.results.step(parent)
        self.assertEqual(DeliveryQueries(self.store).tree('batch')['charged_failures'], 1)

    def test_bundle_revision_and_ancestry_are_independently_checked(self):
        head, bundle = self.child_bundle(0)
        self.assertEqual(verify_bundle(bundle, head, [self.base])['head_oid'], head)
        with self.assertRaises(ValueError): verify_bundle(bundle, 'd' * 40, [self.base])
        with self.assertRaises(ValueError): verify_bundle(bundle, head, ['e' * 40])
        with self.assertRaises(ValueError): verify_bundle(bundle[:-5], head, [self.base])

    def test_bundle_must_contain_every_assigned_predecessor(self):
        first, _ = self.child_bundle(0)
        independent, bundle = self.child_bundle(1)
        with self.assertRaises(ValueError):
            verify_bundle(bundle, independent, [self.base, first])
        self.git('merge', '--quiet', '--no-edit', first)
        head = self.git('rev-parse', 'HEAD').strip()
        path = self.root / 'dependent.bundle'
        self.git('bundle', 'create', str(path), 'HEAD', '^' + self.base)
        self.assertEqual(verify_bundle(path.read_bytes(), head, [self.base, first, independent])['head_oid'], head)
