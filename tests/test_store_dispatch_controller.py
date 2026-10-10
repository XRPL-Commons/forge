from copy import deepcopy
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.config import validate, policy
from forge.factory.dispatch_controller import DispatchController, GitHubPrepare, PreparedDispatch
from forge.factory.github import GitHubUnavailable
from forge.store.db import Store
from forge.store.executions import ExecutionQueries, Executions, Launch
from forge.store.models import Candidate, Command, TransitionError, VersionConflict
from forge.store.transitions import manifest_hash


BASE = 'b' * 40
HEAD = 'a' * 40


class Runner:
    def __init__(self):
        self.allowed = True
        self.ready_calls = 0

    def ready(self):
        self.ready_calls += 1

    def capacity(self):
        return {'allowed': self.allowed}


class GitHub:
    def __init__(self):
        self.remote = dict(number=1, state='open', title='Fix issue', body='', labels=[], assignees=[])
        self.requirements = 'github-requirements'
        self.protected = True
        self.branch = 'main'
        self.head = BASE
        self.open_pulls = []
        self.draft = None

    def issue(self, item):
        return deepcopy(self.remote), self.requirements

    def pulls(self, item):
        return self.open_pulls

    def target(self, item):
        return self.branch

    def protections(self, item, candidate):
        return self.protected

    def call(self, role, item, method, path):
        assert method == 'GET'
        return {'commit': {'sha': self.head}}

    def ci_evidence(self, item):
        return {'verified': item['candidate']} if item['candidate'] else None

    def publisher(self, role):
        return role + '[bot]'

    def own_draft(self, item, number, publisher):
        return self.draft


class DispatchTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now = 100.0
        self.life = Executions(self.store, clock=lambda: self.now)
        self.config = validate({'enabled': True, 'image': 'sha256:' + '1' * 64})
        self.runner = Runner()
        self.github = GitHub()
        self.prepare = GitHubPrepare(self.github, self.config)
        self.allowed = True
        self.controller = self.driver()
        self.admit('u', 1)

    def admit(self, unit, number):
        return self.life.admit_issue(request_id='admit-' + unit, actor_id='operator', unit_id=unit,
            issue_id='issue-' + unit, repository_id=1, repository_name='owner/repo', issue_number=number,
            title='Fix issue', body='', requirements='github-requirements', observed_at=90)

    def driver(self, prepare=None):
        return DispatchController(self.store, self.runner, config=self.config,
            prepare=prepare or self.prepare, admission=lambda snapshot: self.allowed, clock=lambda: self.now)

    def command(self, unit='u'):
        return Command(str(self.now) + unit, 'test', unit, self.controller.snapshot(unit)['unit']['version'])

    def job(self, result):
        return ExecutionQueries(self.store).job(result['job_id'])

    def test_reserves_exact_inputs_without_starting_or_obtaining_tokens(self):
        result = self.controller.step('u')
        self.assertTrue(result['reserved'])
        job = self.job(result)
        self.assertEqual(job['role'], 'build')
        self.assertEqual(job['phase'], 'reserved')
        self.assertEqual(job['requirements'], 'github-requirements')
        self.assertEqual(job['input_manifest']['base_oid'], BASE)
        self.assertEqual(job['input_manifest']['policy_hash'], manifest_hash(policy(self.config,1,repository_name='owner/repo')))
        self.assertEqual(job['slot'], 'build-1')
        self.assertEqual(job['deadline'], 2800)
        self.assertTrue(job['branch'].startswith('forge/issue-1-'))
        self.assertEqual(self.controller.step('u')['reason'], 'cleanup_pending')

    def test_every_admission_failure_leaves_no_attempt_or_budget_charge(self):
        cases = [lambda: setattr(self, 'allowed', False),
                 lambda: setattr(self.runner, 'allowed', False),
                 lambda: self.github.remote.update(labels=[{'name': 'hold'}]),
                 lambda: self.github.remote.update(assignees=[{'login': 'human', 'type': 'User'}]),
                 lambda: self.github.remote.update(state='closed'),
                 lambda: setattr(self.github, 'requirements', 'changed'),
                 lambda: setattr(self.github, 'protected', False),
                 lambda: setattr(self.github, 'branch', 'changed')]
        for change in cases:
            with self.subTest(change=change):
                self.allowed = self.runner.allowed = True
                self.github = GitHub()
                self.prepare = GitHubPrepare(self.github, self.config)
                self.controller = self.driver()
                change()
                self.assertFalse(self.controller.step('u')['reserved'])
                with self.store.read_snapshot() as db:
                    self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0], 0)

    def test_pause_racing_with_remote_preparation_cannot_reserve(self):
        def prepare(snapshot, role):
            result = self.prepare(snapshot, role)
            self.life.pause(Command('pause', 'operator', 'u', snapshot['unit']['version']))
            return result
        with self.assertRaises(VersionConflict):
            self.driver(prepare).step('u')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_admission_is_rechecked_after_capacity(self):
        def prepare(snapshot, role):
            result = self.prepare(snapshot, role)
            self.allowed = False
            return result
        self.assertEqual(self.driver(prepare).step('u')['reason'], 'repository_setup')

    def test_scoped_gate_prevents_preparation_and_removing_hold_allows_dispatch(self):
        command = self.command()
        gate = self.life.open_gate(command, 'label_hold', details={'label': 'hold'}, member_id=self.controller.snapshot('u')['member']['id'])
        self.assertEqual(self.controller.step('u')['reason'], 'label_hold')
        self.assertEqual(self.runner.ready_calls, 0)
        self.now += 1
        self.life.resolve_gate(self.command(), gate['gate_id'], release='observation', evidence={'verified': True, 'observed_at': self.now})
        self.assertTrue(self.controller.step('u')['reserved'])

    def test_provider_rotation_and_account_lease_survive_controller_restart(self):
        self.config['provider_pool'] = [dict(id='first', provider='codex', model=None,
            provider_environment=[], provider_auth_file_env='AUTH_ONE'),
            dict(id='second', provider='codex', model='second-model', provider_environment=[], provider_auth_file_env='AUTH_TWO')]
        def prepare(snapshot, role):
            return PreparedDispatch(BASE, role + '[bot]', deepcopy(self.config))
        first = self.driver(prepare).step('u')
        self.admit('v', 2)
        second = self.driver(prepare).step('v')
        self.assertEqual(self.job(first)['provider_id'], 'first')
        self.assertEqual(self.job(second)['provider_id'], 'second')
        self.admit('w', 3)
        self.assertEqual(self.driver(prepare).step('w')['reason'], 'provider_unavailable')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT cursor FROM runtime_controls WHERE name='provider_cursor'").fetchone()[0], '0')

    def test_transaction_rechecks_account_and_rolls_back_entire_reservation(self):
        self.config['provider_auth_file_env'] = 'AUTH_ONE'
        self.prepare = GitHubPrepare(self.github, self.config)
        first = self.driver().step('u')
        added = self.admit('v', 2)
        launch = replace(Launch('forge/issue-2', 'build[bot]', 'codex', 200, self.config, {}))
        with self.assertRaisesRegex(TransitionError, 'provider account'):
            self.life.reserve_job(self.command('v'), 'build', slot='build-2', execution_fence='fence-two',
                input_manifest={'requirements_revision_id': added['revision_id'], 'base_oid': BASE, 'policy_hash': 'policy'},
                member_id=added['member_id'], launch=launch)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM attempts').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0], 1)
        self.assertEqual(self.job(first)['cleanup'], 0)

    def test_codex_parallel_snapshots_drain_old_refresh_writer(self):
        self.config['provider_auth_file_env'] = 'AUTH_ONE'
        def prepare(snapshot, role):
            return PreparedDispatch(BASE, role + '[bot]', deepcopy(self.config))
        first = self.driver(prepare).step('u')
        self.admit('v', 2)
        self.config['codex_parallel_auth'] = True
        self.assertEqual(self.driver(prepare).step('v')['reason'], 'provider_unavailable')
        job = self.job(first)
        self.life.fence_execution(Command('fence', 'test', 'u', job['unit_version']), job['id'], execution_fence=job['execution_fence'], reason='test')
        self.now += 1
        self.life.verify_cleanup(self.command(), job['id'], execution_fence=job['execution_fence'],
                                 evidence={'process_stopped': True, 'resources_released': True})
        self.assertTrue(self.driver(prepare).step('v')['reserved'])
        self.admit('w', 3)
        self.assertTrue(self.driver(prepare).step('w')['reserved'])

    def test_provider_cooldown_and_observation_fail_closed(self):
        with self.store.write() as db:
            db.execute("INSERT INTO provider_cooldowns(account_ref,provider,reason_code,retry_at,observed_at) VALUES('codex','codex','availability',200,90)")
        self.assertEqual(self.controller.step('u')['reason'], 'provider_unavailable')
        self.now = 201
        with self.store.write() as db:
            db.execute("INSERT INTO provider_observations VALUES('o','codex','codex','unknown',NULL,201,'{}')")
        self.assertEqual(self.controller.step('u')['reason'], 'provider_unavailable')

    def test_review_pins_candidate_and_rejects_policy_or_base_drift(self):
        build = self.controller.step('u')
        job = self.job(build)
        candidate = Candidate(10, job['branch'], HEAD, BASE, job['input_manifest']['policy_hash'],
                              manifest_hash('github-requirements'), manifest_hash(job['input_manifest']))
        self.life.finish_job(Command('finish', 'test', 'u', job['unit_version']), job['id'],
            execution_fence=job['execution_fence'], input_manifest=job['input_manifest'],
            evidence={'head_oid': HEAD, 'base_oid': BASE, 'pull_number': 10}, candidate=candidate)
        self.now += 1
        self.life.verify_cleanup(self.command(), job['id'], execution_fence=job['execution_fence'],
                                 evidence={'process_stopped': True, 'resources_released': True})
        self.github.head = 'c' * 40
        self.assertEqual(self.controller.step('u')['reason'], 'observation_changed')
        self.github.head = BASE
        reviewed = self.controller.step('u')
        review = self.job(reviewed)
        self.assertEqual(review['role'], 'review')
        self.assertEqual(review['candidate']['head'], HEAD)
        self.assertEqual(review['branch'], job['branch'])
        self.assertEqual(review['input_manifest']['candidate_id'], review['candidate_id'])
        self.assertEqual(review['ci_evidence']['verified'], {'head': HEAD, 'base': BASE})
        self.assertEqual(review['review_policy'], policy(self.config,1,repository_name='owner/repo'))

    def test_existing_draft_resumes_branch_and_pr(self):
        self.github.draft = {'branch': 'forge/issue-1-existing', 'pr': 9}
        job = self.job(self.controller.step('u'))
        self.assertEqual(job['branch'], 'forge/issue-1-existing')
        self.assertEqual(job['resume_branch'], job['branch'])
        self.assertEqual(job['resume_pr'], 9)

    def test_cycle_is_bounded_and_unavailable_issue_does_not_starve_other_units(self):
        self.admit('v', 2)
        self.admit('w', 3)
        def prepare(snapshot, role):
            if snapshot['unit']['id'] == 'u':
                raise GitHubUnavailable('unavailable')
            return PreparedDispatch(BASE, role + '[bot]', self.config)
        page = self.driver(prepare).cycle(limit=2)
        self.assertEqual(page['next_cursor'], 'v')
        self.assertEqual(page['items'][0]['error'], 'GitHubUnavailable')
        self.assertTrue(page['items'][1]['reserved'])
        self.assertEqual(self.driver(prepare).cycle(after='v', limit=2)['items'][0]['unit_id'], 'w')

    def test_default_cycle_cursor_survives_restart_and_wraps_past_blocked_units(self):
        self.admit('v', 2)
        self.admit('w', 3)
        def prepare(snapshot, role):
            if snapshot['unit']['id'] != 'w':
                return None
            return PreparedDispatch(BASE, role + '[bot]', self.config)
        first = self.driver(prepare).cycle(limit=2)
        self.assertEqual([item['unit_id'] for item in first['items']], ['u', 'v'])
        restarted = self.driver(prepare).cycle(limit=2)
        self.assertEqual(restarted['items'][0]['unit_id'], 'w')
        self.assertTrue(restarted['items'][0]['reserved'])
        self.assertIsNone(restarted['next_cursor'])
        self.assertEqual(self.driver(prepare).cycle(limit=2)['items'][0]['unit_id'], 'u')

    def test_cooldown_racing_with_capacity_is_checked_in_reservation_transaction(self):
        def capacity():
            with self.store.write() as db:
                db.execute("INSERT INTO provider_cooldowns(account_ref,provider,reason_code,retry_at,observed_at) VALUES('codex','codex','availability',200,90)")
            return {'allowed': True}
        self.runner.capacity = capacity
        with self.assertRaisesRegex(TransitionError, 'provider account'):
            self.controller.step('u')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_launches').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM job_leases').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM events WHERE kind='attempt_reserved'").fetchone()[0], 0)

    def test_review_priority_precedes_an_unserved_build_delivery(self):
        self.admit('v', 2)
        job = self.job(self.controller.step('v'))
        candidate = Candidate(10, job['branch'], HEAD, BASE, job['input_manifest']['policy_hash'],
                              manifest_hash('github-requirements'), manifest_hash(job['input_manifest']))
        self.life.finish_job(Command('finish-v', 'test', 'v', job['unit_version']), job['id'],
            execution_fence=job['execution_fence'], input_manifest=job['input_manifest'],
            evidence={'head_oid': HEAD, 'base_oid': BASE, 'pull_number': 10}, candidate=candidate)
        self.life.verify_cleanup(self.command('v'), job['id'], execution_fence=job['execution_fence'],
                                 evidence={'process_stopped': True, 'resources_released': True})
        page = self.driver().cycle()
        self.assertEqual([(item['unit_id'], item['role']) for item in page['items']], [('v', 'review'), ('u', 'build')])
        review = self.job(page['items'][0])
        self.life.finish_job(Command('finish-review-v', 'test', 'v', review['unit_version']), review['id'],
            execution_fence=review['execution_fence'], input_manifest=review['input_manifest'],
            evidence={'candidate_id': review['candidate_id'], 'verdict': 'pass'})
        self.now += 1
        self.life.verify_cleanup(self.command('v'), review['id'], execution_fence=review['execution_fence'],
                                 evidence={'process_stopped': True, 'resources_released': True})
        audit = self.driver().cycle()['items'][0]
        self.assertEqual((audit['unit_id'], audit['role']), ('v', 'audit'))

    def test_schema28_reservations_retain_order_through_index_upgrade(self):
        from forge.store import db as storage
        from forge.store import upgrade
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        catalog = storage._migrations()
        with patch('forge.store.db._migrations', return_value=catalog[:28]):
            self.store = Store.create(directory.name)
        self.life = Executions(self.store, clock=lambda: self.now)
        self.admit('u', 1); self.admit('v', 2)
        old = self.job(self.driver().step('u'))
        with self.store.read_snapshot() as db:
            history = [tuple(row) for row in db.execute('SELECT * FROM events ORDER BY sequence')]
            jobs = [tuple(row) for row in db.execute('SELECT * FROM jobs ORDER BY id')]
        target = Store(directory.name)
        self.assertEqual(upgrade.apply(target)['current_schema'], 29)
        self.assertEqual(upgrade.apply(target)['pending_migrations'], [])
        self.store = target
        # Previously allocated u is still occupied; the new v gets first turn.
        page = self.driver().cycle()
        self.assertEqual([item['unit_id'] for item in page['items']], ['v', 'u'])
        self.assertTrue(page['items'][0]['reserved'])
        self.assertEqual(page['items'][1]['reason'], 'cleanup_pending')
        with self.store.read_snapshot() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM events ORDER BY sequence LIMIT ?', (len(history),))], history)
            self.assertEqual(tuple(db.execute('SELECT * FROM jobs WHERE id=?', (old['id'],)).fetchone()), jobs[0])
            plan = db.execute("EXPLAIN QUERY PLAN SELECT e.sequence FROM events e "
                "JOIN jobs j ON j.id=e.job_id JOIN job_launches l ON l.job_id=j.id "
                "WHERE e.unit_id=? AND e.kind='attempt_reserved' AND j.role='build' "
                'ORDER BY e.sequence DESC LIMIT 1', ('u',)).fetchall()
            self.assertTrue(any('unit_reservation_events' in row['detail'] for row in plan), [tuple(row) for row in plan])

    def test_capacity_is_shared_with_discussions(self):
        with self.store.write() as db:
            db.execute("INSERT INTO discussions(id,title,created_at,updated_at) VALUES('d','Talk',90,90)")
            db.execute("INSERT INTO discussion_executions(id,discussion_id,phase,created_at) VALUES('d1','d','reserved',90)")
            db.execute("INSERT INTO discussion_leases VALUES('build-1','d1','discussion-fence',90)")
        self.assertEqual(self.job(self.controller.step('u'))['slot'], 'build-2')
        self.admit('v', 2)
        self.config['provider_auth_file_env'] = 'AUTH_ONE'
        def prepare(snapshot, role):
            return PreparedDispatch(BASE, role + '[bot]', self.config)
        self.assertEqual(self.driver(prepare).step('v')['reason'], 'provider_unavailable')

    def test_staging_import_cannot_dispatch(self):
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata(key,value) VALUES('import_only','true')")
        with self.assertRaisesRegex(TransitionError, 'staging'):
            self.controller.step('u')

    def test_raw_secret_or_unknown_config_field_is_not_persisted(self):
        def prepare(snapshot, role):
            return PreparedDispatch(BASE, role + '[bot]', {**self.config, 'github_token': 'private-token'})
        with self.assertRaisesRegex(ValueError, 'Unknown runtime'):
            self.driver(prepare).step('u')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_launches').fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
