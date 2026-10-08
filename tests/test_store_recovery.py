from dataclasses import replace
import tempfile
import unittest

from forge.store.artifacts import Artifacts
from forge.store.db import Store
from forge.store.executions import ExecutionQueries, Launch
from forge.store.models import Candidate, Command, TransitionError, IdempotencyConflict
from forge.store.recovery import Recovery, RecoveryQueries
from forge.store.transitions import manifest_hash, project_unit


class RecoveryStoreTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store.create(directory.name)
        self.now, self.serial = 100.0, 0
        self.life = Recovery(self.store, clock=lambda: self.now)
        admitted = self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
            repository_id=1, repository_name='owner/repo', issue_number=1, title='Issue', body='',
            requirements='requirements', observed_at=90)
        self.member = admitted['member_id']
        self.manifest = dict(requirements_revision_id=admitted['revision_id'], base_oid='base', policy_hash='policy')
        self.queries = ExecutionQueries(self.store)
        self.recoveries = RecoveryQueries(self.store)

    def command(self):
        self.serial += 1
        return Command(str(self.serial), 'controller', 'u', self.view()['version'])

    def view(self):
        with self.store.read_snapshot() as db:
            return project_unit(db, 'u')

    def launch(self):
        return Launch('forge/issue-1', 'controller[bot]', 'codex', self.now + 300, {}, {})

    def capture_cleanup(self, job_id):
        job = self.queries.job(job_id)
        self.life.collect(self.command(), job_id, execution_fence=job['execution_fence'],
                          capture={'result': None, 'logs': 'preserved diagnostic', 'missing': []})
        self.life.verify_cleanup(self.command(), job_id, execution_fence=job['execution_fence'],
                                 evidence={'process_stopped': True, 'resources_released': True})
        return self.queries.job(job_id)

    def fail(self):
        result = self.life.reserve_job(self.command(), 'build', slot='build-1', execution_fence='fence-' + str(self.serial),
                                      member_id=self.member, input_manifest=self.manifest, launch=self.launch())
        job = self.capture_cleanup(result['job_id'])
        self.life.record_failure(self.command(), job['id'], execution_fence=job['execution_fence'],
                                 input_manifest=job['input_manifest'], evidence={'reason': 'failed tests'})
        return self.queries.job(job['id'])

    def reserve_recovery(self, original):
        result = self.life.reserve(self.command(), original['id'], slot='recovery-1',
                                   execution_fence='recovery-' + str(self.serial), launch=self.launch())
        return self.queries.job(result['job_id'])

    @staticmethod
    def findings(pattern='missing dependency', change=None):
        return dict(summary='Investigated failure', pattern=pattern, evidence=['preserved test output'], uncertainty='',
                    alternative={'change': change, 'reason': 'Test evidence', 'evidence': 'failing test'} if change else None)

    def finish(self, original, findings=None, incomplete=False):
        recovery = self.reserve_recovery(original)
        self.life.progress(self.command(), recovery['id'], execution_fence=recovery['execution_fence'], phase='running', container_id='container')
        recovery = self.capture_cleanup(recovery['id'])
        outcome = self.life.finish(self.command(), recovery['id'], execution_fence=recovery['execution_fence'],
            input_manifest=recovery['input_manifest'], findings=findings or self.findings(), incomplete=incomplete)
        return recovery, outcome

    def test_failure_creates_exact_durable_facts_and_recovery_pins_original_manifest(self):
        failed = self.fail()
        self.assertEqual(self.recoveries.pending_failures()['items'], [failed['id']])
        recovery = self.reserve_recovery(failed)
        self.assertTrue(recovery['launch_allowed'])
        self.assertEqual(recovery['failure']['attempt'], failed['id'])
        self.assertEqual(recovery['failure']['input_manifest'], failed['input_manifest'])
        self.assertEqual(recovery['failure']['failure_evidence']['reason'], 'failed tests')
        self.assertIn('preserved diagnostic', recovery['failure']['logs'])
        self.assertEqual(recovery['input_manifest']['failed_input_manifest_hash'], manifest_hash(failed['input_manifest']))
        self.assertEqual(self.recoveries.pending_failures()['items'], [])
        with self.assertRaises(TransitionError):
            self.reserve_recovery(failed)

    def test_recovery_requires_original_cleanup_and_honors_operator_pause(self):
        result = self.life.reserve_job(self.command(), 'build', slot='build-1', execution_fence='f',
                                      member_id=self.member, input_manifest=self.manifest, launch=self.launch())
        self.life.record_failure(self.command(), result['job_id'], execution_fence='f', input_manifest=self.manifest, evidence={'reason':'failed'})
        with self.assertRaises(TransitionError):
            self.reserve_recovery({'id': result['job_id']})
        self.life.verify_cleanup(self.command(), result['job_id'], execution_fence='f',
                                 evidence={'process_stopped':True,'resources_released':True})
        self.life.pause(self.command())
        with self.assertRaises(TransitionError):
            self.reserve_recovery({'id': result['job_id']})

    def test_incomplete_investigation_is_retained_without_charge_or_recursive_recovery(self):
        failed = self.fail()
        recovery, outcome = self.finish(failed, incomplete=True)
        self.assertTrue(outcome['accepted'])
        self.assertEqual(self.view()['charged_failures'], 1)
        self.assertEqual(self.recoveries.pending_failures()['items'], [])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempt_failures').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT incomplete FROM recovery_findings').fetchone()[0], 1)
        with self.assertRaises(TransitionError):
            self.life.record_failure(self.command(), recovery['id'], execution_fence=recovery['execution_fence'],
                                     input_manifest=recovery['input_manifest'], evidence={'reason':'failed recovery'})

    def test_third_failure_is_investigated_and_budget_stays_exhausted(self):
        for index in range(3):
            failed = self.fail()
            recovery, outcome = self.finish(failed, self.findings(pattern='distinct-' + str(index)))
            self.assertTrue(outcome['accepted'])
        self.assertEqual(self.view()['charged_failures'], 3)
        self.assertEqual(self.view()['reason_code'], 'budget_exhausted')
        self.assertEqual(self.recoveries.pending_failures()['items'], [])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_findings').fetchone()[0], 3)

    def test_repeated_pattern_requires_new_concrete_change_not_reworded_reason(self):
        self.finish(self.fail(), self.findings(change='Use bounded retries'))
        repeated = self.findings(change=' USE  BOUNDED retries ')
        repeated['alternative']['reason'] = 'A different explanation of the same approach'
        recovery, outcome = self.finish(self.fail(), repeated)
        self.assertTrue(outcome['approach_required'])
        self.assertEqual(self.view()['reason_code'], 'approach_required')
        with self.assertRaises(TransitionError):
            self.fail()

    def test_new_evidence_supported_change_is_saved_for_next_assignment(self):
        self.finish(self.fail(), self.findings(change='Fix dependency version'))
        _, outcome = self.finish(self.fail(), self.findings(change='Remove unsupported dependency'))
        self.assertFalse(outcome['approach_required'])
        next_job = self.fail()
        self.assertEqual(next_job['history']['retry_plan']['change'], 'Remove unsupported dependency')

    def test_operator_retry_cannot_skip_mandatory_investigation(self):
        failed = self.fail()
        with self.assertRaisesRegex(TransitionError, 'mandatory'):
            self.life.retry(self.command(), evidence={'approved':True})
        with self.assertRaisesRegex(TransitionError, 'typed'):
            self.life.reconcile_recovery(self.command(), failed['id'], evidence={'verified':True})
        self.finish(failed)
        self.life.retry(self.command(), evidence={'approved':True})
        self.assertEqual(self.view()['charged_failures'], 0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM attempt_failures').fetchone()[0], 1)

    def test_pause_after_capture_keeps_hold_and_cannot_correct_publication(self):
        failed = self.fail()
        recovery = self.capture_cleanup(self.reserve_recovery(failed)['id'])
        self.life.pause(self.command())
        candidate, evidence = self.publication(failed)
        outcome = self.life.recover_publication(self.command(), recovery['id'], execution_fence=recovery['execution_fence'],
            input_manifest=recovery['input_manifest'], findings=self.findings(), candidate=candidate,evidence=evidence)
        self.assertFalse(outcome['accepted'])
        self.assertFalse(outcome['corrected'])
        self.assertEqual(self.view()['charged_failures'], 1)
        self.assertEqual(self.view()['reason_code'], 'operator_pause')
        self.assertIsNone(self.view()['current_candidate_id'])

    def publication(self, failed):
        candidate = Candidate(1, failed['branch'], 'head', 'base', 'policy', manifest_hash('requirements'), manifest_hash(self.manifest))
        return candidate, {**{key:getattr(candidate,key) for key in ('head_oid','base_oid','pull_number','policy_hash','requirements_hash','input_manifest_hash')},
                           'verified':True,'observed_at':self.now,'failed_job_id':failed['id']}

    def test_verified_publication_corrects_only_original_charge_and_replays_once(self):
        failed = self.fail()
        recovery = self.capture_cleanup(self.reserve_recovery(failed)['id'])
        candidate, evidence = self.publication(failed)
        kwargs = dict(execution_fence=recovery['execution_fence'],input_manifest=recovery['input_manifest'],
                      findings=self.findings(),candidate=candidate,evidence=evidence)
        command = self.command()
        accepted = self.life.recover_publication(command,recovery['id'],**kwargs)
        self.assertEqual(self.life.recover_publication(command,recovery['id'],**kwargs),accepted)
        self.assertEqual(self.view()['charged_failures'],0)
        self.assertEqual(self.view()['state'],'reviewing')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM budget_entries WHERE kind='correction'").fetchone()[0],1)
            self.assertEqual(db.execute('SELECT outcome FROM jobs WHERE id=?',(failed['id'],)).fetchone()[0],'failure')

    def test_unverified_publication_does_not_finish_or_correct(self):
        failed = self.fail()
        recovery = self.capture_cleanup(self.reserve_recovery(failed)['id'])
        candidate,evidence = self.publication(failed)
        for change in ({'verified':False},{'failed_job_id':'other'},{'observed_at':1},{'policy_hash':'other'}):
            with self.subTest(change=change),self.assertRaises(TransitionError):
                self.life.recover_publication(self.command(),recovery['id'],execution_fence=recovery['execution_fence'],
                    input_manifest=recovery['input_manifest'],findings=self.findings(),candidate=candidate,evidence={**evidence,**change})
        self.assertEqual(self.view()['charged_failures'],1)
        self.assertIsNone(self.queries.job(recovery['id'])['outcome'])

    def test_provider_wait_cools_down_immutable_account_without_charging(self):
        launch = replace(self.launch(), provider_id='account-one', runner_config={'provider_auth_file_env':'AUTH_ONE'})
        result = self.life.reserve_job(self.command(), 'build', slot='build-1', execution_fence='provider-fence',
                                      member_id=self.member, input_manifest=self.manifest, launch=launch)
        job = self.capture_cleanup(result['job_id'])
        self.life.defer_job(self.command(),job['id'],execution_fence=job['execution_fence'],input_manifest=job['input_manifest'],
            evidence={'reason':'unavailable'}, code='provider_unavailable',
            details={'reason':'blocked_auth','retry_at':200,'provider':'worker-chosen-provider','provider_id':'worker-chosen-account'})
        with self.store.read_snapshot() as db:
            row = dict(db.execute('SELECT * FROM provider_cooldowns').fetchone())
        self.assertEqual(row['account_ref'],'AUTH_ONE')
        self.assertEqual(row['provider'],'codex')
        self.assertEqual(row['source_job_id'],job['id'])
        self.assertEqual(row['retry_at'],200)
        self.assertEqual(self.view()['charged_failures'],0)
        self.assertEqual(self.recoveries.pending_failures()['items'],[])

    def test_recovered_publication_adds_verification_to_the_new_candidate_atomically(self):
        failed = self.fail()
        recovery = self.capture_cleanup(self.reserve_recovery(failed)['id'])
        candidate,evidence = self.publication(failed)
        result = self.life.recover_publication(self.command(),recovery['id'],execution_fence=recovery['execution_fence'],
            input_manifest=recovery['input_manifest'],findings=self.findings(),candidate=candidate,evidence=evidence,
            incomplete=True,gates=[{'code':'verification_human','details':{'what':'Run production smoke test'}}])
        self.assertTrue(result['corrected'])
        self.assertEqual(self.view()['charged_failures'],0)
        self.assertEqual(self.view()['reason_code'],'verification_human')
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT candidate_id FROM unit_gates WHERE code='verification_human'").fetchone()[0],result['candidate_id'])

    def test_logs_expire_after_investigation_but_diagnostic_excerpt_is_durable(self):
        failed = self.fail()
        self.now += 31 * 86400
        self.assertEqual(Artifacts(self.store).prune_raw_logs(now=self.now),0)
        self.finish(failed)
        self.assertGreater(Artifacts(self.store).prune_raw_logs(now=self.now),0)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM artifact_pins').fetchone()[0],0)
        self.assertIn(b'preserved diagnostic',Artifacts(self.store).read(failed['id'],'failure_evidence').content)


if __name__ == '__main__':
    unittest.main()
