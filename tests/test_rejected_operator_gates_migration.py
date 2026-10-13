"""Only historical rejection receipts lose their accidental execution hold."""
import json
import tempfile
import unittest
from unittest.mock import patch

from forge.store import db as storage, upgrade
from forge.store.models import Command
from forge.store.transitions import Lifecycle, project_unit


class RejectedOperatorGateMigrationTests(unittest.TestCase):
    def test_034_repair_preserves_actual_external_changes_holds_and_history(self):
        migrations=storage._migrations()
        with tempfile.TemporaryDirectory() as root:
            with patch('forge.store.db._migrations',return_value=migrations[:34]):
                source=storage.Store.create(root);life=Lifecycle(source,clock=lambda:100)
                for number,details in enumerate(({'request_id':'stale-request','reason':'requirements changed'},
                        {'reason':'candidate_head_changed'},{'request_id':None,'reason':'candidate_unavailable'}),1):
                    unit=f'u{number}'
                    life.admit_issue(request_id=f'admit{number}',actor_id='operator',unit_id=unit,issue_id=f'i{number}',
                        repository_id=1,repository_name='owner/repo',issue_number=number,title='Issue',body='Body',
                        requirements='required',observed_at=90)
                    with source.read_snapshot() as db:version=project_unit(db,unit)['version']
                    opened=life.open_gate(Command(f'gate{number}','operator',unit,version),'external_change',details=details)
                    if number==1:life.pause(Command('pause','operator',unit,opened['version']))
                with source.read_snapshot() as db:
                    before={table:[tuple(row) for row in db.execute(f'SELECT * FROM {table}')]
                            for table in ('issues','issue_revisions','unit_issues','budget_entries','decisions')}
                    generations={row['id']:row['fencing_generation'] for row in db.execute('SELECT * FROM units')}
                    event_count=db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
            store=storage.Store(root);receipt=upgrade.apply(store)
            with store.read_snapshot() as db:
                after={table:[tuple(row) for row in db.execute(f'SELECT * FROM {table}')] for table in before}
                self.assertEqual(after,before)
                gates={row['unit_id']:dict(row) for row in db.execute("SELECT * FROM unit_gates WHERE code='external_change'")}
                self.assertEqual(gates['u1']['resolution'],'superseded')
                self.assertEqual(json.loads(gates['u1']['details_json'])['request_id'],'stale-request')
                self.assertIsNone(gates['u2']['resolved_at']);self.assertIsNone(gates['u3']['resolved_at'])
                self.assertEqual(project_unit(db,'u1')['reason_code'],'operator_pause')
                self.assertEqual(project_unit(db,'u2')['reason_code'],'external_change')
                self.assertEqual({row['id']:row['fencing_generation'] for row in db.execute('SELECT * FROM units')},generations)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],event_count+1)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(),[])
            self.assertEqual(upgrade.apply(store),receipt)
            with store.read_snapshot() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM events').fetchone()[0],event_count+1)
