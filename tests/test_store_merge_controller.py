"""Merge authorization uses current GitHub facts and actual lifecycle evidence."""
from copy import deepcopy
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.config import policy, validate
from forge.factory.effect_controller import EffectController
from forge.factory.github import GitHub
from forge.factory.merge_controller import MergeAdmission, MergeController, candidate_snapshot
from forge.factory.verification_controller import VerificationController
from forge.store.db import Store
from forge.store.effects import EffectQueries
from forge.store.executions import Executions
from forge.store.models import Candidate, Command, TransitionError
from forge.store.transitions import manifest_hash, project_unit


HEAD = 'a' * 40
BASE = 'b' * 40


class Remote:
    # Exercise the production latest-review and owned-check logic too, while
    # replacing only the network reads and protected-branch observation.
    approvals = GitHub.approvals
    gates = GitHub.gates

    def __init__(self, store, runtime):
        self.store, self.runtime = store, runtime
        self.requirements = 'github-requirements'
        self.issue_value = {'number': 1, 'state': 'open', 'labels': [], 'assignees': []}
        self.pull = dict(number=10, state='open', draft=False, merged=False, mergeable=True,
            head={'sha': HEAD, 'repo': {'id': 1}}, base={'sha': BASE, 'ref': 'main', 'repo': {'id': 1}},
            user={'login': 'builder[bot]', 'type': 'Bot'})
        self.reviews = []
        self.runs = [dict(id=11, name='forge/review', app={'id': 101}, head_sha=HEAD, status='completed', conclusion='success'),
                     dict(id=12, name='forge/audit', app={'id': 102}, head_sha=HEAD, status='completed', conclusion='success')]
        self.ci = {'head': HEAD, 'base': BASE, 'observed_at': 300,
                   'checks': {HEAD: [dict(id=13, name='Tests', app_id=201, status='completed', conclusion='success')]}}
        self.protected = True
        self.branch = 'main'
        self.reads = []
        self.open_pulls = []
        self.on_check = None
        self.merge_calls = 0
        self.prerequisites = {}
        self.bindings_read = []

    def probe(self, kind):
        self.reads.append(kind)
        # A second writer proves remote IO is outside every SQL transaction.
        with self.store.write() as db:
            db.execute("INSERT OR REPLACE INTO store_metadata VALUES('merge_remote_probe','ok')")

    def approve(self, *, state='APPROVED', identifier=1, login='operator', commit=HEAD, at=200):
        from datetime import datetime, timezone
        self.reviews.append(dict(id=identifier, state=state, user={'login': login, 'type': 'User'},
                                 commit_id=commit, submitted_at=datetime.fromtimestamp(at, timezone.utc).isoformat()))

    def pages(self, role, item, path, key=None):
        self.probe(path)
        return deepcopy(self.reviews if path.endswith('/reviews') else self.runs)

    def connection(self, role):
        return {'app_id': {'review': 101, 'audit': 102}[role]}

    def can_write(self, item, login):
        return login in {'operator', 'other'}

    def issue(self, item):
        self.probe('issue')
        return deepcopy(self.issue_value), self.requirements

    def pulls(self, item):
        self.probe('pulls')
        return deepcopy(self.open_pulls)

    def prerequisite(self, reference, binding=None):
        self.probe('prerequisite')
        self.bindings_read.append(deepcopy(binding))
        return deepcopy(self.prerequisites[reference])

    def pr(self, item, number):
        self.probe('pr')
        return deepcopy(self.pull)

    def target(self, item):
        self.probe('target')
        return self.branch

    def protections(self, item, candidate):
        self.probe('protections')
        return self.protected

    def ci_evidence(self, item):
        self.probe('ci')
        return deepcopy(self.ci)

    def check(self, item, role, candidate, external_id, conclusion, summary):
        self.probe('publish-check')
        if self.on_check:
            self.on_check()
        return 99

    def merge(self, item):
        self.probe('merge')
        self.merge_calls += 1
        self.pull.update(merged=True, state='closed', merge_commit_sha='c' * 40)
        return True


class MergeControllerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now, self.serial = 100.0, 0
        self.life = Executions(self.store, clock=lambda: self.now)
        self.config = validate({'enabled': True, 'merge_enabled': True, 'image': 'sha256:' + '1' * 64})
        self.ready = True
        self.config_reads = self.admission_reads = 0
        self.remote = Remote(self.store, self.config)
        self.admission = MergeAdmission(self.store, self.remote, config=self.current_config,
                                        repository_ready=lambda repo: self.ready, clock=lambda: self.now)
        self.controller = MergeController(self.store, admission=self.authorize, clock=lambda: self.now)
        self.admitted = self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
            repository_id=1, repository_name='owner/repo', issue_number=1, title='Issue', body='',
            requirements=self.remote.requirements, observed_at=90)

    def current_config(self):
        self.config_reads += 1
        return deepcopy(self.config)

    def test_merge_retry_uses_configured_schedule(self):
        from forge.store.scheduling import Scheduling
        self.prepare_candidate()
        self.ready = False
        self.controller.scheduling = Scheduling(self.store, clock=lambda: self.now, retry_seconds=75)
        self.assertEqual(self.controller.step('u')['pending'], 'authorization')
        before = self.admission_reads
        self.now += 74
        self.assertEqual(self.controller.step('u')['retry_at'], self.now + 1)
        self.assertEqual(self.admission_reads, before)
        self.now += 1
        self.controller.step('u')
        self.assertEqual(self.admission_reads, before + 1)

    def authorize(self, effect):
        self.admission_reads += 1
        return self.admission(effect)

    def view(self):
        with self.store.read_snapshot() as db:
            return project_unit(db, 'u')

    def command(self):
        self.serial += 1
        return Command('command-' + str(self.serial), 'operator', 'u', self.view()['version'])

    def identity(self):
        with self.store.read_snapshot() as db:
            return dict(db.execute('SELECT * FROM candidates WHERE id=?', (self.view()['current_candidate_id'],)).fetchone())

    def prepare_candidate(self, *, human_required=False, audit_required=False):
        self.config['repository_merge_policies']={'1':'human' if human_required else 'auto'}
        build_manifest = dict(requirements_revision_id=self.admitted['revision_id'], base_oid=BASE,
                              policy_hash=manifest_hash(policy(self.config,1,repository_name='owner/repo')))
        job = self.life.reserve_job(self.command(), 'build', slot='build-1', execution_fence='build-fence',
                                   input_manifest=build_manifest, member_id=self.admitted['member_id'])['job_id']
        candidate = Candidate(10, 'forge/issue-1-test', HEAD, BASE, build_manifest['policy_hash'],
                              manifest_hash(self.remote.requirements), manifest_hash(build_manifest))
        self.life.finish_job(self.command(), job, execution_fence='build-fence', input_manifest=build_manifest,
                             evidence={'head_oid': HEAD, 'base_oid': BASE, 'pull_number': 10}, candidate=candidate)
        self.life.verify_cleanup(self.command(), job, execution_fence='build-fence',
                                 evidence={'process_stopped': True, 'resources_released': True})
        identity = self.identity()
        for role, check in [('review', 11), ('audit', 12)]:
            manifest = {key: identity[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
            manifest['candidate_id'] = identity['id']
            job = self.life.reserve_job(self.command(), role, slot=role + '-1', execution_fence=role + '-fence',
                                       input_manifest=manifest, candidate_id=identity['id'])['job_id']
            report = {'candidate': identity['id'], 'outcome': 'pass'}
            if role == 'audit':
                report['human_required'] = 'required' if audit_required else 'not_required'
            gates = [{'code': 'approval_required', 'details': {'source': 'audit'}}] if role == 'audit' and audit_required else []
            self.life.finish_job(self.command(), job, execution_fence=role + '-fence', input_manifest=manifest,
                evidence={'candidate_id': identity['id'], 'verdict': 'pass', 'report': report, 'check': {'check_id': check}}, gates=gates)
            self.life.verify_cleanup(self.command(), job, execution_fence=role + '-fence',
                                     evidence={'process_stopped': True, 'resources_released': True})
        self.now = 300
        return {'unit_id': 'u', 'candidate_id': identity['id']}

    def test_explicit_human_policy_waits_for_current_counted_approval(self):
        effect = self.prepare_candidate(human_required=True)
        self.assertIsNone(self.admission(effect))
        self.remote.approve(commit='d' * 40)
        self.assertIsNone(self.admission(effect))
        self.remote.approve(identifier=2)
        proof = self.admission(effect)
        self.assertTrue(proof['human_approved'])
        self.assertEqual(proof['observed_at'], 300)
        self.assertTrue(self.remote.reads[-1].endswith('/reviews'))

    def test_explicit_automatic_policy_still_requires_technical_evidence_and_audit_permission(self):
        effect = self.prepare_candidate(human_required=False)
        proof = self.admission(effect)
        self.assertTrue(proof['approved'])
        self.assertFalse(proof['human_approved'])
        self.remote.runs.pop()
        self.assertIsNone(self.admission(effect))

    def test_initial_conflict_rebuilds_without_ever_authorizing_an_effect(self):
        self.controller=MergeController(self.store,admission=self.admission,clock=lambda:self.now)
        self.prepare_candidate(human_required=False)
        self.remote.pull['mergeable']=False
        self.assertEqual(self.controller.step('u')['pending'],'merge_conflict')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
        self.now+=900
        self.assertTrue(self.controller.step('u')['rebuilding'])
        self.assertEqual(self.view()['state'],'building')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)
        self.assertEqual(self.remote.merge_calls,0)

    def test_initial_unknown_escalates_and_fresh_ready_releases_only_its_own_gate(self):
        self.controller=MergeController(self.store,admission=self.admission,clock=lambda:self.now)
        self.prepare_candidate(human_required=False)
        self.remote.pull['mergeable']=None
        self.assertEqual(self.controller.step('u')['pending'],'mergeability_unknown')
        self.now+=900
        self.assertEqual(self.controller.step('u')['pending'],'mergeability_unknown')
        self.assertEqual(self.view()['reason_code'],'forge_problem')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
        self.life.pause(self.command())
        self.remote.pull['mergeable']=True
        with self.assertRaises(TransitionError):
            self.controller.step('u')
        self.assertEqual(self.view()['reason_code'],'operator_pause')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE code='forge_problem' AND resolved_at IS NULL").fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],0)
        self.life.resume(self.command())
        self.assertIn('effect_id',self.controller.step('u'))
        self.assertEqual(self.remote.merge_calls,0)

    def test_initial_conflict_bound_resets_after_unknown_observation(self):
        self.controller=MergeController(self.store,admission=self.admission,clock=lambda:self.now)
        self.prepare_candidate(human_required=False)
        self.remote.pull['mergeable']=False
        self.controller.step('u')
        self.now+=600
        self.remote.pull['mergeable']=None
        self.controller.step('u')
        self.now+=30
        self.remote.pull['mergeable']=False
        self.controller.step('u')
        self.now+=300
        self.assertEqual(self.controller.step('u')['pending'],'merge_conflict')
        self.assertEqual(self.view()['state'],'merging')
        self.now+=600
        self.assertTrue(self.controller.step('u')['rebuilding'])

    def test_audit_requires_human_and_gate_is_resolved_before_durable_queue(self):
        self.prepare_candidate(human_required=False, audit_required=True)
        self.assertEqual(self.controller.step('u')['pending'], 'authorization')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0], 0)
        self.remote.approve()
        self.now += 30
        queued = self.controller.step('u')
        effect = EffectQueries(self.store).get(queued['effect_id'])
        self.assertEqual(effect['status'], 'pending')
        self.assertEqual(effect['kind'], 'github_merge')
        self.assertEqual(effect['candidate_id'], self.identity()['id'])
        self.assertEqual(self.remote.merge_calls, 0)
        with self.store.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT resolved_at FROM unit_gates WHERE code='approval_required'").fetchone()[0])
            self.assertEqual(db.execute('SELECT count(*) FROM merge_requests').fetchone()[0], 1)
        restarted = MergeController(self.store, admission=self.authorize, clock=lambda: self.now)
        self.assertEqual(restarted.step('u')['effect_id'], queued['effect_id'])
        self.assertEqual(len(EffectQueries(self.store).pending()), 1)

    def test_dismissed_approval_and_any_latest_requested_changes_block(self):
        effect = self.prepare_candidate(human_required=True)
        self.remote.approve()
        self.assertIsNotNone(self.admission(effect))
        self.remote.approve(identifier=2, state='DISMISSED')
        self.assertIsNone(self.admission(effect))
        self.remote.approve(identifier=3)
        self.remote.approve(identifier=4, login='other', state='CHANGES_REQUESTED')
        self.assertIsNone(self.admission(effect))

    def test_each_candidate_and_remote_identity_mismatch_fails_closed(self):
        effect = self.prepare_candidate()
        original = deepcopy(self.remote.pull)
        cases = [('number', None, 11), ('head', 'sha', 'c' * 40), ('base', 'sha', 'c' * 40),
                 ('base', 'ref', 'different'), ('head', 'repo', {'id': 2}), ('base', 'repo', {'id': 2}),
                 ('draft', None, True), ('merged', None, True), ('mergeable', None, None), ('state', None, 'closed')]
        for field, child, value in cases:
            with self.subTest(field=field, child=child):
                self.remote.pull = deepcopy(original)
                if child:
                    self.remote.pull[field][child] = value
                else:
                    self.remote.pull[field] = value
                self.assertIsNone(self.admission(effect))
        self.remote.pull = original
        self.assertIsNone(self.admission({**effect, 'candidate_id': 'another'}))
        self.remote.branch = 'different'
        self.assertIsNone(self.admission(effect))

    def test_current_requirements_policy_readiness_and_holds_are_rechecked(self):
        effect = self.prepare_candidate()
        for field, value in [('requirements', 'new requirements'), ('protected', False)]:
            before = getattr(self.remote, field)
            setattr(self.remote, field, value)
            self.assertIsNone(self.admission(effect))
            setattr(self.remote, field, before)
        for field, value in [('labels', [{'name': 'hold'}]), ('assignees', [{'login': 'person', 'type': 'User'}]), ('state', 'closed')]:
            before = self.remote.issue_value[field]
            self.remote.issue_value[field] = value
            self.assertIsNone(self.admission(effect))
            self.remote.issue_value[field] = before
        self.ready = False
        self.assertIsNone(self.admission(effect))
        self.ready = True
        self.config['merge_method'] = 'rebase'
        self.assertIsNone(self.admission(effect))
        self.config['merge_method'] = 'squash'
        self.config['merge_enabled'] = False
        self.assertIsNone(self.admission(effect))
        self.assertGreaterEqual(self.config_reads, 8)

    def test_exact_owned_check_ids_and_latest_status_are_required(self):
        effect = self.prepare_candidate()
        original = deepcopy(self.remote.runs)
        for field, value in [('id', 15), ('app', {'id': 999}), ('status', 'in_progress'), ('conclusion', 'failure')]:
            with self.subTest(field=field):
                self.remote.runs = deepcopy(original)
                self.remote.runs[0][field] = value
                self.assertIsNone(self.admission(effect))
        self.remote.runs = original + [{**original[0], 'id': 100, 'conclusion': 'failure'}]
        self.assertIsNone(self.admission(effect))

    def test_resolved_ci_verification_is_checked_again_against_exact_commits(self):
        effect = self.prepare_candidate()
        gate = self.life.open_gate(self.command(), 'verification_ci', candidate_id=effect['candidate_id'],
            details={'ci': [{'name': 'Tests', 'app_id': 201}]})['gate_id']
        self.assertIsNone(self.admission(effect))
        verifier = VerificationController(self.store, self.remote, profiles=lambda repo: [{'name': 'Tests', 'app_id': 201}],
            policy=lambda item: policy(self.config,1,repository_name='owner/repo'), clock=lambda: self.now)
        verifier.step(gate)
        self.assertIsNotNone(self.admission(effect))
        self.remote.ci['head'] = 'd' * 40
        self.assertIsNone(self.admission(effect))
        self.remote.ci['head'] = HEAD
        self.remote.ci['checks'][HEAD][0]['app_id'] = 202
        self.assertIsNone(self.admission(effect))

    def test_human_verification_requires_approval_after_explicit_confirmation(self):
        effect = self.prepare_candidate()
        gate = self.life.open_gate(self.command(), 'verification_human', candidate_id=effect['candidate_id'],
                                   details={'summary': 'Verify migration'})['gate_id']
        self.remote.approve(at=325)
        self.now = 350
        verifier = VerificationController(self.store, self.remote, profiles=lambda repo: [],
            policy=lambda item: policy(self.config,1,repository_name='owner/repo'), clock=lambda: self.now)
        verifier.confirm_human(self.command(), gate, statement='Verified migration on the current revision.')
        self.assertIsNone(self.admission(effect))
        self.now = 400
        self.remote.approve(identifier=2, at=360)
        self.assertTrue(self.admission(effect)['human_approved'])

    def test_effect_driver_rechecks_approval_after_publishing_controller_check(self):
        self.prepare_candidate(human_required=True)
        self.remote.approve()
        self.now += 30
        queued = self.controller.step('u')
        self.assertEqual(self.admission_reads, 1)
        self.remote.on_check = lambda: self.remote.approve(identifier=2, state='DISMISSED')
        driver = EffectController(self.store, self.remote, admission=self.authorize, clock=lambda: self.now)
        driver.step(queued['effect_id'])
        self.assertEqual(self.admission_reads, 3)
        self.assertEqual(self.remote.merge_calls, 0)
        self.assertNotEqual(self.view()['state'], 'merged')

    def test_missing_human_policy_fails_closed(self):
        effect = self.prepare_candidate()
        self.config.pop('human_required')
        self.assertIsNone(self.admission(effect))

    def test_pause_during_admission_cannot_queue_merge(self):
        self.prepare_candidate()
        def admission(effect):
            proof = self.authorize(effect)
            self.life.pause(self.command())
            return proof
        controller = MergeController(self.store, admission=admission, clock=lambda: self.now)
        with self.assertRaises(TransitionError):
            controller.step('u')
        self.assertEqual(EffectQueries(self.store).pending(), [])

    def test_fresh_approval_observation_is_visible_for_exact_candidate(self):
        from forge.factory.store_views import PipelineViews
        self.prepare_candidate(human_required=True)
        controller=MergeController(self.store, admission=self.admission, clock=lambda:self.now)
        self.remote.approve(state='CHANGES_REQUESTED')
        self.assertEqual(controller.step('u')['pending'], 'authorization')
        row=PipelineViews(self.store,runtime=self.config,clock=lambda:self.now).issues(key='i')['issue']
        self.assertTrue(any(review['state']=='CHANGES_REQUESTED' for review in row['approvals']))
        self.remote.approve(identifier=2)
        self.now += 30
        self.assertIn('effect_id', controller.step('u'))
        row=PipelineViews(self.store,runtime=self.config,clock=lambda:self.now).issues(key='i')['issue']
        self.assertTrue(any(review['counted'] for review in row['approvals']))

    def prerequisite(self, reference='other/repo#2', **changes):
        fact=dict(reference=reference, repository_id=2, github_id=202, number=2,
                  kind='issue', state='closed', state_reason='completed', merged=False,
                  updated_at='2026-10-10T00:00:00Z') | changes
        self.remote.prerequisites[reference]=fact
        with self.store.write() as db:
            db.execute('INSERT INTO prerequisite_identities VALUES(?,?,?,?,?,?)',
                (reference, fact['repository_id'], fact['github_id'], fact['number'], fact['kind'], 100))
        return fact

    def test_merge_prerequisites_are_fresh_typed_and_bound(self):
        self.remote.issue_value['body']='Depends on: other/repo#2'
        fact=self.prerequisite()
        effect=self.prepare_candidate()
        self.assertIsNotNone(self.admission(effect))
        for change in ({'state':'open'}, {'state_reason':'not_planned'}, {'state_reason':'duplicate'},
                       {'state_reason':None}, {'repository_id':3}, {'github_id':999},
                       {'number':3}, {'kind':'pull_request', 'merged':True},
                       {'error':'prerequisite_inaccessible'}):
            with self.subTest(change=change):
                self.remote.prerequisites[fact['reference']]=fact | change
                self.assertIsNone(self.admission(effect))
        self.assertTrue(all(binding['github_id']==202 for binding in self.remote.bindings_read))

    def test_closed_pr_requires_merge_and_unknown_binding_cannot_authorize_merge(self):
        self.remote.issue_value['body']='Depends on: other/repo#2, other/repo#3'
        first=self.prerequisite(kind='pull_request')
        self.remote.prerequisites['other/repo#3']=dict(first, reference='other/repo#3', number=3, github_id=203, kind='issue')
        effect=self.prepare_candidate()
        self.assertIsNone(self.admission(effect))
        first['merged']=True
        self.assertIsNone(self.admission(effect))
        self.prerequisite('other/repo#3', number=3, github_id=203)
        self.assertIsNotNone(self.admission(effect))

    def test_prerequisite_reopened_after_check_prevents_merge(self):
        self.remote.issue_value['body']='Depends on: other/repo#2'
        fact=self.prerequisite()
        self.prepare_candidate()
        queued=self.controller.step('u')
        self.remote.on_check=lambda: fact.update(state='open')
        driver=EffectController(self.store,self.remote,admission=self.admission,clock=lambda:self.now)
        driver.step(queued['effect_id'])
        self.assertEqual(self.remote.merge_calls,0)

    def test_renamed_self_reference_and_persisted_order_are_checked(self):
        from forge.store.repository_requirements import order_in
        with self.store.write() as db:
            order_in(db, issue_id='i', requirements_revision_id=self.admitted['revision_id'],
                prerequisites=['old/name#1'], actor_id='operator', request_id='order')
            # Model the observer's earlier completion, before the remote reopens.
            db.execute("UPDATE unit_gates SET resolved_at=100,resolution='observation' WHERE code='dependency'")
        self.prerequisite('old/name#1',repository_id=1,number=1,github_id=101)
        effect=self.prepare_candidate()
        self.assertIsNone(self.admission(effect))

    def test_persisted_prerequisite_is_rechecked_without_a_body_reference(self):
        self.life.admit_issue(request_id='dependency-admit',actor_id='operator',unit_id='other-unit',issue_id='other-issue',
            repository_id=2,repository_name='other/repo',issue_number=2,title='Prerequisite',body='',requirements='other',observed_at=90)
        self.life.observe_issue(Command('dependency-closed','observer','other-unit',1),'other-issue',
            state='closed',state_reason='completed',title='Prerequisite',body='',requirements='other',
            remote_updated_at='2026-10-10T00:00:00Z',generation=1)
        with self.store.write() as db:
            db.execute('INSERT INTO issue_dependencies VALUES(?,?,?,?,?,?,?)',
                ('dependency','i',self.admitted['revision_id'],'other-issue','operator','{}',100))
        fact=self.prerequisite()
        effect=self.prepare_candidate()
        self.assertIsNotNone(self.admission(effect))
        fact['state']='open'
        self.assertIsNone(self.admission(effect))

    def test_admission_never_writes_or_advances_versions(self):
        self.remote.issue_value['body']='Depends on: other/repo#2'
        self.prerequisite()
        effect=self.prepare_candidate(human_required=True)
        self.remote.approve()
        before=self.view()['version']
        self.remote.probe=lambda kind:self.remote.reads.append(kind)
        with patch.object(self.store,'write',side_effect=AssertionError('admission wrote to store')):
            observed=self.admission.evaluate_observation(effect)
        self.assertTrue(observed['authorization']['approved'])
        self.assertTrue(observed['approval_observation']['reviews'][0]['counted'])
        self.assertEqual(before,self.view()['version'])


if __name__ == '__main__':
    unittest.main()
