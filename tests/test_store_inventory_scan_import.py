"""Large partial scans retain all facts without a repository-sized JSON row."""
from copy import deepcopy
from contextlib import nullcontext
import json
import unittest
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.contracts import digest
from forge.factory.store_inventory import StoreInventory
from forge.settings import DEFAULT
from forge.store.migration.importer import Importer
from forge.store.migration.public_parity import verify
from forge.store.migration.snapshot import MigrationError, encode
from forge.store.supporting_queries import SupportingQueries
from tests import test_store_import as fixtures


class InventoryScanImportTests(unittest.TestCase):
    setUp = fixtures.ImportTests.setUp
    write = fixtures.ImportTests.write
    snapshot = fixtures.ImportTests.snapshot

    def prepare(self):
        config = deepcopy(DEFAULT)
        config['repositories'] = [{'id':1, 'name':'test/repo', 'enabled':True}]
        self.write('factory.json', config)
        self.facts = {str(n):dict(number=n, title='Change' if n == 1 else 'Issue '+str(n),
            body='body '+str(n)+'x'*4096, content_hash=digest('body '+str(n)+'x'*4096), body_truncated=False,
            state='open', state_reason=None, updated_at=None, labels=[], assignees=[], assignee_types={})
            for n in range(1,61)}
        self.issue['requirements'] = self.facts['1']['content_hash']
        self.scan = dict(state='closed', page=7, issues=deepcopy(self.facts))
        # Preserve optional keys and absent versus explicit-null bodies too.
        self.scan['issues']['scan-only'] = {'number':99, 'title':'Scan-only diagnostic', 'extra':{'value':[1,2]}}
        self.scan['issues']['null-body'] = {'number':100, 'body':None}
        self.assertGreater(len(encode(self.scan)), 65536)
        self.identity = digest({'name':'test/repo', 'connections':config['connections']})
        self.write('state/issues/inventory.json', {'repositories':{'1':dict(name='test/repo', at=20,
            complete=False, refreshing=True, identity=self.identity, issues=self.facts, scan=self.scan)}})
        return self.snapshot(), config

    def assert_scan(self, importer):
        reader = SupportingQueries(importer.store)
        actual = {}; after = None; pages = 0
        while True:
            page = reader.inventory_scan('legacy:inventory:1', after=after, limit=13)
            self.assertEqual(page['header'], {'state':'closed','page':7})
            self.assertEqual(page['cursor'], {'state':'closed','page':7})
            self.assertTrue(page['present']); self.assertTrue(page['issues_present'])
            actual.update((row['key'],row['facts']) for row in page['items'])
            pages += 1; after = page['next_cursor']
            if after is None:
                break
        self.assertGreater(pages,1)
        self.assertEqual(actual,self.scan['issues'])
        with importer.store.read_snapshot() as db:
            row = db.execute("SELECT details_json FROM repository_observations WHERE id='legacy:inventory:1'").fetchone()
            self.assertLess(len(row[0].encode()),1024)
            self.assertNotIn('issues',json.loads(row[0])['scan'])

    def test_large_partial_scan_resumes_import_and_passes_public_parity(self):
        source, _ = self.prepare()
        def interrupt(name,end):
            if name == 'normalized:inventory_scan_issues' and end == 20:
                raise RuntimeError('after committed scan batch')
        with self.assertRaisesRegex(RuntimeError,'committed scan'):
            Importer(source,self.base/'staging',batch=20,after_batch=interrupt).run()
        importer = Importer(source,self.base/'staging',batch=20)
        report = importer.run()
        self.assertEqual(report['supporting_checks']['inventory_scan_issues'],62)
        self.assertEqual(report['supporting_checks']['inventory_scan_pages'],2)
        self.assert_scan(importer)
        self.assertEqual(importer.run()['supporting_checks'],report['supporting_checks'])
        parity = verify(source,importer.store)
        self.assertEqual(parity['status'],'passed',parity)
        with importer.store.write() as db:
            db.execute("UPDATE inventory_scan_issues SET body='changed' WHERE issue_key='1'")
        with self.assertRaisesRegex(MigrationError,'inventory_scan_parity_mismatch'):
            importer.validate()

    def test_inventory_refresh_resumes_captured_cursor_and_keeps_original_scan(self):
        source, config = self.prepare()
        importer = Importer(source,self.base/'staging'); importer.run()
        settings = Mock()
        settings.paused.return_value = False
        settings.locked.return_value = nullcontext()
        settings.load.return_value = config
        settings.view.return_value = {'repositories':[{'id':1,'name':'test/repo','enabled':True,'access':{'controller':'ready'}}]}
        github = Mock()
        github.deadline = float('inf')
        github.call.return_value = []
        github.pulls.return_value = []
        github.collaborators.return_value = []
        github.recent_authors.return_value = []
        with importer.store.write() as db:
            db.execute("UPDATE store_metadata SET value='false' WHERE key='import_only'")
        StoreInventory(importer.store,settings,github,config=validate({}),clock=lambda:100).refresh()
        github.call.assert_called_once_with('controller',{'repo':'test/repo','repo_id':1},'GET',
            '/issues?state=closed&sort=created&direction=asc&per_page=100&page=7')
        self.assert_scan(importer)
        with importer.store.read_snapshot() as db:
            row = db.execute("SELECT complete,cursor,details_json FROM repository_observations WHERE kind='inventory' ORDER BY collection_generation DESC LIMIT 1").fetchone()
            self.assertEqual(row['complete'],1)
            self.assertIsNone(row['cursor'])
            self.assertNotIn('scan',json.loads(row['details_json']))

    def test_absent_null_and_empty_scan_keep_distinct_headers(self):
        source, _ = self.prepare()
        importer = Importer(source,self.base/'staging'); importer.run()
        for index, (details, expected) in enumerate([({},(False,None,False)), ({'scan':None},(True,None,False)),
                                  ({'scan':{},'scan_issues_present':True},(True,{},True))]):
            with importer.store.write() as db:
                db.execute("INSERT INTO repository_observations(id,repository_id,kind,collection_generation,fetched_at,complete,refreshing,details_json) VALUES(?,1,'inventory',?,20,0,1,?)",('shape:'+str(index),index+2,json.dumps(details)))
            page = SupportingQueries(importer.store).inventory_scan('shape:'+str(index),limit=1)
            self.assertEqual((page['present'],page['header'],page['issues_present']),expected)
