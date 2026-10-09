"""Composed SQLite delivery with real row adapters and fake external services.

This proves local orchestration, not a live GitHub or production qualification.
"""
from copy import deepcopy
import json
import tempfile
import unittest

from forge.factory.config import policy, validate
from forge.factory.contracts import candidate, digest
from forge.factory.dispatch_controller import DispatchController, GitHubPrepare
from forge.factory.effect_controller import EffectController
from forge.factory.execution_controller import ExecutionController
from forge.factory.merge_controller import MergeAdmission, MergeController
from forge.factory.observation_controller import ObservationController
from forge.factory.result_controller import ResultController
from forge.factory.sqlite_supervisor import SqliteSupervisor
from forge.store.db import Store
from forge.store.queries import Queries
from forge.store.transitions import Lifecycle


HEAD, BASE, MERGE = 'a' * 40, 'b' * 40, 'c' * 40


class GitHub:
    def __init__(self):
        self.pull = None
        self.approved = False
        self.merge_calls = 0
        self.checks = {}
        self.reviews = {}
        self.remote = dict(number=1, state='open', title='Fix issue', body='Body',
                           updated_at='2026-10-07T12:00:00Z', labels=[], assignees=[])

    def issue(self, item):
        return deepcopy(self.remote), 'github-requirements'

    def pulls(self, item):
        return [deepcopy(self.pull)] if self.pull and self.pull['state'] == 'open' else []

    def target(self, item):
        return 'main'

    def protections(self, item, value):
        return True

    def call(self, role, item, method, path):
        assert method == 'GET' and path == '/branches/main'
        return {'commit': {'sha': BASE}}

    def ci_evidence(self, item):
        return None

    def publisher(self, role):
        return role + '[bot]'

    def own_draft(self, item, number, publisher):
        return None

    def token(self, role, repo_id, worker):
        assert worker and repo_id == 1
        return 'ephemeral-token'

    def publish(self, job):
        self.pull = dict(number=7, state='open', draft=False, merged=False, mergeable=True,
            forge_owned=True, user={'login': job['publisher'], 'type': 'Bot'},
            body='<!-- forge-submission ' + json.dumps(dict(version=1, issue=1, attempt=job['id'], head=HEAD)) + ' -->',
            head={'sha': HEAD, 'ref': job['branch'], 'repo': {'id': 1}},
            base={'sha': BASE, 'ref': 'main', 'repo': {'id': 1}})

    def publication(self, job, requirements, policy_hash):
        if self.pull is None:
            return None
        value = candidate(self.pull, job, requirements, policy_hash)
        return {**value, 'branch': self.pull['head']['ref']}

    def pr(self, item, number):
        assert number == 7
        return deepcopy(self.pull)

    def verdict(self, job, report):
        assert report['candidate'] == job['candidate']['key']
        assert report['outcome'] == 'pass'
        check_id = {'review': 21, 'audit': 22}[job['role']]
        self.checks[job['role']] = check_id
        self.reviews[job['role']] = job['candidate']['head']
        return check_id

    def gates(self, item):
        return all(self.reviews.get(role) == item['candidate']['head']
                   and item['evidence'][role]['check_id'] == self.checks.get(role)
                   for role in ('review', 'audit'))

    def approvals(self, item, pr):
        return [{'state': 'APPROVED', 'counted': True, 'at': 200}] if self.approved else []

    def check(self, item, role, selected, external_id, conclusion, summary):
        assert selected['head'] == HEAD
        assert role == 'controller' and conclusion == 'success'
        return 23

    def merge(self, item):
        assert self.approved
        assert item['candidate']['head'] == HEAD
        self.merge_calls += 1
        self.pull.update(merged=True, state='closed', merge_commit_sha=MERGE)


class Runner:
    def __init__(self, github):
        self.github = github
        self.containers = {}
        self.launched = []
        self.cleaned = []

    def ready(self):
        pass

    def capacity(self):
        return {'allowed': True}

    def inspect(self, job):
        return self.containers.get(job['id'])

    def create(self, job):
        container = {'id': 'container-' + job['id'], 'status': 'created', 'running': False}
        self.containers[job['id']] = container
        return container['id']

    def prepare(self, job, token):
        assert token == 'ephemeral-token'

    def start(self, job):
        self.launched.append(job['role'])
        self.containers[job['id']]['status'] = 'exited'

    def capture(self, job):
        report = dict(version=1, attempt=job['id'], role=job['role'], outcome='pass', summary='Completed validation')
        if job['role'] == 'build':
            self.github.publish(job)
        else:
            report['candidate'] = job['candidate']['key']
        if job['role'] == 'audit':
            report.update(human_required='not_required', human_reason='Checks passed')
        return {'result': report, 'logs': 'worker log', 'execution': {'exit_code': 0}}

    def reset(self, job):
        self.containers.pop(job['id'], None)
        self.cleaned.append(job['role'])
        return {'container_absent': True, 'capacity_ready': True}


class SqliteSupervisorIntegrationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now = 100.0
        self.github = GitHub()
        self.runner = Runner(self.github)
        self.config = validate(dict(enabled=True, merge_enabled=True, human_required=True,
                                    image='sha256:' + '1' * 64))
        Lifecycle(self.store, clock=lambda: self.now).admit_issue(request_id='admit', actor_id='operator',
            unit_id='u', issue_id='i', repository_id=1, repository_name='owner/repo', issue_number=1,
            title='Fix issue', body='Body', requirements='github-requirements', observed_at=90)

    def driver(self):
        clock = lambda: self.now
        merge_admission = MergeAdmission(self.store, self.github, config=lambda: self.config,
            repository_ready=lambda repo: True, clock=clock)
        effects = EffectController(self.store, self.github, admission=merge_admission, clock=clock)
        return SqliteSupervisor(self.store,
            execution=ExecutionController(self.store, self.runner, self.github, enabled=lambda: True, admission=lambda job: True, clock=clock),
            observations=ObservationController(self.store, self.github, admission=lambda item: True,
                policy=lambda item: digest(policy(self.config)), clock=clock),
            results=ResultController(self.store, self.github, policy=lambda job: policy(self.config), verdicts=effects, clock=clock),
            merges=MergeController(self.store, admission=merge_admission, clock=clock), effects=effects,
            dispatch=DispatchController(self.store, self.runner, config=self.config,
                prepare=GitHubPrepare(self.github, self.config), admission=lambda snapshot: True, clock=clock), clock=clock)

    def cycle(self):
        self.now += 1
        # Reconstruct the composition each time to exercise persisted progress.
        return self.driver().cycle(limit=50)

    def view(self):
        return Queries(self.store).unit('u')

    def test_build_review_audit_human_approval_merge_across_restarts(self):
        self.cycle()
        self.assertEqual(self.view()['state'], 'building')
        self.assertEqual(self.runner.launched, [])
        self.cycle()
        self.assertEqual(self.view()['state'], 'reviewing')
        self.cycle()
        self.assertEqual(self.view()['state'], 'auditing')
        self.cycle()
        self.assertEqual(self.view()['state'], 'merging')
        self.assertEqual(self.view()['reason_code'], 'approval_required')
        self.assertEqual(self.github.merge_calls, 0)
        self.assertEqual(self.runner.launched, ['build', 'review', 'audit'])
        self.assertEqual(self.runner.cleaned, self.runner.launched)
        self.assertEqual(self.runner.containers, {})
        self.github.approved = True
        self.cycle()
        self.assertEqual(self.view()['state'], 'merged')
        self.assertEqual(Queries(self.store).issue('i')['lifecycle']['state'], 'done')
        self.assertEqual(self.github.merge_calls, 1)
        self.cycle()
        self.assertEqual(self.github.merge_calls, 1)
        self.assertEqual(self.runner.launched, ['build', 'review', 'audit'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM external_effects WHERE status='confirmed'").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE outcome='success' AND cleanup=1").fetchone()[0], 3)


class RecordingStage:
    def __init__(self, name, calls, *, error=None, cursor=None):
        self.name, self.calls, self.error, self.cursor = name, calls, error, cursor

    def cycle(self, *, limit, **kwargs):
        self.calls.append((self.name, limit, kwargs.get('after')))
        if self.error:
            raise self.error
        if self.name == 'effects':
            return []
        return {'items': [], 'next_cursor': self.cursor}


class SqliteSupervisorCompositionTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.calls = []
        self.now = 100.0

    def driver(self, *, next_pages=False, error=None):
        stages = {name: RecordingStage(name, self.calls, error=error if name == 'observations' else None,
                                      cursor=name + '-next' if next_pages else None)
                  for name in ('execution', 'observations', 'results', 'merges', 'effects', 'dispatch')}
        return SqliteSupervisor(self.store, **stages, clock=lambda: self.now)

    def test_order_page_cursors_and_retention_schedule_survive_restart(self):
        from unittest.mock import patch
        with patch('forge.factory.sqlite_supervisor.Artifacts.prune_raw_logs', return_value=0) as prune:
            first = self.driver(next_pages=True).cycle(limit=7)
            self.assertEqual([call[0] for call in self.calls],
                             ['execution', 'observations', 'results', 'merges', 'effects', 'dispatch'])
            self.assertEqual([call[1] for call in self.calls], [7] * 6)
            self.assertTrue(first['stages']['retention']['due'])
            self.calls.clear()
            self.now += 1
            second = self.driver().cycle(limit=7)
            for name, _, cursor in self.calls[:4]:
                self.assertEqual(cursor, name + '-next')
            self.assertFalse(second['stages']['retention']['due'])
            prune.assert_called_once_with(now=100.0, batch=500)
            self.now += 3600
            self.driver().cycle(limit=7)
            self.assertEqual(prune.call_count, 2)

    def test_observation_failure_preserves_its_cursor_and_allows_cleanup_results_dispatch(self):
        from forge.factory.github import GitHubUnavailable
        initial = self.driver()
        initial._put('observations:cursor', 'prior-unit')
        report = self.driver(error=GitHubUnavailable('private remote details')).cycle(limit=3)
        self.assertEqual([call[0] for call in self.calls],
                         ['execution', 'observations', 'results', 'merges', 'effects', 'dispatch'])
        self.assertEqual(report['stages']['observations'], {'error': 'GitHubUnavailable'})
        restarted = self.driver()
        self.assertEqual(restarted._get('observations:cursor'), 'prior-unit')
        heartbeat = restarted._get('heartbeat')
        self.assertEqual(heartbeat['errors'], {'observations': {'count': 1, 'codes': ['GitHubUnavailable']}})
        self.assertNotIn('private remote details', json.dumps(heartbeat))

    def test_optional_verification_and_recovery_are_paged_before_dispatch(self):
        driver = self.driver()
        driver.verification = RecordingStage('verification', self.calls, cursor='next-gate')
        driver.recovery = RecordingStage('recovery', self.calls, cursor={'results': 'next-result', 'failures': 'next-failure'})
        driver.cycle(limit=2)
        self.assertEqual([name for name, _, _ in self.calls],
            ['execution', 'observations', 'verification', 'results', 'recovery', 'merges', 'effects', 'dispatch'])
        self.calls.clear()
        restarted = self.driver()
        restarted.verification = RecordingStage('verification', self.calls)
        restarted.recovery = RecordingStage('recovery', self.calls)
        restarted.cycle(limit=2)
        cursors = {name: cursor for name, _, cursor in self.calls}
        self.assertEqual(cursors['verification'], 'next-gate')
        self.assertEqual(cursors['recovery'], {'results': 'next-result', 'failures': 'next-failure'})

    def test_staged_import_cannot_call_any_controller(self):
        from forge.store.models import TransitionError
        with self.store.write() as db:
            db.execute("INSERT INTO store_metadata(key,value) VALUES('import_only','true')")
        with self.assertRaises(TransitionError):
            self.driver().cycle()
        self.assertEqual(self.calls, [])

    def test_exclusive_cycle_lock_prevents_concurrent_effects(self):
        driver = self.driver()
        with driver._lock():
            with self.assertRaises(BlockingIOError):
                self.driver().cycle()
        self.assertEqual(self.calls, [])


class HeartbeatErrorTests(unittest.TestCase):
    def test_per_item_errors_are_counted_without_copying_private_payloads(self):
        errors = SqliteSupervisor._errors({
            'results': {'items': [{'error': 'GitHubUnavailable', 'payload': 'private'},
                                  {'error': 'GitHubUnavailable'}, {'job_id': 'success'}]},
            'effects': [{'error': 'VersionConflict'}, {'id': 'confirmed'}],
            'execution': {'items': []},
        })
        self.assertEqual(errors, {'results': {'count': 2, 'codes': ['GitHubUnavailable']},
                                  'effects': {'count': 1, 'codes': ['VersionConflict']}})
        self.assertNotIn('private', json.dumps(errors))
