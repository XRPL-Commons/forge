from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.factory.probe_runner import ProbeRunner, PROVIDER
from forge.factory.runner import ContainerRunner, RunnerError
from forge.setup_service import Service
from forge.sqlite_onboarding import SetupState
from forge.store.db import Store
from forge.store.migration.probe_cleanup import cleanup_fenced_probe
from tests.test_worker_boundary import Engine


class UpgradeProbeCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.store=Store.create(self.root/'database')
        self.state=SetupState(self.root,self.store,lambda:100)
        self.config=dict(image='sha256:'+'a'*64,network='forge-workers',harness_argv=PROVIDER,
                         grace_seconds=2,provider='codex')
        self.job=Service.new_probe(None,self.config,'provider')
        self.operation=dict(id='existing-operation',action='harness_check',step='probe')
        with self.state.transaction() as data:
            data.update(generation=1,phase='setup',probe_jobs=[self.job],operation=self.operation,
                        error='auxiliary execution inputs changed')
        self.engine=Engine()
        self.runner=ProbeRunner(self.config,environ={'FORGE_CREDENTIAL_ROOT':str(self.root)})
        self.runner.call=self.engine

    def fence(self):
        with self.state.transaction() as data:
            data['probe_jobs'][0].update(fenced=True,capacity_wait_since=101)

    def test_existing_fenced_manifest_cleans_and_commits_without_losing_history(self):
        spec=self.runner.prepare_source(self.job)
        self.fence()
        with patch('forge.factory.probe_runner.ProbeRunner',return_value=self.runner), \
                patch.object(ContainerRunner,'reset',return_value={'container_absent':True}):
            self.assertEqual(cleanup_fenced_probe(self.root,self.store),1)
        data=self.state.read()
        self.assertEqual(data['operation'],self.operation)
        self.assertEqual(data['error'],'auxiliary execution inputs changed')
        self.assertTrue(data['probe_jobs'][0]['cleanup'])
        self.assertNotIn('execution_fence',data['probe_jobs'][0])
        self.assertNotIn(spec.volume,self.engine.resources['volume'])
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auxiliary_leases').fetchone()[0],0)
        self.assertEqual(cleanup_fenced_probe(self.root,self.store),0)

    def test_active_probe_remains_owned_and_is_never_reset(self):
        before=deepcopy(self.state.read())
        with patch('forge.factory.probe_runner.ProbeRunner') as runner:
            self.assertEqual(cleanup_fenced_probe(self.root,self.store),0)
            runner.assert_not_called()
        self.assertEqual(self.state.read(),before)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auxiliary_leases').fetchone()[0],1)

    def test_failed_reset_keeps_cleanup_and_lease_authority(self):
        self.fence()
        before=deepcopy(self.state.read())
        with patch('forge.factory.probe_runner.ProbeRunner') as runner:
            runner.return_value.reset.side_effect=RunnerError('unconfirmed cleanup')
            with self.assertRaises(RunnerError):cleanup_fenced_probe(self.root,self.store)
        self.assertEqual(self.state.read(),before)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM auxiliary_leases').fetchone()[0],1)

    def test_older_schema_never_enters_current_setup_adapter(self):
        with patch('forge.store.migration.probe_cleanup.schema.inspect',return_value={'current_schema':39,'target_schema':40}), \
                patch('forge.store.db.Store.open',side_effect=AssertionError('older schema')):
            self.assertEqual(cleanup_fenced_probe(self.root,self.store),0)
