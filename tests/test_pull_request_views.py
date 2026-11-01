"""PR-only work stays visible without issue aliases, including inactive waits."""
import json
import tempfile
import unittest
from unittest.mock import Mock, patch

from forge.factory.changes import Changes
from forge.factory.store_views import PipelineViews
from forge.store.models import Command
from tests import test_review_pulls as review_tests
from tests import test_review_pull_pipeline as pipeline_tests


class PullRequestViewsTests(unittest.TestCase):
    setUp = review_tests.ReviewPullTests.setUp
    observe = review_tests.ReviewPullTests.observe
    snapshot = review_tests.ReviewPullTests.snapshot
    reserve = review_tests.ReviewPullTests.reserve
    clear_policy_wait = review_tests.ReviewPullTests.clear_policy_wait

    def views(self):
        return PipelineViews(self.store, runtime=self.config, clock=lambda: self.now)

    def test_pending_without_job_has_identity_and_never_exposes_source_body(self):
        self.remote['body'] = 'private source body not needed for activity'
        admitted = self.observe()
        board = self.views().factory(section='board')
        self.assertEqual(board['issues'], [])
        row = board['pull_requests'][0]
        self.assertEqual((row['repo_id'],row['repo'],row['number']), (1,'owner/repo',7))
        self.assertEqual(row['candidate']['id'], admitted['candidate_id'])
        self.assertEqual(row['candidate']['head'], 'a'*40)
        self.assertEqual(row['status'], 'blocked')
        self.assertIn('Publishing the Forge check', row['reason'])
        self.assertIsNone(row['active_job_id'])
        self.assertNotIn('private source body', json.dumps(board))
        self.clear_policy_wait(admitted)
        self.assertEqual(self.views().pull_request(1,7)['pull_request']['status'], 'queued')

    def test_deferred_revision_remains_visible_without_a_worker_and_updates_feed(self):
        admitted = self.observe(); self.clear_policy_wait(admitted)
        feed = Changes(self.views()); before = feed.read(wait=0)['cursor']
        unit = self.snapshot(admitted['unit_id'])['unit']
        self.life.open_gate(Command('defer','supervisor',unit['id'],unit['version']), 'review_source', details={
            'source':'review_pull','reason':'review_unavailable','candidate_id':admitted['candidate_id'],
            'retry_at':self.now+300,'role':'audit','cause':'dependency_access','diagnostics':'private captured diagnostic'})
        changed = feed.read(since=before,wait=0)
        row = changed['board']['pull_requests'][0]
        self.assertEqual((row['status'],row['retry_at']), ('deferred', self.now+300))
        self.assertIsNone(row['active_job_id'])
        self.assertIn('Audit is deferred',row['reason'])
        self.assertIn('Dependency downloads',row['reason'])
        self.assertEqual(changed['issues'], {})
        detail = self.views().pull_request(1,7)
        self.assertEqual(detail['jobs'], [])
        self.assertNotIn('private captured diagnostic', json.dumps(detail))

    def test_current_candidate_binds_active_job_and_old_job_deep_link(self):
        admitted = self.observe(); job, _ = self.reserve(admitted)
        detail = self.views().pull_request(1,7,job=job['job_id'])
        self.assertTrue(detail['selected_job']['current_candidate'])
        self.assertEqual(detail['selected_job']['pull_request']['number'],7)
        self.assertIsNone(detail['selected_job']['issue_key'])
        self.assertEqual(detail['pull_request']['active_job_id'],job['job_id'])
        self.assertEqual(detail['pull_request']['status'],'queued')
        self.remote['head']['sha']='c'*40; self.now+=1; fresh=self.observe()
        detail=self.views().pull_request(1,7,job=job['job_id'])
        self.assertFalse(detail['selected_job']['current_candidate'])
        self.assertEqual(detail['selected_job']['candidate_id'],admitted['candidate_id'])
        self.assertEqual(detail['pull_request']['candidate']['id'],fresh['candidate_id'])
        self.assertIsNone(detail['pull_request']['active_job_id'])
        # Existing output handling accepts issue-less jobs; no new log reader.
        self.assertEqual(self.views().output(job['job_id']), {'logs':''})

    def test_disabled_repository_and_closed_work_stay_visible(self):
        self.observe()
        with self.store.write() as db:
            db.execute('UPDATE repositories SET enabled=0 WHERE id=1')
        detail=self.views().pull_request(1,7)
        self.assertFalse(detail['pull_request']['enabled'])
        self.assertEqual(detail['pull_request']['reason_code'],'repository_setup')
        self.remote.update(state='closed',merged=True); self.now+=1; self.observe()
        row=self.views().pull_request(1,7)['pull_request']
        self.assertEqual(row['status'],'completed')
        self.assertIn('Merged on GitHub',row['reason'])
        self.assertEqual(self.views().factory(section='completed')['pull_requests'][0]['unit_id'],row['unit_id'])
        self.assertEqual(self.views().factory(section='board')['pull_requests'][0]['unit_id'],row['unit_id'])

    def test_latest_unit_selected_after_retarget_and_wrong_pr_job_rejected(self):
        first=self.observe(); job,_=self.reserve(first)
        self.now+=1; self.remote['base']['ref']='release'; latest=self.observe()
        board=self.views().factory(section='board')['pull_requests']
        self.assertEqual([r['unit_id'] for r in board],[latest['unit_id']])
        detail=self.views().pull_request(1,7,job=job['job_id'])
        self.assertFalse(detail['selected_job']['current_candidate'])
        self.assertIn(job['job_id'],[row['id'] for row in detail['jobs']])
        self.remote['number']=8; self.observe()
        with self.assertRaises(KeyError):self.views().pull_request(1,8,job=job['job_id'])
        with self.assertRaises(KeyError):self.views().pull_request(2,7)
        for before in ('bad','nan:x','1:'):
            with self.assertRaises(ValueError):self.views().pull_request(1,7,before=before)

    def test_history_pages_across_retired_units_without_duplicates(self):
        jobs=[];previous=None
        for index in range(22):
            self.now+=1;self.remote['base']['ref']='target-'+str(index)
            admitted=self.observe()
            if previous:
                unit=self.snapshot(previous['unit_id'])['unit']
                self.life.verify_cleanup(Command('cleanup-'+str(index),'test',unit['id'],unit['version']),
                    jobs[-1],execution_fence='fence',evidence={'process_stopped':True,'resources_released':True})
            job,_=self.reserve(admitted);jobs.append(job['job_id']);previous=admitted
        page=self.views().pull_request(1,7)
        self.assertEqual(len(page['jobs']),20)
        self.assertIsNotNone(page['jobs_next'])
        older=self.views().pull_request(1,7,before=page['jobs_next'])
        self.assertIsNone(older['jobs_next'])
        self.assertEqual([row['id'] for row in page['jobs']+older['jobs']],list(reversed(jobs)))
        self.assertEqual(sum(row['current_candidate'] for row in page['jobs']+older['jobs']),1)

    def test_active_pager_is_bounded_and_cursor_keeps_equal_timestamp_rows(self):
        from forge.factory.pull_request_views import listing_in
        for number in range(1,6):
            self.remote['number']=number;self.observe()
        with self.store.read_snapshot() as db:
            first=listing_in(db,self.now,limit=2)
            second=listing_in(db,self.now,before=first['next'],limit=2)
            third=listing_in(db,self.now,before=second['next'],limit=2)
        self.assertEqual([len(page['pull_requests']) for page in (first,second,third)],[2,2,1])
        self.assertIsNone(third['next'])
        self.assertEqual(len({row['key'] for page in (first,second,third) for row in page['pull_requests']}),5)
        self.assertIsNone(self.views().factory(section='board')['pull_requests_next'])
        with self.assertRaises(ValueError):self.views().pull_requests(before='nan:x')

    def test_detail_reads_bound_job_history_and_no_worker_payloads(self):
        admitted=self.observe();self.reserve(admitted)
        seen=[]
        with self.store.read_session() as db:
            db.set_trace_callback(seen.append)
            detail=self.views().pull_request(1,7)
            db.set_trace_callback(None)
        self.assertTrue(any('LIMIT 21' in sql and 'FROM jobs WHERE unit_id=' in sql for sql in seen))
        self.assertNotIn('source_json',json.dumps(detail))
        self.assertNotIn('input_manifest',json.dumps(detail))
        self.assertNotIn('logs',detail['jobs'][0])


