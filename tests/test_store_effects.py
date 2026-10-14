import json
import tempfile
import unittest
from unittest.mock import Mock

from forge.factory.effect_controller import EffectController
from forge.factory.github import GitHubUnavailable
from forge.store.db import Store
from forge.store.effects import Effects, EffectQueries
from forge.store.executions import Executions, ExecutionQueries, Launch
from forge.store.models import Candidate, Command, IdempotencyConflict, TransitionError, VersionConflict
from forge.store.transitions import manifest_hash, project_unit


class Remote:
    def __init__(self, store):
        self.store = store
        self.pull = {'number': 1, 'head': {'sha': 'head', 'repo': {'id': 1}},
                     'base': {'sha': 'base', 'ref': 'main', 'repo': {'id': 1}},
                     'state': 'open', 'merged': False, 'mergeable': True, 'draft': False}
        self.merge_calls = 0
        self.verdict_calls = 0
        self.check_calls = 0
        self.checks = {}
        self.lose_verdict = False
        self.lose_merge = False
        self.merge_visible = True
        self.on_check = None
        self.on_merge = None

    def outside_transaction(self):
        # A write on a second connection proves no driver transaction spans IO.
        with self.store.write() as db:
            db.execute("INSERT OR REPLACE INTO store_metadata VALUES('test_remote_probe','ok')")

    def pr(self, item, number):
        self.outside_transaction()
        return dict(self.pull)

    def verdict(self, job, report):
        self.outside_transaction()
        self.verdict_calls += 1
        with self.store.read_snapshot() as db:
            effect = db.execute('SELECT e.* FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id '
                                'WHERE c.job_id=?', (job['id'],)).fetchone()
            assert effect['status'] == 'running' and effect['attempts'] >= 1
        check = self.checks.setdefault(job['id'], len(self.checks) + 10)
        if self.lose_verdict:
            self.lose_verdict = False
            raise GitHubUnavailable('lost response')
        return check

    def check(self, item, role, candidate, external_id, conclusion, summary):
        self.outside_transaction()
        self.check_calls += 1
        if self.on_check:
            self.on_check()
        return self.checks.setdefault(external_id, len(self.checks) + 10)

    def merge(self, item):
        self.outside_transaction()
        self.merge_calls += 1
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT status FROM merge_requests').fetchone()
            assert row[0] == 'uncertain'
        if self.on_merge:
            self.on_merge()
        if self.merge_visible:
            self.pull.update(merged=True, state='closed', merge_commit_sha='merge-sha')
        if self.lose_merge:
            self.lose_merge = False
            raise GitHubUnavailable('lost merge response')
        return True


class EffectTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now = 100.0
        self.life = Executions(self.store, clock=lambda: self.now)
        self.effects = Effects(self.store, clock=lambda: self.now)
        self.queries = EffectQueries(self.store)
        self.serial = 0
        admitted = self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
            repository_id=1, repository_name='owner/repo', issue_number=1, title='Issue', body='Body',
            requirements={'acceptance': ['works']}, observed_at=90)
        self.member = admitted['member_id']
        candidate = Candidate(1, 'forge/issue-1', 'head', 'base', 'policy',
                              manifest_hash({'acceptance': ['works']}), 'input-hash')
        evidence = {key: getattr(candidate, key) for key in
                    ('pull_number', 'head_oid', 'base_oid', 'policy_hash', 'input_manifest_hash')}
        self.candidate_id = self.life.adopt_candidate(self.command(), candidate,
                                    evidence={**evidence, 'approved': True, 'verified': True})['candidate_id']
        self.remote = Remote(self.store)
        self.driver = EffectController(self.store, self.remote, admission=lambda effect: self.authorization(), clock=lambda: self.now)

    def view(self):
        with self.store.read_snapshot() as db:
            return project_unit(db, 'u')

    def command(self):
        self.serial += 1
        return Command(str(self.serial), 'operator', 'u', self.view()['version'])

    def identity(self):
        with self.store.read_snapshot() as db:
            return dict(db.execute('SELECT * FROM candidates WHERE id=?', (self.candidate_id,)).fetchone())

    def authorization(self):
        candidate = self.identity()
        return {**{key: candidate[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash')},
                'candidate_id': candidate['id'], 'verified': True, 'merge_enabled': True, 'approved': True,
                'checks_passed': True, 'protection_ready': True, 'observed_at': self.now}

    def captured(self, role='review'):
        identity = self.identity()
        manifest = {key: identity[key] for key in ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')}
        manifest['candidate_id'] = identity['id']
        job_id = self.life.reserve_job(self.command(), role, slot='slot', execution_fence='fence-' + role,
            input_manifest=manifest, candidate_id=identity['id'],
            launch=Launch('forge/issue-1', role + '[bot]', 'codex', 200, {}, {}))['job_id']
        report = {'candidate': identity['id'], 'outcome': 'pass', 'summary': 'Reviewed this candidate'}
        self.life.collect(self.command(), job_id, execution_fence='fence-' + role,
                          capture={'result': report, 'logs': '', 'missing': []})
        self.life.verify_cleanup(self.command(), job_id, execution_fence='fence-' + role,
                                 evidence={'process_stopped': True, 'resources_released': True})
        return ExecutionQueries(self.store).job(job_id), report

    def merge_ready(self):
        for role in ('review', 'audit'):
            job, report = self.captured(role)
            receipt = self.driver.ensure(job, report)
            self.life.finish_job(self.command(), job['id'], execution_fence=job['execution_fence'],
                input_manifest=job['input_manifest'], evidence={'candidate_id': self.candidate_id,
                                                              'verdict': 'pass', **receipt})

    def queue_merge(self, command=None, authorization=None):
        return self.effects.queue_merge(command or self.command(), self.candidate_id, policy_hash='policy',
                       authorization=authorization or self.authorization())['effect_id']

    def test_verdict_replays_durable_intent_after_lost_response_without_duplicate_check(self):
        job, report = self.captured()
        self.remote.lose_verdict = True
        with self.assertRaises(GitHubUnavailable):
            self.driver.ensure(job, report)
        effect = self.queries.verdict(job['id'])
        self.assertEqual(effect['status'], 'uncertain')
        self.now += 30
        restarted = EffectController(self.store, self.remote, clock=lambda: self.now)
        self.assertEqual(restarted.ensure(job, report), {'check_id': 10})
        self.assertEqual(restarted.ensure(job, report), {'check_id': 10})
        self.assertEqual(len(self.remote.checks), 1)
        self.assertEqual(self.remote.verdict_calls, 2)
        self.assertEqual(self.queries.get(effect['id'])['attempts'], 2)
        with self.assertRaises(IdempotencyConflict):
            restarted.ensure(job, {**report, 'summary': 'Different finding'})

    def test_verdict_rejects_unrelated_candidate_and_revoked_capture_without_publication(self):
        job, report = self.captured()
        with self.assertRaises(TransitionError):
            self.driver.ensure(job, {**report, 'candidate': 'other'})
        self.life.pause(self.command())
        with self.assertRaises(TransitionError):
            self.driver.ensure(job, report)
        self.assertEqual(self.remote.verdict_calls, 0)

    def test_merge_intent_replay_binds_payload_and_expected_version(self):
        self.merge_ready()
        command = self.command()
        effect_id = self.queue_merge(command)
        self.assertEqual(self.queue_merge(command), effect_id)
        with self.assertRaises(IdempotencyConflict):
            self.queue_merge(command, {**self.authorization(), 'approved': False})
        with self.assertRaises(VersionConflict):
            self.queue_merge(Command('stale', 'operator', 'u', command.expected_version))

    def test_merge_reconciles_lost_response_before_retry_or_local_revocation(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        self.remote.lose_merge = True
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(effect_id)
        self.assertEqual(self.view()['state'], 'merging')
        self.assertEqual(self.queries.get(effect_id)['status'], 'uncertain')
        self.life.pause(self.command())
        self.driver.admission = None
        self.now += 30
        result = self.driver.step(effect_id)
        self.assertEqual(result['status'], 'confirmed')
        self.assertEqual(self.view()['state'], 'merged')
        self.assertEqual(self.remote.merge_calls, 1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT status FROM merge_requests').fetchone()[0], 'merged')
            self.assertEqual(db.execute("SELECT count(*) FROM candidate_evidence WHERE kind='merge'").fetchone()[0], 1)

    def test_successful_http_response_does_not_mark_merged(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        self.remote.merge_visible = False
        self.driver.step(effect_id)
        self.assertEqual(self.view()['state'], 'merging')
        self.assertEqual(self.queries.get(effect_id)['status'], 'uncertain')
        with self.assertRaises(TransitionError):
            self.effects.record(effect_id, 'confirmed', receipt={'merged': True})
        self.life.pause(self.command())
        self.driver.step(effect_id)
        self.assertEqual(self.remote.merge_calls, 1)
        self.assertEqual(self.queries.get(effect_id)['status'], 'uncertain')

    def test_definitive_merge_refusal_releases_intent_but_server_failure_does_not(self):
        self.merge_ready()
        first = self.queue_merge()
        self.remote.merge = Mock(side_effect=GitHubUnavailable('Head changed', status=409))
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(first)
        self.assertEqual(self.queries.get(first)['status'], 'failed')
        self.assertEqual(self.queries.get(first)['merge_status'], 'refused')
        second = self.queue_merge()
        self.remote.merge = Mock(side_effect=GitHubUnavailable('Proxy failed', status=502))
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(second)
        self.assertEqual(self.queries.get(second)['status'], 'uncertain')
        self.assertEqual(self.queries.get(second)['merge_status'], 'uncertain')
        with self.assertRaises(TransitionError):
            self.effects.refuse_merge(second, http_status=502, evidence={})

    def test_later_refusal_cannot_erase_an_earlier_ambiguous_put(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        self.remote.merge = Mock(side_effect=GitHubUnavailable('Response lost'))
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(effect_id)
        self.now += 30
        self.remote.merge = Mock(side_effect=GitHubUnavailable('Rules refused this retry', status=405))
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(effect_id)
        effect = self.queries.get(effect_id)
        self.assertEqual((effect['status'], effect['merge_status'], effect['merge_send_count']), ('uncertain', 'uncertain', 2))

    def test_refusal_requires_successful_fresh_pr_recheck(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        def refuse(item):
            self.remote.pr = Mock(side_effect=GitHubUnavailable('Cannot observe current PR'))
            raise GitHubUnavailable('Refused', status=405)
        self.remote.merge = refuse
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(effect_id)
        self.assertEqual(self.queries.get(effect_id)['status'], 'uncertain')

    def test_refused_sha_race_can_release_first_intent_after_remote_recheck(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        def refuse(item):
            self.remote.pull['head']['sha'] = 'new-head'
            raise GitHubUnavailable('Expected SHA changed', status=409)
        self.remote.merge = refuse
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(effect_id)
        self.assertEqual(self.queries.get(effect_id)['status'], 'failed')
        self.assertEqual(self.queries.get(effect_id)['receipt']['evidence']['observed_head'], 'new-head')

    def test_merge_gate_rechecks_before_put_after_check_publication(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        self.remote.on_check = lambda: self.life.pause(self.command())
        self.driver.step(effect_id)
        self.assertEqual(self.remote.merge_calls, 0)
        self.assertEqual(self.queries.get(effect_id)['status'], 'superseded')

    def test_changed_head_never_merges_and_pending_intent_can_be_superseded(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        self.remote.pull['head']['sha'] = 'changed'
        result = self.driver.step(effect_id)
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(self.remote.merge_calls, 0)

    def test_changed_base_never_merges(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        self.remote.pull['base']['sha'] = 'changed'
        self.assertEqual(self.driver.step(effect_id)['status'], 'superseded')
        self.assertEqual(self.remote.merge_calls, 0)

    def test_changed_policy_missing_approval_and_stale_observation_are_refused(self):
        self.merge_ready()
        for change in ({'policy_hash': 'new'}, {'approved': False}, {'observed_at': 1}, {'checks_passed': False}):
            with self.subTest(change=change), self.assertRaises(TransitionError):
                self.queue_merge(authorization={**self.authorization(), **change})
        self.assertEqual(self.queries.pending(), [])

    def test_changed_requirements_and_hold_block_merge_admission(self):
        self.merge_ready()
        self.life.open_gate(self.command(), 'label_hold', details={'source': 'github_labels'}, member_id=self.member)
        with self.assertRaises(TransitionError):
            self.queue_merge()

    def test_changed_issue_requirements_block_merge_even_without_stored_gate(self):
        self.merge_ready()
        with self.store.write() as db:
            db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) "
                       "VALUES('revised','i','Issue','Changed','new-content','new-requirements','{}',100)")
            db.execute("UPDATE issues SET current_revision_id='revised',version=version+1 WHERE id='i'")
        with self.assertRaises(TransitionError):
            self.queue_merge()

    def test_repository_identity_and_draft_are_checked_before_verdict_publication(self):
        job, report = self.captured()
        self.remote.pull['head']['repo']['id'] = 2
        with self.assertRaises(TransitionError):
            self.driver.ensure(job, report)
        self.assertEqual(self.remote.verdict_calls, 0)

    def test_harmless_version_change_can_reconcile_same_verdict_identity(self):
        job, report = self.captured()
        self.remote.lose_verdict = True
        with self.assertRaises(GitHubUnavailable):
            self.driver.ensure(job, report)
        # An unrelated observation may bump the version without revoking the
        # assignment, so delivery revalidates current semantic authority.
        with self.store.write() as db:
            db.execute("UPDATE units SET version=version+1 WHERE id='u'")
        self.now += 30
        self.assertEqual(self.driver.ensure(job, report), {'check_id': 10})
        self.assertEqual(len(self.remote.checks), 1)

    def test_version_change_during_remote_admission_cannot_send_put(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        def race(effect):
            with self.store.write() as db:
                db.execute("UPDATE units SET version=version+1 WHERE id='u'")
            return self.authorization()
        self.driver.admission = race
        self.assertEqual(self.driver.step(effect_id)['status'], 'superseded')
        self.assertEqual(self.remote.merge_calls, 0)

    def test_pending_version_change_prevents_any_merge_request(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        self.life.pause(self.command())
        self.assertEqual(self.driver.step(effect_id)['status'], 'superseded')
        self.assertEqual(self.remote.merge_calls, 0)

    def test_default_controller_cannot_merge(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        driver = EffectController(self.store, self.remote, clock=lambda: self.now)
        self.assertEqual(driver.step(effect_id)['status'], 'superseded')
        self.assertEqual(self.remote.merge_calls, 0)

    def test_incomplete_observation_keeps_intent_and_import_cannot_execute(self):
        self.merge_ready()
        effect_id = self.queue_merge()
        self.remote.pull['mergeable'] = None
        self.assertEqual(self.driver.step(effect_id)['status'], 'pending')
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata VALUES('import_only','true')")
        with self.assertRaises(TransitionError):
            self.driver.step(effect_id)
        self.assertEqual(self.remote.merge_calls, 0)

    def test_cancellation_check_has_durable_stable_identity(self):
        effect_id = self.effects.queue_check(self.command(), self.candidate_id, role='review',
                                             conclusion='cancelled', summary='Stopped')['effect_id']
        self.driver.step(effect_id)
        self.driver.step(effect_id)
        self.assertEqual(self.remote.check_calls, 1)
        self.assertIn(effect_id, self.remote.checks)
        self.assertEqual(self.queries.get(effect_id)['status'], 'confirmed')

    def test_settlement_recovers_lost_patch_and_verifies_original_checks(self):
        job, _ = self.captured()
        runs = [dict(id=1, name='forge/review', app={'id': 10}, external_id=job['id'], head_sha='head', status='in_progress'),
                dict(id=2, name='forge/review', app={'id': 10}, external_id='replacement-job', head_sha='head', status='in_progress')]
        self.remote.connection = Mock(return_value={'app_id': 10})
        self.remote.pages = Mock(side_effect=lambda *args: [dict(run) for run in runs])
        patches = []
        def patch_check(role, item, method, path, payload):
            self.remote.outside_transaction()
            patches.append(path)
            runs[0].update(status='completed', conclusion='cancelled')
            raise GitHubUnavailable('Patch response lost')
        self.remote.call = patch_check
        effect_id = self.effects.queue_settlement(self.command(), self.candidate_id, summary='Stopped')['effect_id']
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(effect_id)
        self.now += 30
        self.assertEqual(self.driver.step(effect_id)['receipt'], {'check_ids': [1]})
        self.assertEqual(patches, ['/check-runs/1'])
        self.assertEqual(runs[1]['status'], 'in_progress')

    def test_settlement_does_not_claim_confirmation_from_patch_response(self):
        job, _ = self.captured()
        run = dict(id=1, name='forge/review', app={'id': 10}, external_id=job['id'], head_sha='head', status='in_progress')
        self.remote.connection = Mock(return_value={'app_id': 10})
        self.remote.pages = Mock(return_value=[run])
        self.remote.call = Mock(return_value={'id': 1, 'status': 'completed'})
        effect_id = self.effects.queue_settlement(self.command(), self.candidate_id, summary='Stopped')['effect_id']
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(effect_id)
        self.assertEqual(self.queries.get(effect_id)['status'], 'uncertain')

    def test_effect_events_correlate_intent_delivery_and_verified_result(self):
        job, report = self.captured()
        self.driver.ensure(job, report)
        effect = self.queries.verdict(job['id'])
        with self.store.read_snapshot() as db:
            rows = db.execute("SELECT * FROM events WHERE kind='external_effect_changed' ORDER BY sequence").fetchall()
        self.assertEqual([json.loads(row['payload_json'])['status'] for row in rows], ['pending', 'running', 'confirmed'])
        self.assertTrue(all(row['transaction_id'] == effect['request_id'] for row in rows))
        self.assertTrue(all(json.loads(row['payload_json'])['effect_id'] == effect['id'] for row in rows))

    def test_unavailable_old_effect_does_not_starve_next_page_across_restart(self):
        first = self.effects.queue_check(self.command(), self.candidate_id, role='review',
                                         conclusion='cancelled', summary='First')['effect_id']
        self.now += 1
        second = self.effects.queue_check(self.command(), self.candidate_id, role='audit',
                                          conclusion='cancelled', summary='Second')['effect_id']
        original = self.remote.pr
        def observe(effect, number):
            if effect['id'] == first:
                raise GitHubUnavailable('temporarily unavailable')
            return original(effect, number)
        self.remote.pr = observe
        self.assertEqual(self.driver.cycle(limit=1)[0]['id'], first)
        self.now += 30
        restarted = EffectController(self.store, self.remote, clock=lambda: self.now)
        self.assertEqual(restarted.cycle(limit=1)[0]['id'], second)
        self.assertEqual(self.queries.get(second)['status'], 'confirmed')

    def test_retry_schedule_survives_restart_and_direct_claim_cannot_bypass_it(self):
        self.merge_ready()
        effect_id=self.queue_merge()
        self.remote.merge_visible=False
        self.driver.step(effect_id)
        effect=self.queries.get(effect_id)
        self.assertEqual(effect['attempts'],1)
        self.assertEqual(effect['retry_at'],self.now+30)
        self.assertNotIn(effect_id,EffectQueries(self.store,clock=lambda:self.now).pending())
        restarted=EffectController(self.store,self.remote,admission=lambda e:self.authorization(),clock=lambda:self.now)
        restarted.step(effect_id)
        with self.assertRaises(TransitionError):
            self.effects.claim(effect_id,expected_version=effect['unit_version'],authorization=self.authorization(),merge_request=True)
        self.assertEqual(self.remote.merge_calls,1)
        self.now+=30
        restarted.step(effect_id)
        self.assertEqual(self.remote.merge_calls,2)
        self.assertEqual(self.queries.get(effect_id)['retry_at'],self.now+60)

    def test_two_uncertain_sends_reconcile_fresh_closed_unmerged_and_preserve_pause(self):
        self.merge_ready()
        effect_id=self.queue_merge()
        self.remote.merge_visible=False
        self.driver.step(effect_id)
        self.now+=30
        self.driver.step(effect_id)
        self.assertEqual(self.queries.get(effect_id)['merge_send_count'],2)
        self.life.pause(self.command())
        self.now+=60
        self.remote.pull.update(state='closed',merged=False)
        result=self.driver.step(effect_id)
        self.assertEqual((result['status'],result['merge_status']),('failed','refused'))
        self.assertEqual(self.view()['state'],'building')
        self.assertEqual(self.view()['reason_code'],'operator_pause')
        self.assertEqual(self.remote.merge_calls,2)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_two_uncertain_sends_changed_head_rebuilds_and_rejects_stale_observation(self):
        self.merge_ready()
        effect_id=self.queue_merge()
        self.remote.merge_visible=False
        self.driver.step(effect_id)
        self.now+=30
        self.driver.step(effect_id)
        with self.assertRaises(TransitionError):
            self.effects.observe_unmerged(effect_id,evidence=dict(verified=True,merged=False,candidate_id=self.candidate_id,
                pull_number=1,head_oid='head',observed_head='different',repository_id=1,base_branch='main',state='open',observed_at=100))
        self.now+=60
        self.remote.pull['head']['sha']='new-head'
        self.driver.step(effect_id)
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.queries.get(effect_id)['status'],'failed')

    def test_unknown_mergeability_escalates_without_becoming_conflict_or_charging(self):
        self.merge_ready()
        effect_id=self.queue_merge()
        self.remote.pull['mergeable']=None
        self.driver.step(effect_id)
        self.now+=900
        self.driver.step(effect_id)
        self.assertEqual(self.remote.merge_calls,0)
        self.assertEqual(self.view()['state'],'merging')
        self.assertEqual(self.view()['reason_code'],'forge_problem')
        self.assertEqual(self.queries.get(effect_id)['merge_status'],'pending')
        self.assertIsNotNone(self.queries.get(effect_id)['escalation_at'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)

    def test_unsent_unknown_merge_wait_resumes_from_fresh_mergeable_observation(self):
        self.merge_ready()
        effect_id=self.queue_merge()
        self.remote.pull['mergeable']=None
        self.driver.step(effect_id)
        self.now+=900
        self.driver.step(effect_id)
        self.assertEqual(self.view()['reason_code'],'forge_problem')
        self.now+=900
        self.remote.pull['mergeable']=True
        self.driver.step(effect_id)
        self.assertEqual(self.view()['state'],'merged')
        self.assertEqual(self.remote.merge_calls,1)

    def test_sustained_observed_conflict_rebuilds_without_charge(self):
        self.merge_ready()
        effect_id=self.queue_merge()
        self.remote.pull['mergeable']=False
        self.driver.step(effect_id)
        self.now+=900
        self.driver.step(effect_id)
        self.assertEqual(self.view()['state'],'building')
        self.assertEqual(self.queries.get(effect_id)['status'],'superseded')
        self.assertEqual(self.remote.merge_calls,0)

    def test_send_cap_keeps_fence_and_readback_can_finish_later(self):
        self.merge_ready()
        effect_id=self.queue_merge()
        self.remote.merge_visible=False
        driver=EffectController(self.store,self.remote,admission=lambda e:self.authorization(),clock=lambda:self.now,
            reliability={'effect_max_attempts':2})
        driver.step(effect_id)
        self.now+=30
        driver.step(effect_id)
        self.now+=60
        driver.step(effect_id)
        self.assertEqual(self.remote.merge_calls,2)
        self.assertEqual(self.view()['reason_code'],'forge_problem')
        self.assertEqual(self.queries.get(effect_id)['merge_status'],'uncertain')
        self.now+=900
        self.remote.pull.update(merged=True,state='closed',merge_commit_sha='confirmed-merge')
        driver.step(effect_id)
        self.assertEqual(self.view()['state'],'merged')
        self.assertEqual(self.remote.merge_calls,2)

    def test_budget_deferred_read_does_not_consume_a_send_attempt(self):
        from forge.request_metrics import RequestBudgetExhausted
        self.merge_ready()
        effect_id=self.queue_merge()
        self.remote.pr=Mock(side_effect=RequestBudgetExhausted())
        with self.assertRaises(RequestBudgetExhausted):
            self.driver.step(effect_id)
        effect=self.queries.get(effect_id)
        self.assertEqual((effect['attempts'],effect['merge_send_count']), (0,0))
        self.assertIsNone(effect['uncertain_since'])


if __name__ == '__main__':
    unittest.main()
