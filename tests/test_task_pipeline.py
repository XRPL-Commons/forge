"""Composed v2 controller, store and real Git bundle acceptance paths."""
import json
import unittest

from tests import test_delivery_pipeline as fixtures
from forge.store.task_inputs import member_contributions_in
from forge.factory.config import validate
from forge.factory.dispatch_controller import PreparedDispatch


class TaskPipelineTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.DeliveryPipelineTests('runTest')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)

    def plan(self, build):
        f = self.f
        identifier = f.dispatch.step('batch')['job_id']
        job = f.jobs.job(identifier)
        self.assertEqual(job['input_manifest']['manifest_version'], 2)
        plan = build(job['members'])
        self.assertTrue(f.capture(identifier, outcome='delegate', delegation_plan=plan)['accepted'])
        return job

    @staticmethod
    def definition(key, members, *, executor='child', depends_on=()):
        return {'key': key, 'member_ids': list(members), 'scope': 'Implement ' + key,
                'executor': executor, 'depends_on': list(depends_on), 'internalizes': []}

    @staticmethod
    def value(tasks, owner=()):
        return {'version': 2, 'retained_task_ids': [], 'tasks': tasks,
                'owner_final': {'member_ids': list(owner), 'scope': 'Complete final owner work' if owner else '', 'internalizes': []}}

    def bundle(self, name, parent=None):
        f = self.f
        f.git('checkout', '--quiet', '--detach', parent or f.base)
        (f.repo / name).write_text(name + '\n')
        f.git('add', name); f.git('commit', '-qm', name)
        head = f.git('rev-parse', 'HEAD').strip()
        path = f.root / (name + '.bundle')
        f.git('bundle', 'create', str(path), 'HEAD', '^' + f.base)
        return head, path.read_bytes()

    def waiting_assignment(self, stage='task'):
        # The quote belongs to member 2 only; the two immutable requirements
        # digests differ. A joint wait must not copy either digest to its sibling.
        f = fixtures.DeliveryPipelineTests('runTest')
        f.setUp(bodies={2: 'Depends on #10 before implementation'})
        self.addCleanup(f.doCleanups); self.f = f
        if stage == 'planning':
            return f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.plan(lambda members: self.value(
            [self.definition('shared', [row['member_id'] for row in members])] if stage == 'task' else [],
            [row['member_id'] for row in members] if stage == 'integration' else []))
        return f.jobs.job(f.dispatch.step('batch')['job_id'])

    def test_joint_task_dependency_wait_is_uncharged_and_each_revision_requires_completion(self):
        from forge.factory.prerequisites import collect
        from forge.store.runtime_requirements import RuntimeRequirements
        from forge.store.models import TransitionError
        job = self.waiting_assignment(); f = self.f
        outcome = f.capture(job['id'], outcome='waiting_on_issues', issues=[10], evidence='Depends on #10')
        self.assertTrue(outcome['accepted']); self.assertFalse(outcome['charged'])
        self.assertEqual(len(outcome['gate_ids']), 2)
        with f.store.read_snapshot() as db:
            gates = list(db.execute("SELECT member_id,details_json FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL"))
            self.assertEqual({row['member_id']: json.loads(row['details_json'])['requirements'] for row in gates},
                             {pin['member_id']: pin['requirements'] for pin in job['members']})
            self.assertEqual({json.loads(row['details_json'])['evidence'] for row in gates}, {'Depends on #10'})
            self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM member_dependencies').fetchone()[0], 0)
        commands = RuntimeRequirements(f.store, clock=lambda: 101)
        fact = {'reference': 'example/repo#10', 'kind': 'issue', 'repository_id': 1, 'github_id': 110,
                'number': 10, 'state': 'open', 'state_reason': None, 'merged': False, 'updated_at': '2026-10-10T12:00:00Z'}
        f.github.prerequisite.side_effect = lambda reference, binding: dict(fact)
        for pin in job['members']:
            observed = collect(f.store, f.github, unit_id='batch', member_id=pin['member_id'],
                               requirements_revision_id=pin['requirements_revision_id'], body='Scope')
            self.assertEqual([row['number'] for row in observed['facts']], [10])
            self.assertFalse(commands.dependencies(f.command(), requirements_revision_id=pin['requirements_revision_id'],
                                                  generation=1, facts=observed['facts'])['clear'])
        with self.assertRaises(TransitionError):
            commands.dependencies(f.command(), requirements_revision_id='obsolete-revision', generation=2, facts=[fact])
        fact.update(state='closed', state_reason='completed')
        for index, pin in enumerate(job['members']):
            observed = collect(f.store, f.github, unit_id='batch', member_id=pin['member_id'],
                               requirements_revision_id=pin['requirements_revision_id'], body='Scope')
            self.assertTrue(commands.dependencies(f.command(), requirements_revision_id=pin['requirements_revision_id'],
                                                 generation=2, facts=observed['facts'])['clear'])
            with f.store.read_snapshot() as db:
                self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL").fetchone()[0], 1-index)
        self.assertEqual(f.jobs.job(f.dispatch.step('batch')['job_id'])['builder_stage'], 'planning')

    def test_owner_planning_and_integration_dependency_waits_preserve_every_member(self):
        from forge.factory.contracts import assignment_note
        for stage in ('planning', 'integration'):
            with self.subTest(stage=stage):
                job = self.waiting_assignment(stage); f = self.f
                if stage == 'planning':
                    self.assertNotIn('return needs_human_verification', assignment_note(job)['instructions'])
                outcome = f.capture(job['id'], outcome='waiting_on_issues', issues=[10], evidence='Depends on #10')
                self.assertTrue(outcome['accepted']); self.assertFalse(outcome['charged'])
                with f.store.read_snapshot() as db:
                    gates = list(db.execute("SELECT member_id,details_json FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL"))
                    self.assertEqual({row['member_id']: json.loads(row['details_json'])['requirements'] for row in gates},
                                     {pin['member_id']: pin['requirements'] for pin in job['members']})
                    self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0], 0)

    def test_owner_environment_waits_can_be_repaired_by_normal_member_overrides(self):
        from forge.store.runtime_requirements import RuntimeRequirements
        for stage in ('planning', 'integration'):
            with self.subTest(stage=stage):
                job = self.waiting_assignment(stage); f = self.f
                outcome = f.capture(job['id'], outcome='blocked_environment', blocker='services',
                                    required_services=['postgres'])
                self.assertTrue(outcome['accepted']); self.assertFalse(outcome['charged'])
                commands = RuntimeRequirements(f.store, clock=lambda: 101)
                for index, pin in enumerate(job['members']):
                    commands.set_override(f.command(), requirements_revision_id=pin['requirements_revision_id'],
                                          override={'mode': 'full', 'services': ['postgres']})
                    with f.store.read_snapshot() as db:
                        remaining = {row[0] for row in db.execute("SELECT member_id FROM unit_gates WHERE code='environment' AND resolved_at IS NULL")}
                        self.assertEqual(remaining, {row['member_id'] for row in job['members'][index+1:]})
                        self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0], 0)
                self.assertTrue(f.dispatch.step('batch')['reserved'])

    def test_joint_wait_rejects_own_scope_and_unassigned_quotes(self):
        from forge.factory.contracts import result
        job = self.waiting_assignment(); f = self.f
        report = dict(version=1, attempt=job['id'], role='build', outcome='waiting_on_issues', summary='Waiting',
                      delegation_plan=None, output_commit=None, issues=[1], evidence='Depends on #1')
        for pin in job['members']:
            with self.assertRaises(ValueError): result({**report, 'issues': [pin['issue_number']]}, job)
        # Well-shaped but ungrounded prose still fails normal result verification.
        rejected = f.capture(job['id'], outcome='waiting_on_issues', issues=[10], evidence='Invented #10 requirement')
        self.assertTrue(rejected['charged'])
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL").fetchone()[0], 0)

    def test_task_wait_does_not_approve_an_internal_member_dependency(self):
        from forge.factory.prerequisites import collect
        from forge.store.models import TransitionError
        f = fixtures.DeliveryPipelineTests('runTest')
        f.setUp(bodies={1: 'Depends on #2 before implementation'})
        self.addCleanup(f.doCleanups); self.f = f
        self.plan(lambda members: self.value(
            [self.definition('first', [row['member_id'] for row in members if row['issue_number'] == 1])],
            [row['member_id'] for row in members if row['issue_number'] == 2]))
        job = f.jobs.job(f.dispatch.step('batch')['job_id'])
        outcome = f.capture(job['id'], outcome='waiting_on_issues', issues=[2], evidence='Depends on #2')
        self.assertTrue(outcome['accepted']); self.assertFalse(outcome['charged'])
        pin = job['members'][0]
        with self.assertRaisesRegex(TransitionError, 'absent from the approved composition'):
            collect(f.store, f.github, unit_id='batch', member_id=pin['member_id'],
                    requirements_revision_id=pin['requirements_revision_id'], body='Depends on #2 before implementation')
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM member_dependencies').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='dependency' AND resolved_at IS NULL").fetchone()[0], 1)

    def test_owner_only_plan_goes_directly_to_one_integration(self):
        owner = self.plan(lambda members: self.value([], [row['member_id'] for row in members]))
        f = self.f
        integration = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertEqual(integration['builder_stage'], 'integration')
        self.assertEqual(integration['builder_id'], owner['builder_id'])
        self.assertEqual(integration['input_manifest']['part_outputs'], [])
        self.assertEqual(len(integration['members']), 2)
        f.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40, 'base': f.base}
        self.assertTrue(f.capture(integration['id'])['accepted'])
        self.assertEqual(f.dispatch.step('batch')['role'], 'review')

    def test_shared_task_needs_every_member_and_combines_only_its_environment_scope(self):
        owner = self.plan(lambda members: self.value([
            self.definition('shared', [row['member_id'] for row in members])]))
        f = self.f
        first, second = [row['member_id'] for row in owner['members']]
        f.config = validate({**f.config, 'memory': '4g', 'memory_tiers': ['4g', '8g', '16g'],
            'environment_tiers': {'full': {'memory': '8g', 'image': 'sha256:' + 'b' * 64, 'cpus': 2}}})
        f.dispatch = f.dispatcher()
        choices = {first: {'tier': 'full', 'services': ['postgres'], 'automatic': True}}
        f.dispatch.prepare = lambda snapshot, role: PreparedDispatch(f.base, role + '[bot]', f.config, member_choices=choices)
        self.assertFalse(f.dispatch.step('batch')['reserved'])
        choices[second] = {'tier': 'full', 'services': ['redis'], 'automatic': True}
        with f.store.write() as db:
            db.execute('INSERT INTO issue_environment_observations(issue_id,memory_retry_json) VALUES(?,?)',
                       (owner['members'][1]['issue_key'], json.dumps({'role': 'build', 'memory': '16g'})))
        task = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertEqual({row['member_id'] for row in task['members']}, {first, second})
        self.assertEqual(task['runner_config']['services'], ['postgres', 'redis'])
        self.assertEqual(task['runner_config']['memory'], '16384m')

    def test_same_member_tasks_run_independently_and_first_output_is_not_member_completion(self):
        owner = self.plan(lambda members: self.value([
            self.definition('first', [members[0]['member_id']]), self.definition('second', [members[0]['member_id']])],
            [members[1]['member_id']]))
        f = self.f
        first = f.jobs.job(f.dispatch.step('batch')['job_id'])
        second = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertNotEqual(first['task']['id'], second['task']['id'])
        self.assertEqual(first['members'], second['members'])
        for index, job in enumerate((first, second)):
            head, bundle = self.bundle('part-' + str(index))
            self.assertTrue(f.capture(job['id'], output_commit=head, bundle=bundle)['accepted'])
            with f.store.read_snapshot() as db:
                unit = dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
                contributions = member_contributions_in(db, unit, owner['members'][0]['member_id'])
                self.assertEqual(contributions['ready'], index == 1)
        integration = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertEqual(len(integration['input_manifest']['part_outputs']), 2)

    def test_owner_checkpoint_then_children_preserve_exact_graph_and_human_verification(self):
        owner = self.plan(lambda members: self.value([
            self.definition('checkpoint', [row['member_id'] for row in members], executor='owner'),
            self.definition('left', [members[0]['member_id']], depends_on=['checkpoint']),
            self.definition('right', [members[1]['member_id']], depends_on=['checkpoint'])]))
        f = self.f
        checkpoint = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertEqual(checkpoint['task']['executor'], 'owner')
        self.assertEqual(len(checkpoint['members']), 2)
        self.assertFalse(f.dispatch.step('batch')['reserved'])
        head, bundle = self.bundle('checkpoint')
        self.assertTrue(f.capture(checkpoint['id'], output_commit=head, bundle=bundle)['accepted'])
        children = [f.jobs.job(f.dispatch.step('batch')['job_id']) for _ in range(2)]
        request = {'what': 'Check the physical device', 'why': 'Needs hardware', 'checks': []}
        for index, child in enumerate(children):
            self.assertEqual([row['commit_oid'] for row in child['input_manifest']['prerequisite_outputs']], [head])
            child_head, child_bundle = self.bundle('child-' + str(index), head)
            fields = {'outcome': 'needs_human_verification', 'verification': request} if index == 0 else {}
            self.assertTrue(f.capture(child['id'], output_commit=child_head, bundle=child_bundle, **fields)['accepted'])
        integration = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertEqual(integration['builder_id'], owner['builder_id'])
        references = f.jobs.input_refs(integration)
        self.assertEqual(len(references), 3)
        self.assertEqual(references[0]['job_id'], checkpoint['id'])
        self.assertEqual(len(integration['input_manifest']['verification_requests']), 1)
        f.github.publication.return_value = {'pr': 9, 'branch': integration['branch'], 'head': 'c' * 40, 'base': f.base}
        self.assertTrue(f.capture(integration['id'])['accepted'])
        with f.store.read_snapshot() as db:
            gate = db.execute("SELECT * FROM unit_gates WHERE unit_id='batch' AND code='verification_human' AND resolved_at IS NULL").fetchone()
            from forge.store.verification_requests import gate_details_in
            self.assertIn(request['what'], gate_details_in(db, gate)['what'])
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='charge'").fetchone()[0], 0)

    def test_recovery_preserves_all_failed_task_pins_beside_independent_active_task(self):
        from forge.factory.recovery_controller import RecoveryController
        owner = self.plan(lambda members: self.value([
            self.definition('failed', [row['member_id'] for row in members]),
            self.definition('independent', [members[0]['member_id']])]))
        f = self.f
        failed = f.jobs.job(f.dispatch.step('batch')['job_id'])
        independent = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertTrue(f.capture(failed['id'], outcome='unable')['charged'])
        recovery = RecoveryController(f.store, f.runner, f.github, config=f.config,
                                      admission=lambda _: True, clock=lambda: 100)
        identifier = recovery.schedule(failed['id'])['job_id']
        investigation = f.jobs.job(identifier)
        self.assertEqual(investigation['input_manifest']['members'],
            [{'member_id': row['member_id'], 'requirements_revision_id': row['requirements_revision_id']}
             for row in failed['members']])
        self.assertEqual(investigation['members'], failed['members'])
        self.assertFalse(f.jobs.job(independent['id'])['fenced'])
        f.github.publication.reset_mock()
        result = f.capture(identifier, classifier=recovery.finish, pattern='Concrete cause',
                           evidence=['Observed failure'], uncertainty='', alternative=None)
        self.assertTrue(result['accepted'])
        f.github.publication.assert_not_called()
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM budget_entries WHERE kind='charge'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT investigation FROM attempt_failures WHERE attempt_id=?", (failed['attempt_id'],)).fetchone()[0], 'complete')

    def test_environment_conflict_requests_replan_without_lease_or_charge(self):
        owner = self.plan(lambda members: self.value([
            self.definition('shared', [row['member_id'] for row in members])]))
        f = self.f
        a, b = [row['member_id'] for row in owner['members']]
        choices = {a: {'tier': 'standard', 'automatic': False, 'services': []},
                   b: {'tier': 'full', 'automatic': True, 'services': ['postgres']}}
        with f.store.write() as db:
            for revision, member in enumerate(owner['members'], 1):
                db.execute('INSERT INTO unit_environment(unit_id,revision,choice,requirements_revision_id,config_hash,created_at,member_id) VALUES(?,?,?,?,?,?,?)',
                    ('batch', revision, json.dumps(choices[member['member_id']]), member['requirements_revision_id'], 'config', 100, member['member_id']))
        f.dispatch.prepare = lambda snapshot, role: PreparedDispatch(f.base, role + '[bot]', f.config, member_choices=choices)
        result = f.dispatch.step('batch')
        self.assertEqual(result['reason'], 'environment_conflict')
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)
        replan = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertEqual(replan['builder_stage'], 'planning')
        self.assertEqual(replan['input_manifest']['environment_conflicts'][0]['choices'], choices)
        self.assertEqual(replan['runner_config']['environment_tier'], 'standard')
        # Repeating the same incompatible split cannot launch a planning loop.
        self.assertTrue(f.capture(replan['id'], outcome='delegate', delegation_plan=self.value([
            self.definition('same-shared', [a, b])]))['accepted'])
        self.assertFalse(f.dispatch.step('batch')['reserved'])
        with f.store.read_snapshot() as db:
            gate = db.execute("SELECT details_json FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL").fetchone()
            self.assertEqual(json.loads(gate[0])['choices'], choices)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)

    def test_failed_task_wait_does_not_block_independent_task_dispatch(self):
        self.plan(lambda members: self.value([
            self.definition('first', [members[0]['member_id']]),
            self.definition('second', [members[1]['member_id']])]))
        f = self.f
        failed = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertTrue(f.capture(failed['id'], outcome='unable')['charged'])
        with f.store.read_snapshot() as db:
            wait = db.execute("SELECT job_id FROM unit_gates WHERE code='recovery_pending' AND resolved_at IS NULL").fetchone()
            self.assertEqual(wait[0], failed['id'])
        independent = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertNotEqual(independent['task']['id'], failed['task']['id'])
        head, bundle = self.bundle('independent-after-failure')
        self.assertTrue(f.capture(independent['id'], output_commit=head, bundle=bundle)['accepted'])
        # New owner work must still wait for the failed task's investigation.
        self.assertFalse(f.dispatch.step('batch')['reserved'])
        with f.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM builder_assignments WHERE stage='planning'").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0], 1)

    def test_integration_environment_conflict_records_operator_decision(self):
        owner = self.plan(lambda members: self.value([], [row['member_id'] for row in members]))
        f = self.f
        a, b = [row['member_id'] for row in owner['members']]
        choices = {a: {'tier': 'standard', 'automatic': False, 'services': []},
                   b: {'tier': 'full', 'automatic': True, 'services': ['postgres']}}
        with f.store.write() as db:
            for revision, member in enumerate(owner['members'], 1):
                db.execute('INSERT INTO unit_environment(unit_id,revision,choice,requirements_revision_id,config_hash,created_at,member_id) VALUES(?,?,?,?,?,?,?)',
                    ('batch', revision, json.dumps(choices[member['member_id']]), member['requirements_revision_id'], 'config', 100, member['member_id']))
        f.dispatch.prepare = lambda snapshot, role: PreparedDispatch(f.base, role + '[bot]', f.config, member_choices=choices)
        result = f.dispatch.step('batch')
        self.assertEqual(result['reason'], 'environment_conflict')
        with f.store.read_snapshot() as db:
            gate = db.execute("SELECT details_json FROM unit_gates WHERE code='decision_required' AND resolved_at IS NULL").fetchone()
            details = json.loads(gate[0])
            self.assertIsNone(details['task_id'])
            self.assertEqual(details['choices'], choices)
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)
        self.assertFalse(f.dispatch.step('batch')['reserved'])

    def test_repeated_task_failure_pauses_its_scope_but_independent_task_can_dispatch(self):
        from forge.factory.recovery_controller import RecoveryController
        owner = self.plan(lambda members: self.value([
            self.definition('first', [members[0]['member_id']]),
            self.definition('second', [members[0]['member_id']]),
            self.definition('independent', [members[1]['member_id']])]))
        f = self.f
        affected, independent = [row['member_id'] for row in owner['members']]
        choices = {affected: {'tier': 'standard', 'automatic': True, 'services': []}}
        f.dispatch.prepare = lambda snapshot, role: PreparedDispatch(f.base, role + '[bot]', f.config, member_choices=choices)
        jobs = [f.jobs.job(f.dispatch.step('batch')['job_id']) for _ in range(2)]
        recovery = RecoveryController(f.store, f.runner, f.github, config=f.config,
                                      admission=lambda _: True, clock=lambda: 100)
        for index, job in enumerate(jobs):
            self.assertEqual({pin['member_id'] for pin in job['members']}, {affected})
            self.assertTrue(f.capture(job['id'], outcome='unable')['charged'])
            identifier = recovery.schedule(job['id'])['job_id']
            result = f.capture(identifier, classifier=recovery.finish, pattern='Same concrete cause',
                               evidence=['Observed cause'], uncertainty='', alternative=None)
            self.assertEqual(result['approach_required'], index == 1)
        with f.store.read_snapshot() as db:
            gates = db.execute("SELECT member_id FROM unit_gates WHERE code='approach_required' AND resolved_at IS NULL").fetchall()
            self.assertEqual([gate[0] for gate in gates], [affected])
            self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0], 2)
        choices[independent] = {'tier': 'standard', 'automatic': True, 'services': []}
        next_job = f.jobs.job(f.dispatch.step('batch')['job_id'])
        self.assertEqual(next_job['task']['key'], 'independent')
        self.assertEqual({pin['member_id'] for pin in next_job['members']}, {independent})

    def test_repeated_shared_task_failure_records_every_affected_member(self):
        from forge.factory.recovery_controller import RecoveryController
        owner = self.plan(lambda members: self.value([
            self.definition(key, [row['member_id'] for row in members]) for key in ('one', 'two')]))
        f = self.f
        jobs = [f.jobs.job(f.dispatch.step('batch')['job_id']) for _ in range(2)]
        recovery = RecoveryController(f.store, f.runner, f.github, config=f.config,
                                      admission=lambda _: True, clock=lambda: 100)
        for job in jobs:
            self.assertTrue(f.capture(job['id'], outcome='unable')['charged'])
            identifier = recovery.schedule(job['id'])['job_id']
            f.capture(identifier, classifier=recovery.finish, pattern='Same cause',
                      evidence=['Observed cause'], uncertainty='', alternative=None)
        with f.store.read_snapshot() as db:
            gates = db.execute("SELECT member_id FROM unit_gates WHERE code='approach_required' AND resolved_at IS NULL").fetchall()
            self.assertEqual({gate[0] for gate in gates}, {pin['member_id'] for pin in owner['members']})
            self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='charge'").fetchone()[0], 2)
