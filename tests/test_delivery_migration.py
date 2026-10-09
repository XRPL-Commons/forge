"""Delivery upgrades preserve launch pins, leases, and issue environment intent."""
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
                    choice=json.dumps({'tier':'full','services':['postgres']})
                    override=json.dumps({'tier':'full','actor':'operator'})
                    retry=json.dumps({'role':'build','services':['postgres']})
                    memory=json.dumps({'role':'build','memory':'8g'})
                    hold=json.dumps({'reason':'service_missing'})
                    db.execute('INSERT INTO unit_environment_observations VALUES(?,?,?,?,?,?,?)',
                        ('u',choice,override,retry,memory,'env:full',hold))
                    db.execute("INSERT INTO unit_environment(unit_id,revision,choice,override,created_at,requirements_revision_id) VALUES('u',1,?,?,3,'new')",(choice,override))
                    db.execute("INSERT INTO dependency_checks VALUES('check','u','m','new',1,1,?,3)",
                        (json.dumps([{'number':7,'state':'closed','updated_at':'2026-10-09T00:00:00Z'}]),))
                    db.execute("INSERT INTO unit_gates(id,unit_id,member_id,subject_key,code,details_json,created_at) VALUES('gate','u','m','member:m','dependency',?,3)",
                        (json.dumps({'source':'runtime_dependencies','blocked':[7]}),))
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
                migrated=db.execute("SELECT * FROM issue_environment_observations WHERE issue_id='i'").fetchone()
                self.assertEqual(tuple(migrated),('i',choice,override,retry,memory,'env:full',hold))
                self.assertEqual(tuple(db.execute('SELECT member_id,requirements_revision_id,choice FROM unit_environment').fetchone()),('m','new',choice))
                self.assertEqual(db.execute('SELECT choice_json FROM unit_environment_observations').fetchone()[0],choice)
                self.assertNotIn('requirements_revision_id',[row[1] for row in db.execute('PRAGMA table_info(job_launches)')])
                checks=db.execute('SELECT generation,clear,facts_json FROM dependency_checks ORDER BY generation').fetchall()
                self.assertEqual(len(checks),2)
                self.assertEqual(json.loads(checks[0]['facts_json'])[0]['state'],'closed')
                self.assertEqual((checks[1]['generation'],checks[1]['clear']),(2,0))
                self.assertEqual(json.loads(checks[1]['facts_json']),[{'reference':'example/repo#7','error':'prerequisite_unobserved'}])
                gate=json.loads(db.execute("SELECT details_json FROM unit_gates WHERE id='gate'").fetchone()[0])
                self.assertEqual(gate['blocked'],[{'reference':'example/repo#7','reason':'prerequisite_unobserved'}])
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())
