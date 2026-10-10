"""Real selected settings, delivery gates and public projection use one report."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import uuid
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.dispatch_controller import DispatchController, GitHubPrepare
from forge.factory.observation_controller import ObservationController
from forge.factory.repository_readiness import RepositoryReadiness
from forge.factory.store_views import PipelineViews
from forge.repository_setup import RepositorySetup, observation_key
from forge.settings import Settings, digest
from forge.store.delivery import Delivery
from forge.store.executions import Executions, ExecutionQueries
from forge.store.models import Command, TransitionError, VersionConflict
from forge.store.repository_readiness import read_revision
from forge.store.queries import Queries
from forge.store.transitions import Lifecycle
from tests.test_settings import configured
from tests.test_sqlite_identity import selected_store
from tests.test_sqlite_runtime_delivery import GitHub
from tests import test_store_recovery_controller as recovery_helpers


class RepositoryReadinessTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(); self.addCleanup(directory.cleanup)
        self.root = Path(directory.name); self.now = 100.0
        self.store = selected_store(self.root)
        self.github = GitHub()
        self.settings = Settings(self.root, client=self.github, clock=lambda: self.now)
        self.settings.save_config(configured()); self.settings.refresh()
        self.repo = {'id': 1, 'name': 'owner/repo', 'enabled': True}
        self.report = dict(repo_id=1, branch='main', observed_at=self.now, available=True,
            requirements=[{'id': 'reviews', 'passed': True}, {'id': 'stale', 'passed': True}],
            bypass={'status': 'ready'})
        self.save_report()
        config = self.settings.load(); config['repositories'] = [self.repo]
        self.settings.save(config, digest(self.settings.load()))
        self.life = Lifecycle(self.store, clock=lambda: self.now)
        self.pins = []
        for number in (1, 2):
            row = self.life.admit_issue(request_id='admit-' + str(number), actor_id='operator', unit_id='u' + str(number),
                issue_id='i' + str(number), repository_id=1, repository_name='owner/repo', issue_number=number,
                title='Issue', body='', requirements='github-requirements', observed_at=90)
            self.pins.append(dict(issue_id='i' + str(number), issue_version=1, requirements_revision_id=row['revision_id'],
                                 unit_id='u' + str(number), unit_version=1, member_id=row['member_id']))
        self.readiness = RepositoryReadiness(self.store, self.settings, clock=lambda: self.now)
        self.config = validate({'enabled': True, 'image': 'sha256:' + '1' * 64})
        self.runner = Mock(); self.runner.capacity.return_value = {'allowed': True}
        self.github.protection_report = lambda item, candidate: deepcopy(self.report)
        # Existing runtime fake has its own singleton digest; use the admitted pins.
        self.github.issue = lambda item: (dict(number=item['number'], state='open', title='Issue', body='',
            labels=[], assignees=[], updated_at='2026-10-10T00:00:00Z'), 'github-requirements')
        self.dispatch = DispatchController(self.store, self.runner, config=self.config,
            prepare=GitHubPrepare(self.github, self.config, readiness=self.readiness),
            admission=self.readiness.allowed, readiness=self.readiness, clock=lambda: self.now)

    def save_report(self, *, revision=None):
        RepositorySetup(self.settings).save_observation(1, {'repositories': {'1': dict(name=self.repo['name'],
            revision=revision or observation_key(self.settings.load(), self.repo), started_at=self.now,
            status='ready' if self.report.get('available') and self.report.get('bypass', {}).get('status') != 'missing' else 'needs_setup',
            report=deepcopy(self.report))}})

    def command(self, unit='u1'):
        return Command(str(uuid.uuid4()), 'test', unit, Queries(self.store).unit(unit)['version'])

    def gates(self, unit='u1'):
        with self.store.read_snapshot() as db:
            return [dict(row) for row in db.execute('SELECT * FROM unit_gates WHERE unit_id=? AND resolved_at IS NULL', (unit,))]

    def test_missing_bypass_is_actionable_for_each_grouped_member_without_reserving(self):
        proposal = dict(repository_id=1, base_branch='main', members=self.pins, dependencies=[], existing_prs=[], rationale='One change')
        delivery = Delivery(self.store, clock=lambda: self.now)
        delivery.compose(Command('compose', 'operator', 'batch', 1), **proposal,
                         approval={'approved': True, 'composition_hash': delivery.composition_hash(**proposal)})
        self.report['bypass'] = {'status': 'missing'}; self.save_report()
        self.assertEqual(self.dispatch.step('batch')['reason'], 'repository_setup')
        views = PipelineViews(self.store, runtime=self.config, clock=lambda: self.now)
        for identifier in ('i1', 'i2'):
            row = views.issues(key=identifier)['issue']
            self.assertTrue(row['setup_needed'], row)
            self.assertEqual(row['setup_url'], '/settings/repositories/1?setup=1')
            self.assertEqual(row['reason_code'], 'repository_setup')
            self.assertEqual(row['summary']['plain'], 'GitHub repository rules need attention. Open Repository setup to see which requirements need changes.')
            self.assertEqual(row['status'], 'attention')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT enabled FROM repositories WHERE id=1').fetchone()[0], 1)

    def test_public_setup_explanation_describes_evidence_without_leaking_codes(self):
        views = PipelineViews(self.store, runtime=self.config, clock=lambda: self.now)
        reasons = {
            'protection_unchecked': 'current setup',
            'protection_stale': 'out of date',
            'protection_branch_changed': 'target branch',
            'protection_identity_changed': 'different repository',
            'protection_unavailable': 'could not verify',
            'github_access_unverified': 'GitHub Apps’ access',
            'repository_not_enabled': 'not enabled',
            'repository_disabled': 'not enabled',
            'future_readiness_reason': 'access and rules',
            None: 'access and rules',
        }
        for reason, text in reasons.items():
            with self.subTest(reason=reason):
                gate = self.life.open_gate(self.command(), 'repository_setup', details={'reason': reason})
                row = views.issues(key='i1')['issue']
                self.assertIn(text, row['summary']['plain'])
                self.assertNotIn(reason or 'None', row['summary']['plain'])
                self.assertEqual(row['summary']['headline'], row['explanation'])
                self.assertEqual(row['reason_code'], 'repository_setup')
                self.assertEqual(row['setup_url'], '/settings/repositories/1?setup=1')
                self.life.resolve_gate(self.command(), gate['gate_id'], release='observation',
                                       evidence={'verified': True, 'observed_at': self.now})
        self.life.open_gate(self.command(), 'repository_setup', details={
            'reason': 'github_access_unverified', 'operator_summary': 'Reconnect the Review App in Repository setup.'})
        self.assertEqual(views.issues(key='i1')['issue']['summary']['plain'], 'Reconnect the Review App in Repository setup.')

    def test_live_dispatch_failure_persists_report_then_fresh_observation_resumes(self):
        # Saved readiness is positive; the actual final remote read finds a repair needed.
        self.report['bypass'] = {'status': 'missing'}
        self.assertEqual(self.dispatch.step('u1')['reason'], 'repository_setup')
        self.assertEqual(RepositorySetup(self.settings).read()['repositories']['1']['report']['bypass']['status'], 'missing')
        self.now += 1
        self.life.pause(self.command())
        self.report['bypass'] = {'status': 'ready'}; self.report['observed_at'] = self.now; self.save_report()
        observer = ObservationController(self.store, self.github, admission=lambda item: self.settings.admission(item['repo'])['allowed'],
                                        readiness=self.readiness, clock=lambda: self.now)
        observer.step('u1')
        self.assertEqual([gate['code'] for gate in self.gates()], ['operator_pause'])
        self.now += 1; self.life.resume(self.command())
        self.assertTrue(self.dispatch.step('u1')['reserved'])

    def test_stale_unavailable_wrong_revision_and_wrong_branch_fail_closed(self):
        original = deepcopy(self.report)
        for field, value, reason in [('observed_at', -201, 'protection_stale'), ('available', False, 'protection_unavailable'),
                                     ('branch', 'other', 'protection_branch_changed'), ('repo_id', 9, 'protection_identity_changed')]:
            with self.subTest(field=field):
                self.report = {**original, field: value}; self.save_report()
                result = self.readiness.evaluate({'repo': 'owner/repo', 'target_branch': 'main'})
                self.assertFalse(result['allowed']); self.assertEqual(result['reason'], reason)
        self.report = original; self.save_report(revision='old-connections')
        self.assertEqual(self.readiness.evaluate({'repo': 'owner/repo', 'target_branch': 'main'})['reason'], 'protection_unchecked')
        self.assertFalse(self.dispatch.step('u1')['reserved'])
        # Auxiliary planning uses access-only admission even when implementation is blocked.
        self.assertTrue(self.settings.admission('owner/repo')['allowed'])

    def test_running_build_survives_report_expiry_and_repair(self):
        identifier = self.dispatch.step('u1')['job_id']
        executions = Executions(self.store, clock=lambda: self.now)
        job = ExecutionQueries(self.store).job(identifier)
        executions.progress(self.command(), identifier, execution_fence=job['execution_fence'],
                            phase='starting', container_id='owned-container')
        executions.progress(self.command(), identifier, execution_fence=job['execution_fence'],
                            phase='running', container_id='owned-container')
        before = ExecutionQueries(self.store).job(identifier)
        self.now = 401
        self.readiness.observe('u1')
        stale = ExecutionQueries(self.store).job(identifier)
        self.assertFalse(stale['fenced'])
        self.assertEqual(stale['fencing_generation'], before['fencing_generation'])
        self.assertEqual(stale['phase'], 'running')
        self.assertEqual(json.loads(self.gates()[0]['details_json'])['reason'], 'protection_stale')
        self.report['observed_at'] = self.now; self.save_report()
        self.readiness.observe('u1')
        self.assertEqual(self.gates(), [])
        executions.collect(self.command(), identifier, execution_fence=job['execution_fence'], capture={'result': None, 'logs': ''})
        captured = ExecutionQueries(self.store).job(identifier)
        self.assertTrue(captured['capture_authorized'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM budget_entries').fetchone()[0], 0)

    def test_gate_release_rejects_config_access_or_report_change_after_evaluation(self):
        self.report['bypass'] = {'status': 'missing'}; self.save_report(); self.readiness.observe('u1')
        self.report['bypass'] = {'status': 'ready'}; self.save_report()
        for kind in ('config', 'access', 'report'):
            with self.subTest(kind=kind):
                authority = self.readiness.evaluate({'repo': 'owner/repo', 'target_branch': 'main'})
                if kind == 'config':
                    config = self.settings.load(); config['connections']['controller']['app_id'] += 100
                    self.settings.save_config(config)
                elif kind == 'access':
                    access = self.settings.observation(); access['roles']['controller']['status'] = 'unavailable'
                    self.settings.save_observation(access)
                else:
                    self.report['bypass'] = {'status': 'missing'}; self.save_report()
                with self.assertRaisesRegex(VersionConflict, 'readiness changed'):
                    self.life.observe_repository_readiness(self.command(), **authority)
                self.assertEqual([gate['code'] for gate in self.gates()], ['repository_setup'])
                # Restore the real access and setup observations for the next race.
                config = self.settings.load(); config['connections'] = configured()['connections']
                self.settings.save_config(config); self.settings.refresh()
                self.report['bypass'] = {'status': 'ready'}; self.save_report()

    def test_access_expiry_rejects_release_and_diagnostic_exemption_without_database_change(self):
        # Access was observed at100 (expires1000); protection is newer.
        self.now = 999
        self.report['observed_at'] = self.now
        for status in ('ready', 'missing'):
            with self.subTest(bypass=status):
                self.now = 999
                self.report['bypass'] = {'status': status}; self.save_report()
                value = self.readiness.evaluate({'repo': 'owner/repo', 'target_branch': 'main'})
                self.assertTrue(value['access_allowed'])
                self.assertEqual(value['valid_until'], 1000)
                self.now = 1001
                with self.assertRaisesRegex(VersionConflict, 'readiness changed'):
                    self.life.observe_repository_readiness(self.command(), **value)
                self.assertEqual(self.gates(), [])

    def test_config_change_during_remote_report_cannot_publish_old_proof(self):
        before = RepositorySetup(self.settings).read()
        def changed(item, candidate):
            config = self.settings.load(); config['connections']['controller']['app_id'] += 100
            self.settings.save_config(config)
            return deepcopy(self.report)
        self.github.protection_report = changed
        with self.assertRaisesRegex(VersionConflict, 'settings changed'):
            self.dispatch.step('u1')
        self.assertEqual(RepositorySetup(self.settings).read(), before)
        self.assertFalse(self.readiness.allowed({'repo': 'owner/repo', 'target_branch': 'main'}))


class DiagnosticReadinessTests(unittest.TestCase):
    setUp = recovery_helpers.RecoveryControllerTests.setUp
    driver = recovery_helpers.RecoveryControllerTests.driver
    command = recovery_helpers.RecoveryControllerTests.command
    failed_build = recovery_helpers.RecoveryControllerTests.failed_build
    reserve_recovery = recovery_helpers.RecoveryControllerTests.reserve_recovery
    finish_recovery = recovery_helpers.RecoveryControllerTests.finish_recovery
    view = recovery_helpers.RecoveryControllerTests.view
    budget = recovery_helpers.RecoveryControllerTests.budget

    def readiness(self, *, access=True):
        with self.store.read_snapshot() as db:
            authority = dict(domain='live:factory', scope='live',
                revision=read_revision(db, domain='live:factory', scope='live', repository_id=1))
        return self.life.observe_repository_readiness(self.command(), repository_id=1, base_branch='main',
            allowed=False, access_allowed=access, reason='protection_needs_setup' if access else 'github_access_unverified',
            source_revision='verified-current-connections', read_authority=authority, valid_until=self.now + 1000 if access else None)

    def test_diagnostics_keep_exact_lease_but_mixed_access_failure_blocks(self):
        failed = self.failed_build()
        self.readiness()
        investigation = self.reserve_recovery(failed)
        self.now += 1
        self.readiness()
        self.assertFalse(self.jobs.job(investigation)['fenced'])
        self.now += 1
        self.readiness(access=False)
        self.assertTrue(self.jobs.job(investigation)['fenced'])
        self.assertEqual(self.budget(), 1)

    def test_diagnosis_can_finish_without_correcting_publication_charge(self):
        failed = self.failed_build(); self.readiness()
        investigation = self.reserve_recovery(failed)
        self.github.publish(self.jobs.job(failed))
        answer = self.finish_recovery(investigation)
        self.assertTrue(answer['accepted'])
        self.assertFalse(answer['corrected'])
        self.assertEqual(self.budget(), 1)
        self.assertIsNone(self.view()['current_candidate_id'])
        self.assertEqual(self.view()['reason_code'], 'repository_setup')

    def test_access_and_protection_blocker_never_admits_diagnostics(self):
        failed = self.failed_build(); self.readiness(access=False)
        with self.assertRaisesRegex(TransitionError, 'gate blocks recovery'):
            self.recovery.schedule(failed)
        self.assertEqual(self.budget(), 1)
