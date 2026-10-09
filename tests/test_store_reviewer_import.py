import unittest
from forge.store.migration.importer import Importer
from forge.store.operator_views import reviewer_request_in
from tests import test_store_import as fixtures


class ReviewerImportTests(unittest.TestCase):
    setUp=fixtures.ImportTests.setUp
    write=fixtures.ImportTests.write
    snapshot=fixtures.ImportTests.snapshot

    def test_current_candidate_reviewer_assignment_preserved_with_import_parity(self):
        self.issue['state']='approval_needed'
        self.issue['candidate']=dict(pr=12,head='head',base='base',policy='policy',requirements='requirements-hash',
            key='identity',build_attempt='build',accepted_at=12,branch='work')
        self.issue['reviewers_requested']={'logins':['reviewer'],'at':15,'request':'old-request'}
        self.ledger['jobs']['build']=dict(id='build',issue_key='1:1',role='build',cleanup=True,fenced=True,
            slot='slot',created_at=11,phase='cleaned',outcome='accepted',branch='work',target_branch='main')
        importer=Importer(self.snapshot(),self.base/'staging');report=importer.run()
        self.assertEqual(report['source_parity_mismatches'],0)
        with importer.store.read_snapshot() as db:
            receipt=reviewer_request_in(db,'legacy:candidate:identity')
        self.assertEqual(receipt,{'candidate_id':'legacy:candidate:identity','logins':['reviewer'],'at':15,'request':'old-request','issue_assignment':True})
