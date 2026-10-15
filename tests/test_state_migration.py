"""Forward phase migration preserves captures and delivery history."""
import json
import unittest
from unittest.mock import patch

from forge.store import db as storage, upgrade
from forge.store.queries import Queries
from forge.store.transitions import project_unit
from tests import test_publications as publication_fixture


class StateMigrationTests(unittest.TestCase):
    def test_populated_schema_three_maps_pending_publication_without_rewriting_history(self):
        self.check_migration('pending')

    def test_populated_schema_three_keeps_uncertain_publication_for_readback(self):
        self.check_migration('uncertain')

    def check_migration(self, publication_status):
        def old_unit(query,unit_id):
            # Schema 3 predates the unified-outbox execution-condition query.
            with query.store.read_snapshot() as db:return project_unit(db,unit_id)
        with patch('forge.store.db._migrations',return_value=storage._migrations()[:3]), patch.object(Queries,'unit',old_unit):
            fixture=publication_fixture.PublicationLedgerTests()
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        old=fixture.store
        history_tables=('events','decisions','attempts','jobs','job_members','unit_issues',
                        'issue_revisions','job_artifacts','publication_intents','publication_steps')
        with old.write() as db:
            for phase in ('queued','integrating','dissolved'):
                db.execute('INSERT INTO units(id,repository_id,base_branch,state,created_at,updated_at) '
                    "VALUES(?,1,'main',?,50,50)",(phase,phase))
                db.execute('INSERT INTO events(transaction_id,unit_id,kind,at,actor_id,payload_json) '
                    "VALUES(?,?,'phase_changed',50,'historical',?)",
                    ('event-'+phase,phase,json.dumps({'before':'queued','after':phase})))
            db.execute('INSERT INTO publication_intents(id,job_id,unit_id,execution_fence,input_manifest_json,'
                'output_digest,output_json,target_json,report_json,status,created_at,updated_at) '
                "VALUES('publication',?,'u',?,?,?,?,?,?,?,90,100)",
                (fixture.job,fixture.launch['execution_fence'],json.dumps(fixture.fixture.manifest),
                 fixture.output.digest(),json.dumps(fixture.output.identity()),json.dumps(fixture.target),json.dumps(fixture.report),publication_status))
            db.execute('INSERT INTO publication_steps(intent_id,key,request_digest,status,attempts,uncertain_since,created_at,updated_at) '
                "VALUES('publication','commit',?,'sending',1,90,90,100)",('c'*64,))
            history={table:[tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')]
                     for table in history_tables}
            before_unit=dict(db.execute("SELECT * FROM units WHERE id='u'").fetchone())
        target=storage.Store(old.root)
        result=upgrade.apply(target)
        self.assertEqual(result['current_schema'],4)
        with target.read_snapshot() as db:
            self.assertEqual([tuple(row) for row in db.execute('SELECT id,state,close_code FROM units ORDER BY id')],
                [('dissolved','closed','dissolved'),('integrating','building',None),('queued','building',None),('u','publishing',None)])
            after_unit=dict(db.execute("SELECT * FROM units WHERE id='u'").fetchone())
            self.assertEqual(after_unit,{**before_unit,'state':'publishing'})
            for table,rows in history.items():
                with self.subTest(table=table):
                    self.assertEqual([tuple(row) for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid')],rows)
            self.assertIsNone(db.execute("SELECT issue_outcome FROM close_codes WHERE code='dissolved'").fetchone()[0])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
        upgrade.apply(target)
        with target.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM publication_steps WHERE intent_id='publication'").fetchone()[0],1)
