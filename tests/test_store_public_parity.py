from copy import deepcopy
import unittest

from forge.settings import DEFAULT
from forge.store.migration.importer import Importer
from forge.store.migration.public_parity import verify
from tests import test_store_import as fixtures


class PublicParityTests(unittest.TestCase):
    setUp=fixtures.ImportTests.setUp
    write=fixtures.ImportTests.write
    snapshot=fixtures.ImportTests.snapshot

    def prepared(self):
        config=deepcopy(DEFAULT);config['repositories']=[{'id':1,'name':'test/repo','enabled':True}]
        self.write('factory.json',config)
        source=self.snapshot();importer=Importer(source,self.base/'staging');importer.run()
        return source,importer.store

    def test_compares_public_projections_with_frozen_source(self):
        source,store=self.prepared()
        report=verify(source,store)
        self.assertEqual(report['status'],'passed',report)
        self.assertGreater(report['counts']['comparisons'],10)
        self.assertEqual(report['counts']['public_issues'],1)
        self.assertTrue(report['approved_differences'])

    def test_metadata_corruption_blocks_without_exposing_source_values(self):
        source,store=self.prepared()
        with store.write() as db:
            db.execute('DROP TRIGGER issue_revisions_no_update')
            db.execute("UPDATE issue_revisions SET title='secret-sentinel' WHERE issue_id='1:1'")
        report=verify(source,store)
        self.assertEqual(report['status'],'blocked')
        self.assertNotIn('secret-sentinel',str(report))
        self.assertTrue(any(row.get('field')=='title' for row in report['findings']),report)

    def test_imported_output_chunks_and_original_event_names_are_publicly_preserved(self):
        self.ledger['jobs']['build']=dict(id='build',issue_key='1:1',role='build',cleanup=True,fenced=True,
            slot='builder',created_at=11,phase='cleaned',outcome='failed',provider='codex',model='model',deadline=50,
            branch='work',target_branch='main',capture={'logs':'Long log \n with unicode ✓'*80000})
        self.ledger['events'].append({'sequence':2,'at':12,'kind':'runner_released','issue':'1:1','attempt':'build','reason':'Finished'})
        source,store=self.prepared();report=verify(source,store)
        self.assertEqual(report['status'],'passed',report)
        self.assertEqual(report['counts']['public_jobs'],1)

    def test_dependencies_related_pulls_and_collaborators_are_rendered(self):
        from forge.factory.contracts import digest
        from forge.factory.store_views import PipelineViews
        body='Depends on #7 and #8.'
        self.issue.update(declared_dependencies=[9],dependency_body=digest(body),requirements=digest(body))
        self.write('state/issues/inventory.json',{'repositories':{'1':{'name':'test/repo','at':20,'complete':True,'identity':digest({'name':'test/repo','connections':DEFAULT['connections']}),
            'issues':{'1':{'number':1,'title':'Change','body':body,'content_hash':digest(body),'body_truncated':False,'state':'open','state_reason':None,'updated_at':None,'labels':[],'assignees':[],'assignee_types':{}}},
            'pulls':[{'number':23,'title':'External fix','body':'Fixes #1','branch':'fix','author':'human','forge_owned':False},
                     {'number':24,'title':'Old attempt','body':'','branch':'forge/issue-1-old','author':'forge','forge_owned':True}],
            'collaborators':{'at':20,'rows':[{'login':'Alice','name':'Alice Example'}],'authors':['alice','Bob']}}}})
        source,store=self.prepared()
        row=PipelineViews(store,clock=lambda:20).issues(key='1:1')['issue']
        self.assertEqual(row['dependencies'],[7,8,9])
        self.assertEqual({r['number']:r['kind'] for r in row['pull_requests']},{23:'external',24:'forge_attempt'})
        self.assertEqual(row['reviewers'],[{'login':'Alice','name':'Alice Example','source':'collaborator'},{'login':'Bob','name':'','source':'recent_author'}])
        report=verify(source,store)
        self.assertEqual(report['status'],'passed',report)

    def test_completed_issue_keeps_historical_pull_link_without_current_authority(self):
        from forge.factory.store_views import PipelineViews
        self.issue['state']='merged'
        self.issue['environment_hold']={'detail':'Historical service failure','blocker':'services','state':'ready'}
        self.issue['candidate']=dict(pr=12,head='head',base='base',policy='policy',requirements='requirements-hash',key='identity',build_attempt='build',accepted_at=12,branch='work')
        self.issue['evidence']={'merge':{'pr':12,'sha':'commit'}}
        self.issue['github_observation']={'at':20,'issue':{'number':1,'title':'Change','body':'','state':'closed','state_reason':'completed','labels':[],'assignees':[]}}
        self.ledger['jobs']['build']=dict(id='build',issue_key='1:1',role='build',cleanup=True,fenced=True,slot='slot',created_at=11,phase='cleaned',outcome='accepted',branch='work',target_branch='main')
        source,store=self.prepared()
        row=PipelineViews(store,clock=lambda:20).issues(key='1:1')['issue']
        self.assertEqual(row['pull_requests'],[{'number':12,'title':'Forge pull request','kind':'candidate'}])
        self.assertIsNone(row['lifecycle']['candidate_id'])
        self.assertIsNone(row['lifecycle']['unit_id'])
        report=verify(source,store)
        self.assertEqual(report['status'],'passed',report)

    def test_decision_environment_worker_summary_and_hold_actors_are_preserved(self):
        from forge.factory.contracts import digest
        from forge.factory.store_views import PipelineViews
        self.issue.update(state='hold',decision_request={'question':'Choose the migration approach','options':['Copy','Rebuild']},last_summary={'headline':'Choose an approach','plain':'Both approaches need an operator decision.'},blocked_dependencies=[7],dependency_reason='Waiting for #7')
        facts={'number':1,'title':'Change','body':'','state':'open','labels':['hold'],'assignees':[],'content_hash':digest(''),'label_events':[{'label':'hold','login':'Alice','at':'2026-10-08T01:00:00Z'}]}
        self.write('state/issues/inventory.json',{'repositories':{'1':{'name':'test/repo','at':20,'complete':True,'identity':digest({'name':'test/repo','connections':DEFAULT['connections']}),'issues':{'1':facts}}}})
        source,store=self.prepared();views=PipelineViews(store,clock=lambda:20)
        detail=views.issues(key='1:1')['issue'];board=views.factory(issue='1:1')['issues'][0]
        self.assertEqual(detail['decision_request'],self.issue['decision_request'])
        self.assertEqual(detail['held_by'],facts['label_events'])
        self.assertEqual(board['last_summary'],self.issue['last_summary'])
        self.assertEqual(board['blocked_dependencies'],[7])
        report=verify(source,store);self.assertEqual(report['status'],'passed',report)
