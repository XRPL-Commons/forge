"""Durable progress updates share a candidate check and retire only owned runs."""
import copy
import json
import unittest
from unittest.mock import Mock

from forge.factory.github import GitHub, GitHubUnavailable
from forge.factory.merge_checks import ReviewPullsController
from forge.store.effects import EffectQueries
from forge.store.review_pulls import normalize_source
from forge.store.review_progress import details_url
from forge.store.transitions import Lifecycle
from tests import test_review_policy_effects as helpers
from tests.test_settings import configured


class ProgressTests(unittest.TestCase):
    setUpBase = helpers.ReviewPolicyTests.setUp
    admit = helpers.ReviewPolicyTests.admit
    command = helpers.ReviewPolicyTests.command
    check = helpers.ReviewPolicyTests.check
    policy_current = helpers.ReviewPolicyTests.policy_current

    def setUp(self):
        self.setUpBase()
        settings = Mock(); settings.load.return_value = configured()
        self.remote = GitHub(settings, self.config)
        self.app = self.remote.connection('controller')['app_id']
        self.runs = []; self.writes = []
        self.remote.pages = Mock(side_effect=lambda *args: copy.deepcopy(self.runs))
        self.remote.work_details_url = lambda item, selected: details_url('https://anvil.example', item['repo_id'], selected['pr'])
        def call(role, item, method, path, payload):
            self.writes.append((method, path, copy.deepcopy(payload)))
            if method == 'POST':
                self.runs.append({**copy.deepcopy(payload), 'id': 100 + len(self.runs), 'app': {'id': self.app}, 'conclusion': payload.get('conclusion')})
            else:
                run = next(r for r in self.runs if str(r['id']) == path.rsplit('/', 1)[1])
                run.update(copy.deepcopy(payload))
        self.remote.call = Mock(side_effect=call)
        self.github.check.side_effect = self.remote.check
        self.github.connection.side_effect = self.remote.connection
        self.github.app_ids.side_effect = self.remote.app_ids
        self.github.pages.side_effect = self.remote.pages
        self.github.call.side_effect = self.remote.call
        self.github.work_details_url.side_effect = self.remote.work_details_url

    def gate(self, row, **details):
        return Lifecycle(self.store, clock=lambda: self.now).open_gate(self.command(row['unit_id']), 'review_source',
            details={'source': 'review_pull', 'reason': 'review_unavailable', 'candidate_id': row['candidate_id'], **details})

    def latest(self, row, kind='github_check'):
        with self.store.read_snapshot() as db:
            return db.execute('SELECT e.id FROM external_effects e JOIN candidate_effects c ON c.effect_id=e.id '
                'WHERE c.candidate_id=? AND e.kind=? ORDER BY e.rowid DESC', (row['candidate_id'], kind)).fetchone()[0]

    def test_retry_updates_same_check_and_duplicate_refresh_does_not_publish(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        self.gate(row, cause='dependency_access', retry_at=self.now + 300, attempt=1, summary='secret provider prose')
        update = self.observer.refresh_progress(row['unit_id'])
        self.driver.step(update['effect_id'])
        self.assertEqual(len(self.runs), 1)
        run = self.runs[0]
        self.assertEqual(run['external_id'], row['effect_id'])
        self.assertEqual(run['details_url'], 'https://anvil.example/pull-requests/1/7')
        self.assertIn('Dependency downloads are unavailable', run['output']['summary'])
        self.assertIn('1970-01-01T00:21:40Z', run['output']['summary'])
        self.assertNotIn('secret', run['output']['summary'])
        self.assertFalse(self.observer.refresh_progress(row['unit_id'])['changed'])
        before = len(self.writes); self.driver.step(update['effect_id'])
        self.assertEqual(len(self.writes), before)
        self.now += 301
        self.observer.observe(normalize_source(self.remotes[7], 1, 'owner/repo'), policy_hash=self.policy)
        queued = self.observer.refresh_progress(row['unit_id'])
        self.driver.step(queued['effect_id'])
        self.assertEqual(len(self.runs), 1)
        self.assertIn('queued', run['output']['summary'])

    def test_running_and_deferred_state_publish_without_duplicate_runs(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        with self.store.read_snapshot() as db:
            candidate = dict(db.execute('SELECT * FROM candidates WHERE id=?', (row['candidate_id'],)).fetchone())
        manifest = {key: candidate[key] for key in ('head_oid','base_oid','policy_hash','requirements_hash','input_manifest_hash')}
        manifest['candidate_id'] = candidate['id']
        life = Lifecycle(self.store, clock=lambda:self.now)
        job = life.reserve_job(self.command(row['unit_id']), 'review', slot='review-1', execution_fence='fence',
                               input_manifest=manifest, candidate_id=row['candidate_id'])
        with self.store.write() as db:
            db.execute("UPDATE jobs SET phase='running' WHERE id=?", (job['job_id'],))
        running = self.observer.refresh_progress(row['unit_id'])
        self.driver.step(running['effect_id'])
        self.assertIn('Review is running', self.runs[0]['output']['summary'])
        result = self.observer.defer_review(self.command(row['unit_id']), job['job_id'], execution_fence='fence',
            input_manifest=manifest, evidence={'safe': True}, reason='dependency_access')
        self.assertEqual(result['retry_at'], self.now + 300)
        self.driver.step(self.latest(row))
        self.assertEqual(len(self.runs), 1)
        self.assertIn('Next automatic retry', self.runs[0]['output']['summary'])

    def test_progress_superseded_before_send_and_terminal_never_reopened(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        self.gate(row, cause='untrusted prose with token', retry_at=None)
        update = self.observer.refresh_progress(row['unit_id'])
        self.assertNotIn('untrusted', EffectQueries(self.store).get(update['effect_id'])['payload']['summary'])
        self.remotes[7]['head']['sha'] = 'c' * 40
        fresh = self.observer.observe(normalize_source(self.remotes[7], 1, 'owner/repo'), policy_hash=self.policy)
        self.assertEqual(self.driver.step(update['effect_id'])['status'], 'superseded')
        self.assertNotEqual(fresh['candidate_id'], row['candidate_id'])
        self.runs[0].update(status='completed', conclusion='success')
        before = len(self.writes)
        self.remote.check({'repo_id': 1}, 'controller', {'head': 'a'*40, 'pr': 7}, row['effect_id'], None, 'late progress')
        self.assertEqual(len(self.writes), before)
        self.assertEqual(self.runs[0]['conclusion'], 'success')

    def test_lost_progress_response_reconciles_same_check_without_resend(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        self.gate(row, cause='dependency_access', retry_at=self.now + 300)
        effect = self.observer.refresh_progress(row['unit_id'])['effect_id']
        real_call = self.remote.call.side_effect
        def lost_response(*args):
            real_call(*args)
            raise GitHubUnavailable('Response lost')
        self.remote.call.side_effect = lost_response
        with self.assertRaises(GitHubUnavailable):
            self.driver.step(effect)
        self.remote.call.side_effect = real_call
        self.now += 31
        before = len(self.writes)
        self.assertEqual(self.driver.step(effect)['status'], 'confirmed')
        self.assertEqual(len(self.writes), before)
        self.assertEqual(len(self.runs), 1)

    def test_same_head_successor_is_not_cancelled_by_prior_settlement(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        self.remotes[7]['body'] = 'A changed scope on the same head'
        fresh = self.observer.observe(normalize_source(self.remotes[7], 1, 'owner/repo'), policy_hash=self.policy)
        self.driver.step(self.latest(fresh))
        self.assertEqual(len(self.runs), 2)
        self.driver.step(self.latest(row, 'github_settle_checks'))
        self.assertEqual(self.runs[0]['conclusion'], 'cancelled')
        self.assertEqual(self.runs[1]['status'], 'in_progress')
        self.assertNotEqual(self.runs[0]['external_id'], self.runs[1]['external_id'])

    def test_closed_pr_cancels_only_own_pending_run_and_retains_passed_review(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        self.runs += [dict(id=200, name='forge/review', head_sha='a'*40, app={'id': self.app}, external_id='unrelated',
                           status='completed', conclusion='success'),
                      dict(id=201, name='forge/merge-policy', head_sha='a'*40, app={'id': self.app}, external_id='another-candidate',
                           status='in_progress', conclusion=None)]
        self.remotes[7].update(state='closed', merged=True)
        self.observer.observe(normalize_source(self.remotes[7], 1, 'owner/repo'), policy_hash=self.policy)
        settlement = self.latest(row, 'github_settle_checks')
        self.assertEqual(self.driver.step(settlement)['status'], 'confirmed')
        self.assertEqual(self.runs[0]['conclusion'], 'cancelled')
        self.assertIn('merged outside Forge', self.runs[0]['output']['summary'])
        self.assertEqual(self.runs[1]['conclusion'], 'success')
        self.assertEqual(self.runs[2]['status'], 'in_progress')
        self.assertFalse(any(r['name'] == 'forge/audit' for r in self.runs))
        before = len(self.writes); self.driver.step(settlement)
        self.assertEqual(len(self.writes), before)
        with self.store.read_snapshot() as db:
            event = db.execute("SELECT payload_json FROM events WHERE unit_id=? AND kind='phase_changed' ORDER BY rowid DESC", (row['unit_id'],)).fetchone()[0]
        self.assertTrue(json.loads(event)['pull_merged'])

    def test_upgrade_repairs_preexisting_closed_unit_once(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        self.runs[0]['details_url'] = 'https://anvil.example/'
        with self.store.write() as db:
            db.execute("UPDATE units SET state='closed',close_code='pull_request_closed' WHERE id=?", (row['unit_id'],))
        self.remotes[7].update(state='closed', merged=True)
        controller = ReviewPullsController(self.store, self.github, self.config, clock=lambda:self.now)
        result = controller.cycle()
        self.assertEqual(len(result['items']), 1)
        self.driver.step(result['items'][0]['effect_id'])
        self.assertEqual(self.runs[0]['conclusion'], 'cancelled')
        self.assertEqual(self.runs[0]['details_url'], 'https://anvil.example/pull-requests/1/7')
        self.assertEqual(controller.cycle()['items'], [])

    def test_retired_app_run_is_never_patched_or_reopened(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        retired = self.app + 100
        self.runs[0]['app']['id'] = retired
        self.remote.app_ids = lambda role: [self.app, retired]
        self.github.app_ids.side_effect = self.remote.app_ids
        self.remotes[7].update(state='closed', merged=False)
        self.observer.observe(normalize_source(self.remotes[7], 1, 'owner/repo'), policy_hash=self.policy)
        before = len(self.writes)
        self.driver.step(self.latest(row, 'github_settle_checks'))
        self.assertEqual(len(self.writes), before)
        self.assertEqual(self.runs[0]['status'], 'in_progress')

    def test_wrong_head_or_duplicate_receipt_never_gets_patched(self):
        row = self.admit(); self.driver.step(row['effect_id'])
        self.runs[0]['head_sha'] = 'c'*40
        before = len(self.writes)
        with self.assertRaises(GitHubUnavailable):
            self.remote.check({'repo_id':1}, 'controller', {'head':'a'*40, 'pr':7}, row['effect_id'], None, 'update')
        self.runs[0]['head_sha'] = 'a'*40
        self.runs.append({**copy.deepcopy(self.runs[0]), 'id': 800})
        with self.assertRaises(GitHubUnavailable):
            self.remote.check({'repo_id':1}, 'controller', {'head':'a'*40, 'pr':7}, row['effect_id'], None, 'update')
        self.assertEqual(len(self.writes), before)

    def test_url_requires_an_actual_review_work_binding(self):
        self.remote.settings.database = self.store
        with self.store.write() as db:
            db.execute("INSERT INTO operator_auth_configuration VALUES(1,?,?,?,?,?,?,1)",
                       ('https://anvil.example', 'client', 'unused', 'unused', 'instance', 'epoch'))
        self.assertIsNone(GitHub.work_details_url(self.remote, {'repo_id':1}, {'pr':7}))
        self.admit()
        self.assertEqual(GitHub.work_details_url(self.remote, {'repo_id':1}, {'pr':7}),
                         'https://anvil.example/pull-requests/1/7')
        self.assertIsNone(GitHub.work_details_url(self.remote, {'repo_id':1}, {'pr':8}))

    def test_routes_accept_only_configured_https_origin_and_numeric_identity(self):
        self.assertEqual(details_url('https://anvil.example:8443', 1, 7), 'https://anvil.example:8443/pull-requests/1/7')
        for origin in ('http://anvil.example', 'https://evil@example', 'https://anvil.example/path',
                       'https://anvil.example?token=abc', 'https://anvil.example/#x', 'https://anvil.example\n',
                       'https://anvil.example:99999', 'javascript:alert(1)'):
            self.assertIsNone(details_url(origin, 1, 7))
        self.assertIsNone(details_url('https://anvil.example', '../1', 7))
        self.assertIsNone(details_url('https://anvil.example', 1, True))
