"""Exact, durable review-only PR admission and ordinary lifecycle authority."""
import copy
import tempfile
import unittest
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.merge_checks import ReviewPullsController
from forge.store.db import Store
from forge.store.models import Command, TransitionError
from forge.store.review_pulls import ReviewPullRequests, normalize_source, source_in, snapshot_in, exact_source_matches
from forge.store.transitions import Lifecycle, manifest_hash
from forge.store.task_inputs import task_execution_gates_in


class ReviewPullTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now = 1000
        self.commands = ReviewPullRequests(self.store, clock=lambda: self.now)
        self.life = Lifecycle(self.store, clock=lambda: self.now)
        self.config = validate({'enabled': True, 'image': 'example/forge@sha256:' + 'a'*64})
        self.github = Mock()
        self.controller = ReviewPullsController(self.store, self.github, self.config, clock=lambda: self.now)
        self.item = dict(repo_id=1, repo='owner/repo')
        with self.store.write() as db:
            db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'owner/repo')")
        self.remote = dict(number=7, title='Human change', body='Expected behavior', state='open', draft=False,
            merged=False, user={'id': 44, 'login': 'alice', 'type': 'User'}, labels=[],
            head={'sha': 'a'*40, 'ref': 'feature', 'repo': {'id': 2, 'full_name': 'alice/fork'}},
            base={'sha': 'b'*40, 'ref': 'main', 'repo': {'id': 1, 'full_name': 'owner/repo'}})
        self.github.pr.side_effect = lambda item, number: copy.deepcopy(self.remote)

    def observe(self, **options):
        return self.commands.observe(normalize_source(self.remote, **self.item), policy_hash='policy', **options)

    def snapshot(self, unit_id):
        with self.store.read_snapshot() as db:
            unit = dict(db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone())
            return snapshot_in(db, unit)

    def clear_policy_wait(self, admitted):
        # Stand in for the ordinary effect controller's exact confirmed readback.
        with self.store.write() as db:
            gate = db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL AND "
                "json_extract(details_json,'$.source')='review_pull_policy'", (admitted['unit_id'],)).fetchone()
            if gate:
                snapshot = self.snapshot(admitted['unit_id'])
                command = Command('confirm-'+gate[0], 'supervisor:effects', admitted['unit_id'], snapshot['unit']['version'])
                self.life._resolve(db, command, gate[0], {'confirmed': True})

    def reserve(self, admitted):
        self.clear_policy_wait(admitted)
        snapshot = self.snapshot(admitted['unit_id']); candidate = snapshot['candidate']
        manifest = {key: candidate[key] for key in ('head_oid','base_oid','policy_hash','requirements_hash','input_manifest_hash')}
        manifest['candidate_id'] = candidate['id']
        command = Command('reserve-'+candidate['id'], 'supervisor:dispatch', admitted['unit_id'], snapshot['unit']['version'])
        result = self.life.reserve_job(command,'review',slot='review-1',execution_fence='fence', input_manifest=manifest,
                                       candidate_id=candidate['id'])
        return result, manifest

    def test_admission_is_memberless_and_initial_check_is_a_durable_effect(self):
        admitted = self.observe(); snapshot = self.snapshot(admitted['unit_id'])
        self.assertEqual((snapshot['role'],snapshot['members'],snapshot['reason']),('review',[],'observation_unavailable'))
        self.assertEqual(snapshot['review_source']['head']['repo']['id'], 2)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM issues').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM unit_issues').fetchone()[0],0)
            effect = db.execute('SELECT * FROM external_effects').fetchone()
            self.assertEqual((effect['kind'],effect['status'],effect['attempts']),('github_check','pending',0))
        again = self.observe()
        self.assertEqual(again['candidate_id'], admitted['candidate_id'])
        self.assertFalse(again['changed'])

    def test_head_change_fences_job_retains_history_and_cannot_accept_old_result(self):
        old = self.observe(); job, manifest = self.reserve(old)
        self.remote['head']['sha'] = 'c'*40
        fresh = self.observe()
        self.assertNotEqual(old['candidate_id'],fresh['candidate_id'])
        with self.store.read_snapshot() as db:
            unit = dict(db.execute('SELECT * FROM units WHERE id=?',(old['unit_id'],)).fetchone())
            _, current = self.life._job(db,unit,job['job_id'],'fence',manifest)
            self.assertFalse(current)
            self.assertEqual(db.execute('SELECT count(*) FROM candidates').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0],2)
            self.assertEqual(source_in(db,old['candidate_id'])['head']['sha'],'a'*40)
        self.assertEqual(self.snapshot(old['unit_id'])['unit']['state'],'reviewing')

    def test_same_head_source_change_replaces_policy_check_and_preserves_pause(self):
        old = self.observe()
        snapshot=self.snapshot(old['unit_id'])
        self.life.pause(Command('pause','operator',old['unit_id'],snapshot['unit']['version']))
        self.remote['body']='Changed intent'
        fresh=self.observe()
        self.assertNotEqual(old['candidate_id'],fresh['candidate_id'])
        self.assertEqual(self.snapshot(old['unit_id'])['reason'],'operator_pause')
        with self.store.read_snapshot() as db:
            effects=db.execute('SELECT id FROM external_effects ORDER BY id').fetchall()
            self.assertEqual(len(effects),2)
            self.assertNotEqual(effects[0][0],effects[1][0])

    def test_rejection_waits_for_human_changes_without_recovery_or_builder(self):
        admitted=self.observe(); job,manifest=self.reserve(admitted)
        snap=self.snapshot(admitted['unit_id'])
        result=self.life.reject_job(Command('reject','results',admitted['unit_id'],snap['unit']['version']),job['job_id'],
            execution_fence='fence',input_manifest=manifest,evidence={'verdict':'fail','candidate_id':admitted['candidate_id']})
        self.assertFalse(result['charged'])
        again=self.observe(); snap=self.snapshot(admitted['unit_id'])
        self.assertEqual(again['candidate_id'],admitted['candidate_id'])
        self.assertEqual(snap['reason'],'review_source')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempt_failures').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],0)
            self.assertEqual(db.execute("SELECT count(*) FROM external_effects WHERE json_extract(payload_json,'$.conclusion')='failure'").fetchone()[0],1)
        with self.assertRaises(TransitionError):
            self.life.reserve_job(Command('build','dispatch',admitted['unit_id'],snap['unit']['version']),
                'build',slot='build-1',execution_fence='bad',input_manifest={'source':'human'})

    def test_memberless_jobs_obey_unit_pause_gates(self):
        admitted=self.observe(); job,_=self.reserve(admitted)
        snap=self.snapshot(admitted['unit_id'])
        self.life.pause(Command('pause','operator',admitted['unit_id'],snap['unit']['version']))
        with self.store.read_snapshot() as db:
            unit=dict(db.execute('SELECT * FROM units WHERE id=?',(admitted['unit_id'],)).fetchone())
            record=dict(db.execute('SELECT * FROM jobs WHERE id=?',(job['job_id'],)).fetchone())
            self.assertIn('operator_pause',[gate['code'] for gate in task_execution_gates_in(db,unit,record)])

    def test_forge_scope_excludes_new_human_pr_and_fences_existing(self):
        self.assertFalse(self.observe(scope='forge')['admitted'])
        admitted=self.observe(); job,_=self.reserve(admitted)
        self.assertEqual(self.observe(scope='forge')['reason'],'scope_excluded')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT fenced FROM jobs WHERE id=?',(job['job_id'],)).fetchone()[0],1)
        self.assertEqual(self.observe()['candidate_id'],admitted['candidate_id'])

    def test_source_match_checks_author_base_head_fork_and_intent(self):
        source=normalize_source(self.remote,**self.item)
        self.assertTrue(exact_source_matches(self.remote,source))
        for field in ('author','base','head','body'):
            changed=copy.deepcopy(self.remote)
            if field=='author': changed['user']['id']=45
            elif field in {'base','head'}: changed[field]['repo']['id']=99
            else: changed['body']='changed'
            self.assertFalse(exact_source_matches(changed,source))

    def test_controller_uses_fresh_pr_and_never_publishes_automatic_success(self):
        listing={**self.remote,'forge_owned':False,'body':'old listing'}
        admitted,=self.controller.reconcile(self.item,[listing])
        self.assertTrue(admitted['admitted'])
        self.github.pr.assert_called_once_with(self.item,7)
        self.github.call.assert_not_called()
        self.assertEqual(self.controller.reconcile(self.item,[listing]),[])
        self.remote['head']['sha']='c'*40
        changed,=self.controller.cycle()['items']
        self.assertTrue(changed['changed'])

    def test_closed_pr_retires_then_reopen_is_a_new_unit(self):
        old = self.observe()
        self.remote['state'] = 'closed'
        retired = self.observe()
        self.assertEqual(retired['reason'], 'pull_request_closed')
        self.assertEqual(self.snapshot(old['unit_id'])['unit']['state'], 'closed')
        self.remote['state'] = 'open'
        fresh, = self.controller.reconcile(self.item, [{**self.remote, 'forge_owned': False}])
        self.assertNotEqual(old['unit_id'], fresh['unit_id'])

    def test_retarget_uses_new_unit_and_carries_operator_pause(self):
        old = self.observe(); snap = self.snapshot(old['unit_id'])
        self.life.pause(Command('pause','operator',old['unit_id'],snap['unit']['version']))
        self.remote['base']['ref'] = 'release'
        fresh = self.observe()
        self.assertNotEqual(old['unit_id'], fresh['unit_id'])
        self.assertEqual(self.snapshot(old['unit_id'])['unit']['close_code'], 'pull_retargeted')
        successor = self.snapshot(fresh['unit_id'])
        self.assertEqual(successor['unit']['base_branch'], 'release')
        self.assertEqual(successor['reason'], 'operator_pause')

    def test_observation_race_cannot_restore_an_earlier_source(self):
        old = self.observe(); version = self.snapshot(old['unit_id'])['unit']['version']
        stale = normalize_source(self.remote, **self.item)
        self.remote['head']['sha'] = 'c'*40
        fresh = self.observe()
        ignored = self.commands.observe(stale, policy_hash='policy', expected_version=version)
        self.assertEqual(ignored['reason'], 'observation_changed')
        self.assertEqual(self.snapshot(old['unit_id'])['candidate']['id'], fresh['candidate_id'])

    def test_draft_holds_existing_unit_without_creating_a_new_one(self):
        old = self.observe()
        self.remote['draft'] = True
        self.assertEqual(self.observe()['reason'], 'pull_draft')
        self.remote['draft'] = False
        resumed = self.observe()
        self.assertEqual(old['unit_id'], resumed['unit_id'])

    def test_pause_before_initial_check_creates_fresh_intent_without_clearing_pause(self):
        old = self.observe(); snap = self.snapshot(old['unit_id'])
        self.life.pause(Command('pause','operator',old['unit_id'],snap['unit']['version']))
        refreshed = self.observe()
        self.assertEqual(refreshed['candidate_id'], old['candidate_id'])
        self.assertEqual(self.snapshot(old['unit_id'])['reason'], 'operator_pause')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0], 2)
            unit = db.execute('SELECT * FROM units WHERE id=?', (old['unit_id'],)).fetchone()
            generation = db.execute("SELECT c.fencing_generation FROM unit_gates g JOIN candidate_effects c "
                "ON c.effect_id=json_extract(g.details_json,'$.effect_id') WHERE g.unit_id=? AND g.resolved_at IS NULL "
                "AND json_extract(g.details_json,'$.source')='review_pull_policy'", (old['unit_id'],)).fetchone()[0]
            self.assertEqual(generation, unit['fencing_generation'])
        self.observe()
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM external_effects').fetchone()[0], 2)

    def test_new_revision_preserves_independent_external_change_gate(self):
        old = self.observe(); snap = self.snapshot(old['unit_id'])
        independent = self.life.open_gate(Command('external','observer',old['unit_id'],snap['unit']['version']),
            'external_change', details={'source': 'independent_observation'})
        self.remote['draft'] = True
        self.observe()
        self.remote['draft'] = False
        self.remote['head']['sha'] = 'c'*40
        self.observe()
        with self.store.read_snapshot() as db:
            self.assertIsNone(db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',
                                         (independent['gate_id'],)).fetchone()[0])
            self.assertEqual(db.execute("SELECT count(*) FROM unit_gates WHERE unit_id=? AND code='review_source' "
                'AND resolved_at IS NULL', (old['unit_id'],)).fetchone()[0], 0)

    def test_pause_after_rejection_repairs_only_fenced_failure_intent(self):
        admitted = self.observe(); job, manifest = self.reserve(admitted)
        snap = self.snapshot(admitted['unit_id'])
        self.life.reject_job(Command('reject','results',admitted['unit_id'],snap['unit']['version']),job['job_id'],
            execution_fence='fence',input_manifest=manifest,evidence={'verdict':'fail','candidate_id':admitted['candidate_id']})
        snap = self.snapshot(admitted['unit_id'])
        self.life.pause(Command('pause','operator',admitted['unit_id'],snap['unit']['version']))
        self.observe()
        self.assertEqual(self.snapshot(admitted['unit_id'])['reason'], 'operator_pause')
        with self.store.read_snapshot() as db:
            count = db.execute("SELECT count(*) FROM external_effects WHERE json_extract(payload_json,'$.conclusion')='failure'").fetchone()[0]
            self.assertEqual(count, 2)
        self.observe()
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM external_effects WHERE json_extract(payload_json,'$.conclusion')='failure'").fetchone()[0],count)

    def test_controller_excludes_grouped_forge_pr(self):
        listing={**self.remote,'forge_owned':True}
        self.assertEqual(self.controller.reconcile(self.item,[listing]),[])
        self.github.pr.assert_not_called()


if __name__=='__main__':
    unittest.main()
