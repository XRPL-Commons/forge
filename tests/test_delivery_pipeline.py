"""The ordinary controllers execute one builder plan, children and candidate."""
from pathlib import Path
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

from forge.factory.config import validate, policy
from forge.factory.dispatch_controller import DispatchController, PreparedDispatch, GitHubPrepare
from forge.factory.result_controller import ResultController
from forge.factory.recovery_controller import RecoveryController
from forge.factory.delegation_artifacts import verify_bundle
from forge.store.db import Store
from forge.store.delivery import Delivery, DeliveryQueries
from forge.store.executions import Executions, ExecutionQueries
from forge.store.models import Command
from forge.store.transitions import Lifecycle


class DeliveryPipelineTests(unittest.TestCase):
    dependencies=()
    def setUp(self, existing_author=None, bodies=None):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.env = {**os.environ, 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1'}
        self.repo = self.root / 'repo'
        self.git('init', '--quiet', str(self.repo), root=True)
        (self.repo / 'base').write_text('base\n')
        self.git('add', '.'); self.git('commit', '-qm', 'base')
        self.base = self.git('rev-parse', 'HEAD').strip()
        self.now = 100
        self.store = Store.create(self.root / 'state')
        self.life = Lifecycle(self.store, clock=lambda: self.now)
        self.delivery = Delivery(self.store, clock=lambda: self.now)
        self.executions = Executions(self.store, clock=lambda: self.now)
        self.jobs = ExecutionQueries(self.store)
        self.serial = 0
        pins = []
        receipts = []
        for number in (1, 2):
            row = self.life.admit_issue(request_id='admit-' + str(number), actor_id='operator', unit_id='source-' + str(number),
                issue_id='issue-' + str(number), repository_id=1, repository_name='example/repo', issue_number=number,
                title='Issue', body=(bodies or {}).get(number, 'Scope'), requirements='req-' + str(number), observed_at=90)
            version = 1
            if existing_author and number == 1:
                from forge.store.models import Candidate
                from forge.store.transitions import manifest_hash
                candidate = Candidate(7, 'existing-topic', 'a' * 40, self.base, 'old-policy', manifest_hash('req-1'), 'old-input')
                chosen = self.life.adopt_candidate(self.command('source-1'), candidate,
                    evidence={'approved': True, 'verified': True, **{key: getattr(candidate, key) for key in
                        ('pull_number', 'head_oid', 'base_oid', 'policy_hash', 'input_manifest_hash')}})
                version = chosen['version']
                receipts.append(dict(unit_id='source-1', candidate_id=chosen['candidate_id'], verified=True, merged=False,
                    pull_number=7, head_oid='a' * 40, observed_at=100, author=existing_author))
            pins.append(dict(issue_id='issue-' + str(number), issue_version=1, requirements_revision_id=row['revision_id'],
                             unit_id='source-' + str(number), unit_version=version, member_id=row['member_id']))
        proposal = dict(repository_id=1, base_branch='main', members=pins, dependencies=list(self.dependencies), existing_prs=receipts, rationale='One change')
        if existing_author:
            proposal['publication_target'] = {'kind': 'existing', 'ownership_transfer': True,
                **{key: receipts[0][key] for key in ('unit_id', 'candidate_id', 'pull_number', 'head_oid', 'author')}}
        self.delivery.compose(Command('compose', 'operator', 'batch', 1), **proposal,
            approval={'approved': True, 'composition_hash': self.delivery.composition_hash(**proposal)})
        self.config = validate({'enabled': True, 'repository_merge_policies': {'1': 'auto'}, 'image': 'sha256:' + 'a' * 64, 'slots': {'build': 2, 'review': 1, 'audit': 1, 'recovery': 1}})
        self.runner = Mock(); self.runner.capacity.return_value = {'allowed': True}
        self.github = Mock()
        self.github.issue.side_effect = lambda item: ({'state': 'open', 'body': (bodies or {}).get(item['number'], 'Scope'), 'labels': []}, 'req-' + str(item['number']))
        self.github.publication.return_value = None
        self.github.pulls.return_value = []
        self.github.publisher.side_effect = lambda role: role + '[bot]'
        self.verdicts = Mock(); self.verdicts.ensure.return_value = {'check_id': 7}
        self.results = ResultController(self.store, self.github, policy=lambda job: policy(self.config, job['repo_id'], repository_name=job['repo']), verdicts=self.verdicts, clock=lambda: self.now)
        self.dispatch = self.dispatcher()

    def git(self, *args, root=False):
        argv = ['git'] + ([] if root else ['-C', str(self.repo)])
        return subprocess.check_output(argv + ['-c', 'user.name=Test', '-c', 'user.email=test@example.test', *args], env=self.env).decode()

    def dispatcher(self):
        return DispatchController(self.store, self.runner, config=self.config,
            prepare=lambda snapshot, role: PreparedDispatch(self.base, role + '[bot]', self.config,
                snapshot['candidate']['branch'] if snapshot['candidate'] else None), admission=lambda _: True, clock=lambda: self.now)

    def command(self, unit='batch'):
        self.serial += 1
        with self.store.read_snapshot() as db:
            version = db.execute('SELECT version FROM units WHERE id=?', (unit,)).fetchone()[0]
        return Command('test-' + str(self.serial), 'supervisor:test', unit, version)

    def capture(self, identifier, *, outcome='pass', bundle=None, classifier=None, **fields):
        job = self.jobs.job(identifier)
        report = dict(version=1, attempt=identifier, role=job['role'], outcome=outcome, summary='Complete', **fields)
        if job.get('builder_stage') or job.get('task'):
            report.setdefault('delegation_plan', None); report.setdefault('output_commit', None)
        if job.get('build_result_version') == 2:
            report.update(version=2,outcome={'pass':'done','delegate':'done',
                'needs_human_verification':'done','unable':'cannot_complete'}[outcome],
                headline='Completed build',plain='Build result for this assignment.',
                operator_summary='Build result for this assignment.',claims=[])
            for key in ('verification','delegation_plan','output_commit','pr_title','pr_body'):
                report.setdefault(key,None)
        capture = {'result': report, 'logs': '', 'execution': {'exit_code': 0}}
        if bundle is not None: capture['worktree_bundle'] = bundle
        self.executions.collect(self.command(job['unit_id']), identifier, execution_fence=job['execution_fence'], capture=capture)
        self.life.verify_cleanup(self.command(job['unit_id']), identifier, execution_fence=job['execution_fence'],
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

    @staticmethod
    def plan_value(tasks):
        return {'version': 2, 'retained_task_ids': [], 'tasks': tasks,
                'owner_final': {'member_ids': [], 'scope': '', 'internalizes': []}}

    def integrate(self, verification=None):
        parent = self.dispatch.step('batch')['job_id']
        planning = self.jobs.job(parent)
        self.assertIsNone(planning['issue_number'])
        self.assertEqual(len(planning['members']), 2)
        children = [dict(key='part-' + str(index), executor='child', member_ids=[member['member_id']], scope='Implement member', depends_on=[], internalizes=[])
                    for index, member in enumerate(planning['members'])]
        self.assertTrue(self.capture(parent, outcome='delegate', delegation_plan=self.plan_value(children))['accepted'])
        # Restarting the coordinator retains the plan and never reserves it twice.
        self.dispatch = self.dispatcher()
        first = self.dispatch.step('batch')['job_id']; second = self.dispatch.step('batch')['job_id']
        self.assertEqual(self.jobs.job(first)['task']['parent_job_id'], parent)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0], 2)
        self.assertFalse(self.dispatch.step('batch')['reserved'])
        for index, identifier in enumerate((first, second)):
            head, bundle = self.child_bundle(index)
            extra = dict(outcome='needs_human_verification', verification=verification) if verification else {}
            self.assertTrue(self.capture(identifier, output_commit=head, bundle=bundle, **extra)['accepted'])
        resumed = self.dispatch.step('batch')['job_id']; integration = self.jobs.job(resumed)
        self.assertEqual(integration['builder_id'], planning['builder_id'])
        self.assertEqual(integration['builder_stage'], 'integration')
        self.assertEqual(len(list(self.jobs.input_bundles(self.jobs.input_refs(integration)))), 2)
        return integration

    def test_single_build_slot_alternates_deliveries_through_children_after_restart(self):
        pins = []
        for number in (3, 4):
            unit, issue = 'source-' + str(number), 'issue-' + str(number)
            row = self.life.admit_issue(request_id='admit-' + str(number), actor_id='operator', unit_id=unit,
                issue_id=issue, repository_id=1, repository_name='example/repo', issue_number=number,
                title='Issue', body='Scope', requirements='req-' + str(number), observed_at=90)
            pins.append(dict(issue_id=issue, issue_version=1, requirements_revision_id=row['revision_id'],
                             unit_id=unit, unit_version=1, member_id=row['member_id']))
        proposal = dict(repository_id=1, base_branch='main', members=pins, dependencies=[], existing_prs=[], rationale='Another change')
        self.delivery.compose(Command('compose-other', 'operator', 'other', 1), **proposal,
            approval={'approved': True, 'composition_hash': self.delivery.composition_hash(**proposal)})
        self.config['slots']['build'] = 1
        expected = [('batch', 'planning'), ('other', 'planning'), ('batch', 'task'),
                    ('other', 'task'), ('batch', 'task'), ('other', 'task')]
        allocations = []
        for index, (unit, stage) in enumerate(expected):
            # New store and controller handles, with a frozen clock: only durable
            # reservation order can distinguish turns, not process state or time.
            self.store = Store.open(self.root / 'state')
            page = self.dispatcher().cycle()
            reserved = [item for item in page['items'] if item.get('reserved')]
            self.assertEqual(len(reserved), 1, page)
            job = self.jobs.job(reserved[0]['job_id'])
            self.assertEqual(job['unit_id'], unit)
            self.assertEqual(job['slot'], 'build-1')
            self.assertEqual(job.get('builder_stage') or 'child', stage)
            allocations.append(job['id'])
            with self.store.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0], 1)
            # An occupied slot keeps its original deadline even across a restart.
            waiting = self.dispatcher().step('other' if unit == 'batch' else 'batch')
            self.assertEqual(waiting['reason'], 'role_capacity')
            self.assertGreater(waiting['retry_at'], self.now)
            if stage == 'planning':
                children = [dict(key='part-' + str(i), executor='child', member_ids=[member['member_id']], scope='Implement member', depends_on=[], internalizes=[])
                            for i, member in enumerate(job['members'])]
                result = self.capture(job['id'], outcome='delegate', delegation_plan=self.plan_value(children))
            else:
                head, bundle = self.child_bundle(index)
                result = self.capture(job['id'], output_commit=head, bundle=bundle)
            self.assertTrue(result['accepted'])
        # Both deliveries are ready to integrate; neither had to wait for the
        # other delivery's complete sequence of child work.
        selected = [item for item in self.dispatcher().cycle()['items'] if item.get('reserved')]
        integration = self.jobs.job(selected[0]['job_id'])
        self.assertEqual((integration['unit_id'], integration['builder_stage']), ('batch', 'integration'))
        with self.store.read_snapshot() as db:
            events = db.execute("SELECT e.job_id FROM events e JOIN jobs j ON j.id=e.job_id "
                "WHERE e.kind='attempt_reserved' ORDER BY e.sequence").fetchall()
            self.assertEqual([row[0] for row in events], allocations + [integration['id']])
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)

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

    def test_changed_base_retains_exact_group_publication_for_fresh_planning(self):
        from copy import deepcopy
        from forge.factory.ownership import publication_context, owned_publication_matches
        integration = self.integrate()
        old_base = self.base
        self.base = 'd' * 40
        author = {'id': 91, 'login': 'build[bot]', 'type': 'Bot'}
        self.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40,
                                              'base': self.base, 'author': author}
        self.assertEqual(self.capture(integration['id'])['pending'], 'base_reconciliation')
        self.assertFalse(self.results.step(integration['id'])['accepted'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM candidates').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM unit_gates WHERE resolved_at IS NULL").fetchone()[0], 0)
        pr = {'number': 9, 'state': 'open', 'draft': False, 'merged': False, 'body': 'Fixes #1\nFixes #2',
              'user': author, 'head': {'sha': 'c' * 40, 'ref': integration['branch'], 'repo': {'id': 1}},
              'base': {'sha': self.base, 'ref': 'main', 'repo': {'id': 1}}}
        context = publication_context(self.store, 'batch', {})
        self.assertTrue(owned_publication_matches(pr, context))
        for altered in ({**pr, 'number': 10}, {**pr, 'user': {**author, 'id': 92}},
                        {**pr, 'head': {**pr['head'], 'sha': 'e' * 40}}):
            self.assertFalse(owned_publication_matches(altered, context))
        self.github.pr.return_value = pr; self.github.pulls.return_value = [pr]
        self.github.target.return_value = 'main'; self.github.protections.return_value = True
        self.github.call.return_value = {'commit': {'sha': self.base}}
        self.github.ci_evidence.return_value = {}
        self.dispatch.prepare = GitHubPrepare(self.github, self.config)
        # This is the normal read admission and dispatch path after coordinator restart.
        changed_author = deepcopy(pr); changed_author['user']['id'] = 92
        self.github.pr.return_value = changed_author
        self.assertFalse(self.dispatch.step('batch')['reserved'])
        self.github.pr.return_value = pr
        waiting = self.dispatch.step('batch')
        self.assertFalse(waiting['reserved'])
        self.assertEqual(waiting['reason'], 'observation_changed')
        self.now = waiting['retry_at']
        resumed = self.jobs.job(self.dispatch.step('batch')['job_id'])
        self.assertEqual(resumed['builder_stage'], 'planning')
        self.assertEqual(resumed['builder_id'], integration['builder_id'])
        self.assertEqual(resumed['input_manifest']['base_oid'], self.base)
        self.assertNotEqual(resumed['input_manifest']['base_oid'], old_base)
        self.assertEqual(resumed['input_manifest']['previous_publication']['head_oid'], 'c' * 40)
        self.assertEqual(resumed['input_manifest']['previous_publication']['source_job_id'], integration['id'])
        self.assertNotEqual(resumed['branch'], integration['branch'])
        self.assertNotIn('resume_pr', resumed)
        self.assertTrue(self.capture(resumed['id'], outcome='delegate', delegation_plan={
            'version': 2, 'retained_task_ids': [], 'tasks': [],
            'owner_final': {'member_ids': [row['member_id'] for row in resumed['members']],
                            'scope': 'Rebase the combined delivery', 'internalizes': []}})['accepted'])
        publisher = self.jobs.job(self.dispatch.step('batch')['job_id'])
        self.assertEqual(publisher['builder_stage'], 'integration')
        self.assertEqual((publisher['branch'], publisher['resume_pr'], publisher['resume_head']), (integration['branch'], 9, 'c' * 40))

    def test_existing_target_base_revocation_preserves_original_author_and_exact_pr(self):
        from forge.factory.ownership import publication_context, publication_transfer_matches
        from forge.store.models import TransitionError
        author = {'id': 73, 'login': 'maintainer', 'type': 'User'}
        self.setUp(existing_author=author)
        integration = self.integrate()
        self.capture(integration['id'], classifier=lambda _: None)
        publication = dict(pull_number=7, branch='existing-topic', head_oid='c' * 40, base_oid='d' * 40,
                           repository_id=1, base_branch='main', author=author)
        args = dict(execution_fence=integration['execution_fence'], input_manifest=integration['input_manifest'])
        for changed in ({**publication, 'pull_number': 8}, {**publication, 'author': {**author, 'id': 74}}):
            with self.assertRaises(TransitionError):
                self.executions.revoke_captured_base(self.command(), integration['id'], **args, publication=changed)
        self.executions.revoke_captured_base(self.command(), integration['id'], **args, publication=publication)
        self.assertFalse(self.results.step(integration['id'])['accepted'])
        context = publication_context(self.store, 'batch', {})
        self.assertEqual(context['publication_transfer']['author'], author)
        self.assertEqual(context['publication_transfer']['expected_head'], 'c' * 40)
        pr = {'number': 7, 'state': 'open', 'merged': False, 'user': author,
              'head': {'sha': 'c' * 40, 'ref': 'existing-topic', 'repo': {'id': 1}},
              'base': {'sha': 'd' * 40, 'ref': 'main', 'repo': {'id': 1}}}
        self.assertTrue(publication_transfer_matches(pr, context, allow_publication=False))
        self.assertFalse(publication_transfer_matches({**pr, 'number': 8}, context))
        self.assertFalse(publication_transfer_matches({**pr, 'user': {**author, 'id': 74}}, context))

    def test_late_combined_publication_corrects_only_its_parent_failure(self):
        integration = self.integrate()
        failed = self.capture(integration['id'], outcome='pass')
        self.assertTrue(failed['charged'])
        recovery = RecoveryController(self.store, self.runner, self.github, config=self.config,
                                      admission=lambda _: True, clock=lambda: self.now)
        investigation = recovery.schedule(integration['id'])['job_id']
        self.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40, 'base': self.base}
        receipt = self.capture(investigation, classifier=recovery.finish,
                               pattern='Publication response was delayed', evidence=[], uncertainty='', alternative=None)
        self.assertTrue(receipt['corrected'])
        self.assertEqual(DeliveryQueries(self.store).tree('batch')['charged_failures'], 0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT state FROM units WHERE id='batch'").fetchone()[0], 'reviewing')

    def test_unable_integration_is_not_promoted_by_matching_or_late_publication(self):
        integration = self.integrate()
        self.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40, 'base': self.base}
        self.assertTrue(self.capture(integration['id'], outcome='unable')['charged'])
        self.github.publication.assert_not_called()
        recovery = RecoveryController(self.store, self.runner, self.github, config=self.config,
                                      admission=lambda _: True, clock=lambda: self.now)
        investigation = recovery.schedule(integration['id'])['job_id']
        self.capture(investigation, classifier=recovery.finish,
                     pattern='Matching publication exists', evidence=[], uncertainty='', alternative=None)
        self.github.publication.assert_not_called()
        self.assertEqual(DeliveryQueries(self.store).tree('batch')['charged_failures'], 1)
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute("SELECT current_candidate_id FROM units WHERE id='batch'").fetchone()[0])

    def test_late_integration_retains_task_owner_and_investigator_verification(self):
        from forge.store.verification_requests import gate_details_in
        task_check = {'what': 'Check task behavior', 'why': 'Needs a device', 'checks': []}
        owner_check = {'what': 'Check integrated behavior', 'why': 'Needs staging', 'checks': []}
        recovery_check = {'what': 'Check publication behavior', 'why': 'Needs deployment', 'checks': []}
        integration = self.integrate(verification=task_check)
        self.assertTrue(self.capture(integration['id'], outcome='needs_human_verification', verification=owner_check)['charged'])
        recovery = RecoveryController(self.store, self.runner, self.github, config=self.config,
                                      admission=lambda _: True, clock=lambda: self.now)
        investigation = recovery.schedule(integration['id'])['job_id']
        self.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40, 'base': self.base}
        receipt = self.capture(investigation, classifier=recovery.finish, outcome='needs_human_verification',
                              verification=recovery_check, pattern='Publication delayed', evidence=[], uncertainty='', alternative=None)
        self.assertTrue(receipt['corrected'])
        with self.store.read_snapshot() as db:
            gate = dict(db.execute("SELECT * FROM unit_gates WHERE code='verification_human' AND resolved_at IS NULL").fetchone())
            details = gate_details_in(db, gate)
        self.assertEqual(len(details['requests']), 2)
        self.assertEqual(details['final_verification'], owner_check)
        self.assertEqual(details['recovery_verification'], recovery_check)
        for request in (task_check, owner_check, recovery_check):
            self.assertIn(request['what'], details['what'])
            self.assertIn(request['why'], details['why'])
        self.assertLess(len(gate['details_json']), 1000)
        self.assertFalse(self.dispatch.step('batch')['reserved'])

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

    def test_child_uses_its_own_environment_and_memory_not_a_siblings(self):
        self.config = validate({**self.config, 'memory': '4g', 'memory_tiers': ['4g', '8g', '16g'],
                                'environment_tiers': {'full': {'memory': '8g', 'image': 'sha256:' + 'b' * 64, 'cpus': 2}}})
        self.dispatch = self.dispatcher()
        parent = self.dispatch.step('batch')['job_id']; planning = self.jobs.job(parent)
        members = planning['members']
        plan = self.plan_value([dict(key='part-' + str(index), executor='child', member_ids=[member['member_id']], scope='Implement member', depends_on=[], internalizes=[])
                             for index, member in enumerate(members)])
        self.capture(parent, outcome='delegate', delegation_plan=plan)
        with self.store.write() as db:
            for member, memory in zip(members, ['16g', '8g']):
                db.execute('INSERT INTO issue_environment_observations(issue_id,memory_retry_json) VALUES(?,?)',
                           (member['issue_key'], json.dumps({'role': 'build', 'memory': memory})))
        # The first planned child is freshly held; the second remains eligible.
        eligible = members[1]['member_id']
        self.dispatch.prepare = lambda snapshot, role: PreparedDispatch(self.base, role + '[bot]', self.config,
            member_choices={eligible: {'tier': 'full', 'services': ['postgres']}})
        child = self.jobs.job(self.dispatch.step('batch')['job_id'])
        self.assertEqual(child['members'][0]['member_id'], eligible)
        self.assertEqual(child['runner_config']['memory'], '8g')
        self.assertEqual(child['runner_config']['services'], ['postgres'])
        self.assertEqual(child['runner_config']['environment_tier'], 'full')

    def test_owner_resumes_with_union_services_and_maximum_scoped_retry(self):
        self.config = validate({**self.config, 'memory': '4g', 'memory_tiers': ['4g', '8g', '16g'],
                                'environment_tiers': {'full': {'memory': '8g', 'image': 'sha256:' + 'b' * 64, 'cpus': 2}}})
        self.dispatch = self.dispatcher()
        original = self.dispatch.prepare
        def prepare(snapshot, role):
            if snapshot['grouped']:
                choices = {row['member']['id']: {'tier': 'full', 'services': [service]}
                           for row, service in zip(snapshot['members'], ['postgres', 'redis'])}
                return PreparedDispatch(self.base, role + '[bot]', self.config, member_choices=choices)
            return original(snapshot, role)
        self.dispatch.prepare = prepare
        # integrate() restarts dispatch; preserve the desired fresh preparation.
        original_factory = self.dispatcher
        def factory():
            dispatcher = original_factory(); dispatcher.prepare = prepare
            return dispatcher
        self.dispatcher = factory
        with self.store.write() as db:
            db.execute('INSERT INTO issue_environment_observations(issue_id,memory_retry_json) VALUES(?,?)',
                       ('issue-2', json.dumps({'role': 'build', 'memory': '16g'})))
        integration = self.integrate()
        self.assertEqual(integration['runner_config']['memory'], '16384m')
        self.assertEqual(integration['runner_config']['services'], ['postgres', 'redis'])

    def test_rework_keeps_established_pr_and_exact_active_publication_authority(self):
        from forge.factory.ownership import active_publication_matches
        from forge.store.models import Candidate, TransitionError
        from forge.store.transitions import manifest_hash
        integration = self.integrate()
        self.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40, 'base': self.base}
        accepted = self.capture(integration['id'])
        self.life.invalidate_candidate(self.command(), accepted['candidate_id'], reason='candidate_changed', evidence={'verified': True})
        pr = {'number': 9, 'state': 'open', 'draft': False, 'merged': False, 'body': 'Fixes #1\nFixes #2',
              'user': {'id': 1, 'login': 'build[bot]', 'type': 'Bot'},
              'head': {'sha': 'c' * 40, 'ref': integration['branch'], 'repo': {'id': 1}},
              'base': {'sha': self.base, 'ref': 'main', 'repo': {'id': 1}}}
        self.github.pr.return_value = pr; self.github.pulls.return_value = [pr]
        self.github.target.return_value = 'main'; self.github.protections.return_value = True
        self.github.call.return_value = {'commit': {'sha': self.base}}
        self.github.ci_evidence.return_value = {}
        self.dispatch.prepare = GitHubPrepare(self.github, self.config)
        resumed = self.jobs.job(self.dispatch.step('batch')['job_id'])
        self.assertEqual(resumed['builder_stage'], 'planning')
        self.assertNotIn('resume_pr', resumed)
        self.assertNotEqual(resumed['branch'], integration['branch'])
        self.assertTrue(self.capture(resumed['id'], outcome='delegate', delegation_plan={
            'version': 2, 'retained_task_ids': [], 'tasks': [],
            'owner_final': {'member_ids': [row['member_id'] for row in resumed['members']],
                            'scope': 'Revise the combined delivery', 'internalizes': []}})['accepted'])
        resumed = self.jobs.job(self.dispatch.step('batch')['job_id'])
        self.assertEqual(resumed['builder_stage'], 'integration')
        self.assertEqual(resumed['resume_pr'], 9)
        self.assertEqual(resumed['resume_head'], 'c' * 40)
        self.assertEqual(resumed['branch'], integration['branch'])
        self.assertEqual(resumed['builder_id'], integration['builder_id'])
        self.assertEqual(resumed['input_manifest']['previous_publication']['id'], accepted['candidate_id'])
        self.assertTrue(active_publication_matches(self.store, 'batch', {**pr, 'draft': True}))
        marker = {'version': 2, 'unit': 'batch', 'members': [1, 2], 'attempt': resumed['id'], 'head': 'd' * 40}
        updated = {**pr, 'head': {**pr['head'], 'sha': 'd' * 40}, 'draft': True,
                   'body': 'Fixes #1\nFixes #2\n<!-- forge-submission ' + json.dumps(marker) + ' -->'}
        self.assertTrue(active_publication_matches(self.store, 'batch', updated))
        from forge.factory.observation_controller import ObservationController
        self.github.pulls.return_value = [updated]
        self.github.issue.side_effect = lambda item: (dict(number=item['number'], state='open', title='Issue',
            body='Scope', updated_at='2026-10-10T00:00:00Z', labels=[], assignees=[]), 'req-' + str(item['number']))
        observed = ObservationController(self.store, self.github, admission=lambda _: True, clock=lambda: self.now).step('batch')
        self.assertTrue(all(row.get('observed') for row in observed['members'].values()))
        self.assertFalse(self.jobs.job(resumed['id'])['fenced'])
        self.assertFalse(active_publication_matches(self.store, 'batch', {**updated, 'head': {**updated['head'], 'sha': 'e' * 40}}))
        wrong = Candidate(10, resumed['branch'], 'd' * 40, self.base, resumed['input_manifest']['policy_hash'],
                          resumed['input_manifest']['requirements_hash'], manifest_hash(resumed['input_manifest']))
        with self.assertRaises(TransitionError):
            self.delivery._publication_target_in(resumed['input_manifest'], wrong)
        self.delivery.cancel_builder(self.command(), evidence={'reason': 'operator'})
        self.assertFalse(active_publication_matches(self.store, 'batch', updated))

    def test_public_jobs_and_issue_detail_keep_exact_assignment_scope(self):
        from forge.factory.store_views import PipelineViews
        views = PipelineViews(self.store, runtime=self.config, clock=lambda: self.now)
        parent = self.dispatch.step('batch')['job_id']; planning = self.jobs.job(parent)
        shared = views.job(parent)
        self.assertIsNone(shared['issue_key']); self.assertIsNone(shared['issue_number'])
        self.assertEqual({member['issue_key'] for member in shared['members']}, {'issue-1','issue-2'})
        for key in ('issue-1','issue-2'):
            detail = views.issues(key=key)['issue']
            self.assertEqual(detail['active'], parent)
            self.assertTrue(detail['delivery_approved'])
            self.assertFalse(detail['closable'])
        plan = self.plan_value([dict(key=str(index),executor='child',member_ids=[member['member_id']],scope='Implement',depends_on=[],internalizes=[])
                             for index,member in enumerate(planning['members'])])
        self.capture(parent,outcome='delegate',delegation_plan=plan)
        first = self.dispatch.step('batch')['job_id']; second = self.dispatch.step('batch')['job_id']
        for identifier in (first,second):
            child = views.job(identifier)
            self.assertEqual(child['parent_job_id'],parent)
            self.assertEqual(child['builder_stage'],'task')
            detail = views.issues(key=child['issue_key'])
            self.assertEqual(detail['issue']['active'],identifier)
            self.assertEqual([job['id'] for job in detail['jobs']],[identifier])
            other = second if identifier==first else first
            self.assertNotIn(other,[job['id'] for job in detail['factory_issue']['attempts']])
        scoped = self.jobs.job(first)['members'][0]
        self.life.open_gate(self.command(),'decision_required',member_id=scoped['member_id'],details={'question':'Scoped choice'})
        other_key = 'issue-2' if scoped['issue_key']=='issue-1' else 'issue-1'
        self.assertEqual(views.issues(key=scoped['issue_key'])['issue']['decision_request']['question'],'Scoped choice')
        self.assertIsNone(views.issues(key=other_key)['issue']['decision_request'])
        tree = views.delivery('issue-1')
        self.assertEqual(len(tree['members']),2)
        self.assertEqual({job['id'] for job in tree['jobs']},{parent,first,second})
        self.assertNotIn('body',tree['members'][0])
