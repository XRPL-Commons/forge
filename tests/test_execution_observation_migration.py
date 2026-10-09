"""A populated schema25 upgrade cannot invent evidence that an engine was read."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.store import db as storage, upgrade
from forge.store.executions import Executions, Launch
from forge.store.models import Command
from forge.factory.store_views import PipelineViews


class ExecutionObservationMigrationTests(unittest.TestCase):
    def test_populated_upgrade_preserves_lease_and_does_not_promote_import_or_capture_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            migrations = storage._migrations()
            with patch('forge.store.db._migrations', return_value=migrations[:25]):
                store = storage.Store.create(Path(temporary)/'store')
                commands = Executions(store, clock=lambda: 100)
                admitted = commands.admit_issue(request_id='admit', actor_id='operator', unit_id='u', issue_id='i',
                    repository_id=1, repository_name='owner/repo', issue_number=1, title='Issue', body='Body',
                    requirements={}, observed_at=90)
                result = commands.reserve_job(Command('reserve','operator','u',admitted['version']), 'build',
                    slot='build-1', execution_fence='fence', member_id=admitted['member_id'],
                    input_manifest={'requirements_revision_id':admitted['revision_id'],'base_oid':'base','policy_hash':'policy'},
                    launch=Launch('work','builder[bot]','codex',200,{},{}))
                job = result['job_id']
                with store.write() as db:
                    db.execute("UPDATE jobs SET phase='running' WHERE id=?", (job,))
                    db.execute('UPDATE job_execution_state SET container_id=?,captured_at=? WHERE job_id=?', ('container',99,job))
                    db.execute('INSERT INTO job_public_metadata(job_id,started_at,observed_at) VALUES(?,?,?)', (job,80,95))
            upgrade.apply(store)
            upgrade.apply(store)
            view = PipelineViews(store).job(job)
            self.assertIsNone(view['observed_at'])
            self.assertIsNone(view['observed_status'])
            self.assertIsNone(view['started_at'])
            with store.read_snapshot() as db:
                self.assertEqual(tuple(db.execute('SELECT slot,execution_fence FROM job_leases').fetchone()), ('build-1','fence'))
                self.assertEqual(db.execute('SELECT captured_at FROM job_execution_state').fetchone()[0], 99)
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
