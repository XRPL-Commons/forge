"""Only fixed setup checks use the trusted network; their work remains bounded."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from forge.factory.probe_runner import ProbeRunner, PROVIDER
from forge.factory.runner import ContainerRunner, RunnerError
from forge.factory.worker_boundary import BoundaryError
from forge.setup_service import Service
from tests.test_worker_boundary import Engine


class ProbeRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = dict(image='sha256:'+'a'*64, network='forge-workers', harness_argv=PROVIDER,
                           grace_seconds=2, provider='codex')
        self.job = Service.new_probe(None, self.config, 'provider')
        self.runner = ProbeRunner(self.config, environ={'FORGE_CREDENTIAL_ROOT': self.temp.name})
        self.engine = Engine(); self.runner.call = self.engine

    def test_probe_uses_bounded_retained_work_and_immutable_input(self):
        spec = self.runner.prepare_source(self.job)
        self.assertEqual(spec.network, 'forge-workers')
        self.assertEqual(self.engine.resources['volume'][spec.volume]['Options']['o'],
                         'size=268435456,uid=1000,gid=1000,mode=0700,nosuid,nodev')
        keeper = self.engine.resources['container'][spec.frontend]
        self.assertEqual(set(keeper['NetworkSettings']['Networks']), {'none'})
        self.assertFalse(keeper['Mounts'][0]['RW'])
        path = self.runner.inputs(self.job) / 'assignment.json'
        self.assertEqual(path.stat().st_mode & 0o777, 0o444)
        self.assertEqual(json.loads(path.read_text())['harness_argv'], PROVIDER)
        self.assertEqual(self.runner.prepare_source(self.job), spec)
        self.config['model'] = 'changed'
        with self.assertRaisesRegex(RunnerError, 'assignment changed'):
            self.runner.prepare_source(self.job)


    def test_boundary_identity_does_not_change_durable_auxiliary_inputs(self):
        from forge.store.db import Store
        from forge.store.auxiliary import save_in, job_in
        store=Store.create(Path(self.temp.name)/'database')
        original=deepcopy(self.job)
        with store.write() as db:save_in(db,'setup','live',self.job,100)
        spec=self.runner.prepare_source(self.job)
        self.assertEqual(self.job,original)
        # Capacity deferral, followed by fencing and cleanup, must preserve the
        # original launch manifest even when it had no explicit fence field.
        self.job.update(capacity_wait_since=101,fenced=True)
        with store.write() as db:save_in(db,'setup','live',self.job,102)
        with patch.object(ContainerRunner,'reset',return_value={'container_absent':True}):
            self.runner.reset(self.job)
        self.job['cleanup']=True
        with store.write() as db:save_in(db,'setup','live',self.job,103)
        with store.read_snapshot() as db:
            saved=job_in(db,self.job['id'])
            self.assertTrue(saved['cleanup'])
            self.assertNotIn('execution_fence',saved)
            self.assertIsNone(db.execute('SELECT 1 FROM auxiliary_leases WHERE execution_id=?',(self.job['id'],)).fetchone())
        self.assertNotIn(spec.volume,self.engine.resources['volume'])
        self.assertNotIn(spec.frontend,self.engine.resources['container'])

    def test_ordinary_assignment_or_program_cannot_enter_trusted_network(self):
        for change in ({'slot':'ordinary'}, {'unit_id':'unit'}, {'repo':'owner/repo'}):
            with self.subTest(change=change), self.assertRaisesRegex(RunnerError, 'Only persisted'):
                self.runner.prepare_source({**self.job, **change})
        self.config['harness_argv'] = ['/bin/sh']
        with self.assertRaisesRegex(RunnerError, 'Only persisted'):
            self.runner.prepare_source(self.job)
        self.assertEqual(self.engine.calls, [])

    def test_provider_preparation_has_no_repository_authority(self):
        with patch.object(self.runner, '_prepare_credentials', return_value='ready') as credentials, \
                patch.object(self.runner, 'source', side_effect=AssertionError('source authority')):
            self.assertEqual(self.runner.prepare(self.job), 'ready')
            credentials.assert_called_once_with(self.job)
            self.assertIsNone(self.runner.source_record(self.job))
            with self.assertRaisesRegex(RunnerError, 'repository bundles'):
                self.runner.prepare(self.job, bundles=[b'git'])

    def test_cleanup_preserves_foreign_resources_then_removes_owned_inputs(self):
        spec = self.runner.prepare_source(self.job)
        volume = self.engine.resources['volume'][spec.volume]
        labels = deepcopy(volume['Labels'])
        volume['Labels']['forge.boundary'] = 'foreign'
        with patch.object(ContainerRunner, 'reset', return_value={'container_absent':True}):
            with self.assertRaises(BoundaryError): self.runner.reset(self.job)
            self.assertIn(spec.frontend, self.engine.resources['container'])
            volume['Labels'] = labels
            self.runner.reset(self.job)
        self.assertFalse(self.engine.resources['volume'])
        self.assertFalse(self.engine.resources['container'])
        self.assertFalse(self.runner.inputs(self.job).exists())
