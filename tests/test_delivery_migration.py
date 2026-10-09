"""Schema 22 preserves original launch pins when current requirements changed."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.store import db as storage
from forge.store import upgrade


class DeliveryMigrationTests(unittest.TestCase):
    def test_upgrade_preserves_original_scope_and_lease_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            migrations=storage._migrations()
            with patch('forge.store.db._migrations',return_value=migrations[:21]):
                store=storage.Store.create(Path(temporary)/'store')
                with store.write() as db:
                    db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'example/repo')")
                    db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES('i',1,1,'open','new',1)")
                    for revision in ('old','new'):
                        db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES(?,'i','Issue','','body',?,'{}',1)",(revision,revision))
                    db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES('u',1,'main','building',1,1)")
                    db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) VALUES('m','u','i',1,'main','new','building',1)")
                    for role in ('build','review'):
                        member='m' if role=='build' else None
                        manifest=json.dumps({'requirements_revision_id':'old'}) if member else '{}'
                        db.execute("INSERT INTO attempts(id,unit_id,member_id,membership_generation,assignment_generation,input_manifest_json,budget_generation,created_at) VALUES(?,'u',?,1,1,?,1,2)",(role,member,manifest))
                        db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,created_at) VALUES(?,'u',?,?,?,'reserved',2)",(role,role,member,role))
                        db.execute("INSERT INTO job_leases VALUES(?,?,?,2)",(role,role,role))
                        db.execute("INSERT INTO job_launches(job_id,slot,execution_fence,requirements_revision_id,branch,publisher,provider,runner_config_json,policy_json,context_json,deadline) VALUES(?,?,?,'old','forge/issue','builder[bot]','codex','{}','{}','{}',500)",(role,role,role))
            upgrade.apply(store); upgrade.apply(store)
            with store.read_snapshot() as db:
                self.assertEqual([tuple(row) for row in db.execute('SELECT job_id,member_id,requirements_revision_id FROM job_members ORDER BY job_id')],
                    [('build','m','old'),('review','m','old')])
                self.assertEqual(db.execute('SELECT COUNT(*) FROM job_leases').fetchone()[0],2)
                self.assertNotIn('requirements_revision_id',[row[1] for row in db.execute('PRAGMA table_info(job_launches)')])
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
