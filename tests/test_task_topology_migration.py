"""Populated task migration preserves v1 identities, pins, output and gate lineage."""
import json
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from forge.store import db as storage,upgrade
from forge.store.transitions import manifest_hash, _json


class TaskMigrationTests(unittest.TestCase):
    def test_v1_reused_predecessor_and_unresolved_capture_preserve_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            catalog=storage._migrations()
            with patch('forge.store.db._migrations',return_value=catalog[:32]):
                store=storage.Store.create(Path(temporary)/'store')
                with store.write() as db:
                    db.execute("INSERT INTO repositories(id,full_name) VALUES(1,'example/repo')")
                    db.execute("INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) VALUES('u',1,'main','building',1,1)")
                    for number,member in enumerate(('a','b'),1):
                        db.execute("INSERT INTO issues(id,repository_id,github_number,github_state,current_revision_id,observed_at) VALUES(?,1,?,'open',?,1)",(member,number,'rev-'+member))
                        db.execute("INSERT INTO issue_revisions(id,issue_id,title,body,content_hash,requirements_hash,requirements_json,observed_at) VALUES(?,?,'Issue','','body',?,'{}',1)",('rev-'+member,member,'req-'+member))
                        db.execute("INSERT INTO unit_issues(id,unit_id,issue_id,repository_id,base_branch,requirements_revision_id,state,joined_at) VALUES(?,'u',?,1,'main',?,'building',1)",(member,member,'rev-'+member))
                    db.execute("INSERT INTO unit_builders VALUES('builder','u','operator',1)")
                    db.execute("INSERT INTO member_dependencies VALUES('u','b','a',1)")
                    pins=[{'member_id':m,'requirements_revision_id':'rev-'+m} for m in ('a','b')]
                    ref={'output_id':'out-a','member_id':'a','commit_oid':'a'*40,'input_manifest_hash':'child-manifest'}
                    for index,member in enumerate(('a','b'),1):
                        parent='parent-'+member;plan='plan-'+member;child='child-'+member
                        manifest={'members':pins,'base_oid':'base','policy_hash':'policy','requirements_hash':'requirements',
                            'accepted_outputs':[] if member=='a' else [ref]}
                        encoded=json.dumps(manifest)
                        db.execute("INSERT INTO attempts VALUES(?,'u',NULL,1,1,?,1,?)",(parent,encoded,index))
                        db.execute("INSERT INTO jobs(id,unit_id,attempt_id,role,phase,outcome,fenced,cleanup,created_at) VALUES(?,'u',?,'build','cleaned','planned',1,1,?)",(parent,parent,index))
                        db.execute("INSERT INTO builder_assignments VALUES(?,'builder','planning')",(parent,))
                        value={'children':[{'key':member,'member_id':member,'scope':'Implement '+member,'dependencies':[] if member=='a' else ['a']}]}
                        db.execute('INSERT INTO delegation_plans VALUES(?,?,?,?,?,?)',(plan,'builder',parent,manifest_hash(manifest),json.dumps(value),index))
                        child_manifest={'requirements_revision_id':'rev-'+member,'base_oid':'base','policy_hash':'policy',
                            'prerequisite_outputs':[] if member=='a' else [ref],'delegation_plan_id':plan,'delegation_key':member,'scope':'Implement '+member}
                        db.execute("INSERT INTO attempts VALUES(?,'u',?,1,1,?,1,?)",(child,member,_json(child_manifest),index))
                        db.execute("INSERT INTO jobs(id,unit_id,attempt_id,member_id,role,phase,outcome,fenced,cleanup,created_at) VALUES(?,'u',?,?,'build',?,?,?,?,?)",(child,child,member,'cleaned' if member=='a' else 'reserved','success' if member=='a' else None,int(member=='a'),int(member=='a'),index))
                        db.execute('INSERT INTO job_delegations VALUES(?,?,?,?)',(child,plan,member,'tree-'+member))
                        if member=='b':
                            # A real cleaned, authorized capture awaiting its first result transition.
                            db.execute("INSERT INTO job_leases VALUES('slot',?,'fence',2)",(child,))
                            db.execute("INSERT INTO job_launches(job_id,slot,execution_fence,branch,publisher,provider,runner_config_json,policy_json,context_json,deadline) VALUES(?,'slot','fence','forge/work','builder[bot]','codex','{}','{}','{}',1000)",(child,))
                            db.execute('INSERT INTO job_execution_state(job_id,captured_at,capture_authorized) VALUES(?,2,1)',(child,))
                            db.execute("UPDATE jobs SET phase='cleaned',fenced=1,cleanup=1 WHERE id=?",(child,))
                            db.execute('DELETE FROM job_leases WHERE job_id=?',(child,))
                            bundle=b'verified historical bundle'; digest=hashlib.sha256(bundle).hexdigest()
                            capture=_json({'bundle':{'digest':digest,'size':len(bundle),'chunks':1}}).encode()
                            for kind,content in [('worktree_bundle',bundle),('worker_capture',capture)]:
                                db.execute('INSERT INTO job_artifacts(job_id,kind,sequence,media_type,digest,content,size) VALUES(?,?,0,?,?,?,?)',(child,kind,'application/octet-stream',hashlib.sha256(content).hexdigest(),content,len(content)))
                        if member=='a':
                            db.execute("INSERT INTO part_outputs VALUES('out-a','u','a','child-a','rev-a',1,?,'base','child-manifest',1)",('a'*40,))
                    db.execute("INSERT INTO unit_gates(id,unit_id,member_id,subject_key,code,details_json,created_at) VALUES('result-b','u','b','member:b','result_pending',?,2)",(json.dumps({'job_id':'child-b'}),))
                    db.execute("UPDATE units SET primary_gate_id='result-b' WHERE id='u'")
                    db.execute("INSERT INTO budget_entries(id,attempt_id,kind,reason_code,created_at) VALUES('charge','child-a','charge','execution_failure',1)")
                    before={row['id']:row['input_manifest_json'] for row in db.execute('SELECT id,input_manifest_json FROM attempts')}
            target=storage.Store(store.root)
            upgrade.apply(target);upgrade.apply(target)
            with target.read_snapshot() as db:
                self.assertEqual({row['id']:row['input_manifest_json'] for row in db.execute('SELECT id,input_manifest_json FROM attempts')},before)
                self.assertEqual(tuple(db.execute("SELECT id,attempt_id,commit_oid,input_manifest_hash,task_id FROM part_outputs WHERE id='out-a'").fetchone()),('out-a','child-a','a'*40,'child-manifest','v1:plan-a:a'))
                self.assertEqual([tuple(row) for row in db.execute('SELECT task_id,prerequisite_task_id FROM delivery_task_dependencies')],[('v1:plan-b:b','v1:plan-a:a')])
                self.assertEqual({row[0] for row in db.execute("SELECT task_id FROM delegation_plan_tasks WHERE plan_id='plan-b'")},{'v1:plan-a:a','v1:plan-b:b'})
                self.assertEqual(tuple(db.execute("SELECT job_id,subject_key,code FROM unit_gates WHERE resolved_at IS NULL").fetchone()),('child-b','job:child-b','result_pending'))
                self.assertEqual(db.execute("SELECT resolution FROM unit_gates WHERE id='result-b'").fetchone()[0],'superseded')
                self.assertEqual(db.execute('SELECT count(*) FROM budget_entries').fetchone()[0],1)
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def test_bad_reference_rolls_back_and_restores_fk_enforcement(self):
        from contextlib import contextmanager
        import hashlib
        with tempfile.TemporaryDirectory() as temporary:
            catalog=storage._migrations()
            with patch('forge.store.db._migrations',return_value=catalog[:32]):
                original=storage.Store.create(Path(temporary)/'store')
            script=catalog[32][2]+"\nCREATE TABLE invalid_reference(value TEXT REFERENCES units(id));\nINSERT INTO invalid_reference VALUES('missing');\n"
            changed=catalog[:32]+[(33,hashlib.sha256(script.encode()).hexdigest(),script)]
            with patch('forge.store.db._migrations',return_value=changed): target=storage.Store(original.root)
            connection=upgrade.connection; restored=[]
            @contextmanager
            def observed(*args,**kwargs):
                with connection(*args,**kwargs) as db:
                    try: yield db
                    finally: restored.append(db.execute('PRAGMA foreign_keys').fetchone()[0])
            with patch.object(upgrade,'connection',observed):
                with self.assertRaisesRegex(storage.StoreError,'integrity validation'): upgrade.apply(target)
            self.assertEqual(restored,[1])
            with original.read_snapshot() as db:
                self.assertEqual(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],32)
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_schema WHERE name='invalid_reference'").fetchone())
                self.assertIsNone(db.execute("SELECT 1 FROM sqlite_schema WHERE name='delivery_tasks'").fetchone())
                self.assertIsNone(db.execute('PRAGMA foreign_key_check').fetchone())

    def test_unexpected_migration_exception_restores_connection_enforcement(self):
        from contextlib import contextmanager
        with tempfile.TemporaryDirectory() as temporary:
            catalog=storage._migrations()
            with patch('forge.store.db._migrations',return_value=catalog[:32]): original=storage.Store.create(Path(temporary)/'store')
            target=storage.Store(original.root); connection=upgrade.connection; restored=[]
            @contextmanager
            def observed(*args,**kwargs):
                with connection(*args,**kwargs) as db:
                    try: yield db
                    finally: restored.append((db.execute('PRAGMA foreign_keys').fetchone()[0],db.in_transaction))
            with patch.object(upgrade,'connection',observed),patch.object(storage,'_execute_script',side_effect=RuntimeError('interrupted')):
                with self.assertRaisesRegex(RuntimeError,'interrupted'): upgrade.apply(target)
            self.assertEqual(restored,[(1,False)])
            with original.read_snapshot() as db:self.assertEqual(db.execute('SELECT max(version) FROM schema_migrations').fetchone()[0],32)