class PullRequestCompletedViewsTests(unittest.TestCase):
    setUp=pipeline_tests.ReviewPullPipelineTests.setUp
    admission_controller=pipeline_tests.ReviewPullPipelineTests.admission_controller
    discover=pipeline_tests.ReviewPullPipelineTests.discover
    driver=pipeline_tests.ReviewPullPipelineTests.driver
    cycle=pipeline_tests.ReviewPullPipelineTests.cycle
    complete=pipeline_tests.ReviewPullPipelineTests.complete
    successes=pipeline_tests.ReviewPullPipelineTests.successes

    def test_completed_review_and_audit_are_not_reported_as_waiting_for_scheduler(self):
        self.discover();self.complete()
        view=PipelineViews(self.store,runtime=self.config,clock=lambda:self.now).pull_request(1,7)
        self.assertEqual(view['pull_request']['status'],'completed')
        self.assertIn('Review and Audit passed',view['pull_request']['reason'])
        self.assertEqual({job['role'] for job in view['jobs']},{'review','audit'})

    def test_running_review_has_an_active_lease_and_worker_identity(self):
        self.runner.hold=True
        self.discover()
        views=PipelineViews(self.store,runtime=self.config,clock=lambda:self.now)
        for _ in range(5):
            self.cycle()
            detail=views.pull_request(1,7)
            if detail['pull_request']['status']=='running':break
        self.assertEqual(detail['pull_request']['status'],'running')
        self.assertEqual(detail['pull_request']['stage_code'],'reviewing')
        self.assertEqual(detail['pull_request']['active_job_id'],detail['jobs'][0]['id'])


