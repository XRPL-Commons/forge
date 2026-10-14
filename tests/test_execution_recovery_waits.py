"""Recovery faults retry physical workers without losing original failure history."""
import json
import unittest
from unittest.mock import patch

from tests import test_store_recovery_controller as controller_fixtures
from forge.harnesses.execution import fault
from forge.store.execution_problems import ExecutionProblems
from forge.store.configuration import Configuration
from forge.store.models import TransitionError


class RecoveryExecutionWaitTests(unittest.TestCase):
    setUp=controller_fixtures.RecoveryControllerTests.setUp
    driver=controller_fixtures.RecoveryControllerTests.driver
    view=controller_fixtures.RecoveryControllerTests.view
    command=controller_fixtures.RecoveryControllerTests.command
    budget=controller_fixtures.RecoveryControllerTests.budget
    failed_build=controller_fixtures.RecoveryControllerTests.failed_build
    reserve_recovery=controller_fixtures.RecoveryControllerTests.reserve_recovery
    finish_recovery=controller_fixtures.RecoveryControllerTests.finish_recovery

    def blocked(self, failed):
        try:result=self.recovery.schedule(failed)
        except TransitionError:return
        self.assertNotIn('reserved',result)

    def capture_fault(self, job, kind='harness_error'):
        envelope=dict(version=1,attempt=job['id'],harness_status=kind,exit_code=1)
        if kind=='harness_error':envelope['harness_error']=fault('ImportError')
        if kind=='provider_error':envelope['provider_error']={'code':'unknown'}
        return dict(result=None,logs='',execution_envelope=envelope,
            execution=dict(id=job['container_id'],image_id=job['runner_config']['image'],exit_protocol='1',running=False,
                           exit_code=1,oom_killed=kind=='oom'))

    def test_fault_retries_after_qualified_image_and_keeps_history(self):
        failed=self.failed_build(); investigation=self.reserve_recovery(failed)
        with patch.object(self.runner,'capture',side_effect=self.capture_fault):
            answer=self.finish_recovery(investigation)
        self.assertFalse(answer['charged']);self.assertEqual(self.budget(),1)
        self.assertEqual(self.recovery.finish(investigation)['outcome'],'deferred')
        self.blocked(failed)
        config=Configuration(self.store,clock=lambda:self.now)
        replacement='sha256:'+'2'*64
        config.replace('runtime.json',{'image':replacement},expected_revision=0)
        config.replace('host.json',{'image':replacement,'updated_at':self.now},expected_revision=0)
        self.assertTrue(ExecutionProblems(self.store,clock=lambda:self.now).recheck(self.command(),answer['gate_id'])['resolved'])
        self.config['image']=replacement;self.recovery=self.driver()
        retried=self.reserve_recovery(failed)
        self.assertNotEqual(retried,investigation)
        self.assertTrue(self.finish_recovery(retried)['accepted'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_assignments').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT count(*) FROM recovery_findings').fetchone()[0],1)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        self.assertEqual(self.budget(),1)

    def test_provider_cooldown_allows_new_physical_investigation_once_due(self):
        from forge.factory.wait_controller import WaitController
        failed=self.failed_build(); investigation=self.reserve_recovery(failed)
        with patch.object(self.runner,'capture',side_effect=lambda job:self.capture_fault(job,'provider_error')):
            answer=self.finish_recovery(investigation)
        self.assertFalse(answer['charged']);self.assertEqual(self.budget(),1)
        waits=WaitController(self.store,clock=lambda:self.now)
        self.assertIn('pending',waits.step(answer['gate_id']))
        self.now+=31
        self.assertTrue(waits.step(answer['gate_id'])['resolved'])
        retried=self.reserve_recovery(failed)
        self.assertNotEqual(retried,investigation)
        self.blocked(failed)

    def test_recovery_timeout_charges_once_without_recursive_investigation(self):
        failed=self.failed_build();investigation=self.reserve_recovery(failed)
        with patch.object(self.runner,'capture',side_effect=lambda job:self.capture_fault(job,'timeout')):
            answer=self.finish_recovery(investigation)
        self.assertTrue(answer['charged']);self.assertEqual(self.budget(),2)
        self.assertEqual(self.recovery.finish(investigation)['outcome'],'investigated')
        self.assertEqual(self.budget(),2)

    def test_oom_recovery_increases_allocation_without_charge_or_loop(self):
        self.config['memory_tiers']=['2g','4g'];self.config['memory']='2g';self.recovery=self.driver()
        failed=self.failed_build(); investigation=self.reserve_recovery(failed)
        with patch.object(self.runner,'capture',side_effect=lambda job:self.capture_fault(job,'oom')):
            answer=self.finish_recovery(investigation)
        self.assertEqual(answer['memory_retry'],'4g');self.assertEqual(self.budget(),1)
        retried=self.reserve_recovery(failed)
        self.assertEqual(self.jobs.job(retried)['runner_config']['memory'],'4g')
        with patch.object(self.runner,'capture',side_effect=lambda job:self.capture_fault(job,'oom')):
            exhausted=self.finish_recovery(retried)
        self.assertFalse(exhausted['charged']);self.assertNotIn('memory_retry',exhausted)
        self.blocked(failed)
        self.assertEqual(self.budget(),1)
        self.config['memory_tiers']=['2g','4g','8g'];self.recovery=self.driver()
        repaired=self.reserve_recovery(failed)
        self.assertEqual(self.jobs.job(repaired)['runner_config']['memory'],'8g')


class RecoveryHistoryMigrationTests(unittest.TestCase):
    def test_035_upgrade_preserves_nonempty_investigation_findings_and_foreign_keys(self):
        from forge.store import db as storage, upgrade
        migrations=storage._migrations()
        import tempfile
        from tests.historical_store_fixtures import investigation_035
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        with patch('forge.store.db._migrations',return_value=migrations[:35]):
            historical=storage.Store.create(directory.name)
            investigation_035(historical)
            with historical.read_snapshot() as db:
                before={table:[tuple(row) for row in db.execute('SELECT * FROM '+table)]
                    for table in ('recovery_assignments','recovery_findings','attempt_failures','budget_entries')}
                self.assertTrue(all(before.values()))
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
        upgraded=storage.Store(historical.root);upgrade.apply(upgraded)
        with upgraded.read_snapshot() as db:
            self.assertEqual({table:[tuple(row) for row in db.execute('SELECT * FROM '+table)] for table in before},before)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA foreign_keys').fetchone()[0],1)
