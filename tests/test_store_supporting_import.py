import unittest

from forge.store.migration.importer import Importer
from forge.store.migration.snapshot import MigrationError
from forge.store.supporting_queries import SupportingQueries
from tests import test_store_import as fixtures


class SupportingImportTests(unittest.TestCase):
    setUp = fixtures.ImportTests.setUp
    write = fixtures.ImportTests.write
    snapshot = fixtures.ImportTests.snapshot

    def sources(self):
        self.write('state/issues/inventory.json', {'repositories':{'1':dict(name='test/repo',at=20,complete=False,
            refreshing=True,identity='installation',error='incomplete',issues={},
            collaborators={'at':19,'rows':[{'login':'operator','name':'Operator'}],'authors':['author']},
            pulls=[dict(number=2,title='PR',body='Closes #1',author='author',branch='work',forge_owned=True)])}})
        self.write('state/issues/discussions.json', {'1:1':dict(status='idle',revision='opaque-revision',
            requested_at=12,cancel_requested=False,reason=None,partial='',origin={'provider':'test'},
            presence={'phase':'complete'},messages=[dict(id='m'+str(n),sequence=n,role='user' if n%2 else 'assistant',
            text='message '+str(n),at=10+n,proposal={'action':'pause'} if n==4 else None) for n in range(1,6)],
            job=dict(id='discussion-job',cleanup=True,fenced=True,completed=16,context={'large':'evidence'*200000}))})
        self.action=dict(action='pause',created_at=14,key='1:1',number=1,repo='test/repo',repo_id=1,
                         revision='opaque-revision',status='applied',reason='Pause here',message='Applied',plan={'add':['hold']})
        self.write('state/issues/actions.json', {'decision':self.action})
        self.notice=dict(id='notice',message='An event',created_at=15,channels={
            'ntfy':dict(status='accepted_by_relay',attempts=2,next_at=35,last_attempt_at=20),
            'relay':dict(status='retry',attempts=1,next_at=50,last_attempt_at=20),
            'gchat':dict(status='disabled',attempts=1,next_at=50,last_attempt_at=20)})
        self.write('state/notifications/outbox/notice.json',self.notice)

    def test_rows_preserve_messages_decisions_receipts_and_partial_inventory(self):
        self.sources()
        importer=Importer(self.snapshot(),self.base/'staging',batch=1)
        report=importer.run()
        queries=SupportingQueries(importer.store)
        self.assertEqual(queries.action('decision'),self.action)
        self.assertEqual(queries.notification('notice'),self.notice)
        self.assertEqual([m['sequence'] for m in queries.messages('1:1',before=4,limit=2)['messages']],[2,3])
        self.assertEqual(report['supporting_checks']['messages'],5)
        self.assertEqual(report['supporting_checks']['discussion_pages'],3)
        self.assertEqual(report['supporting_checks']['discussion_artifacts'],2)
        with importer.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM notification_deliveries').fetchone()[0],3)
            self.assertEqual(db.execute("SELECT complete FROM repository_observations WHERE kind='inventory'").fetchone()[0],0)
            self.assertIsNone(db.execute('SELECT source_created_at FROM discussion_executions').fetchone()[0])

    def test_supporting_batch_resume_is_exactly_once(self):
        self.sources()
        source=self.snapshot()
        def fail(name, end):
            if name=='normalized:discussions':
                raise RuntimeError('process ended after commit')
        with self.assertRaises(RuntimeError):
            Importer(source,self.base/'staging',batch=1,after_batch=fail).run()
        importer=Importer(source,self.base/'staging',batch=1)
        report=importer.run()
        self.assertEqual(report['supporting_checks']['messages'],5)
        self.assertEqual(importer.run()['supporting_checks'],report['supporting_checks'])
        with importer.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],5)

    def test_parity_detects_changed_delivery_and_conversation_facts(self):
        self.sources()
        importer=Importer(self.snapshot(),self.base/'staging')
        importer.run()
        with importer.store.write() as db:
            db.execute("UPDATE notification_outbox SET delivery_state='pending' WHERE channel='relay'")
        with self.assertRaisesRegex(MigrationError,'notification_parity_mismatch'):
            importer.validate()
        with importer.store.write() as db:
            db.execute("UPDATE notification_outbox SET delivery_state='retry' WHERE channel='relay'")
            db.execute("UPDATE messages SET content='changed' WHERE sequence=1")
        with self.assertRaisesRegex(MigrationError,'discussion_messages_parity_mismatch'):
            importer.validate()

    def test_unknown_entity_field_is_an_explicit_activation_blocker(self):
        self.sources()
        self.notice['future_delivery_policy']='unknown'
        self.write('state/notifications/outbox/notice.json',self.notice)
        importer=Importer(self.snapshot(),self.base/'staging')
        # Unknown data is archived but cannot qualify as equivalent public state.
        with self.assertRaisesRegex(MigrationError,'notification_parity_mismatch'):
            importer.run()
        with importer.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT blocking FROM import_findings WHERE code='unmapped_entity_fields'").fetchone()[0],1)

    def test_configured_repository_without_issues_is_retained(self):
        self.write('factory.json',{'repositories':[{'id':1,'name':'test/repo','enabled':True},
                                                  {'id':2,'name':'test/disabled','enabled':False}]})
        importer=Importer(self.snapshot(),self.base/'staging');importer.run()
        with importer.store.read_snapshot() as db:
            self.assertEqual(tuple(db.execute('SELECT full_name,enabled FROM repositories WHERE id=2').fetchone()),('test/disabled',0))

    def test_message_pages_do_not_read_execution_artifacts_or_archives(self):
        self.sources();importer=Importer(self.snapshot(),self.base/'staging');importer.run()
        from unittest.mock import patch
        from contextlib import contextmanager
        queries=[]
        original=importer.store.read_snapshot
        @contextmanager
        def traced():
            with original() as db:
                db.set_trace_callback(queries.append)
                yield db
        with patch.object(importer.store,'read_snapshot',traced):
            SupportingQueries(importer.store).messages('1:1',limit=2)
        self.assertFalse(any('artifacts' in q or 'import_records' in q for q in queries))


if __name__ == '__main__':
    unittest.main()