class PullRequestHTTPTests(unittest.TestCase):
    def setUp(self):
        from forge.store.db import Store
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.store=Store.create(temporary.name)
        required=patch('forge.store.runtime.required',return_value=self.store)
        required.start();self.addCleanup(required.stop)

    def handler(self,path):
        from tests.test_factory_changes import FactoryChangesHTTPTests
        handler=FactoryChangesHTTPTests.handler(self,path)
        handler._cached=Mock(side_effect=lambda key,build:build())
        return handler

    def test_route_requires_operator_session_and_validates_parameters(self):
        from tests.test_dashboard import dashboard
        views=Mock()
        with patch.object(dashboard,'runtime_control',return_value=False), patch('forge.factory.control.public_views',return_value=views):
            denied=self.handler('/api/pull-requests?repo_id=1&number=7');denied._signed_in.return_value=False
            denied.do_GET();views.pull_request.assert_not_called()
            allowed=self.handler('/api/pull-requests?repo_id=1&number=7');allowed.do_GET()
            views.pull_request.assert_called_once_with(1,7,before='',job=None)
            views.reset_mock()
            listing=self.handler('/api/pull-requests');listing.do_GET()
            views.pull_requests.assert_called_once_with(before='')
            views.reset_mock()
            for query in ('repo_id=1','repo_id=1&number=7&unknown=1','repo_id=1&number=7&number=8'):
                bad=self.handler('/api/pull-requests?'+query);bad.do_GET()
                self.assertEqual(bad._send.call_args.kwargs['status'],404)
            views.pull_request.assert_not_called()
