import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from forge.control import atomic_json, snapshot_path

spec = importlib.util.spec_from_file_location("forge_dashboard", Path(__file__).parents[1] / "legacy/forge-status.py")
dashboard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dashboard)


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "repos.d").mkdir()
        for name, enabled in (("one", "true"), ("parked", "false")):
            (self.root / "repos.d" / (name + ".repo")).write_text(f'REPO="o/{name}"\nOWNER_LABEL="auto:owned-by-me"\nENABLED={enabled}\n')
        self.patch = patch.object(dashboard, "WD", str(self.root))
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_private_and_disabled_sessions_are_not_read(self):
        with patch.object(dashboard, "sh", return_value="one-build anton parked-audit") as shell, \
             patch.object(dashboard, "observe_tmux", return_value={"one-build":{"state":"unknown","reason":"capture_failed"}}) as observe:
            sessions = dashboard.tmux_sessions()
            self.assertEqual(set(sessions), {"one-build"})
            self.assertEqual(shell.call_count, 1)
            observe.assert_called_once_with({"one-build"}, state=self.root/"state")
            self.assertFalse(sessions["one-build"]["busy"])
            self.assertEqual(sessions["one-build"]["state"], "unknown")
        self.assertIn("one-audit", dashboard.allowed_sessions())
        self.assertNotIn("anton", dashboard.allowed_sessions())

    def test_slots_count_disabled_survivors_without_private_panes(self):
        with patch('forge.capacity.read_sessions', return_value=(['one-build','parked-review','private-dev'], 'observed')):
            slots = dashboard.slot_inventory()
        self.assertEqual(slots['occupied'], 2)
        self.assertEqual(slots['roles'], {'build':1, 'review':1, 'audit':0})
        with patch('forge.capacity.read_sessions', side_effect=ValueError('unknown')):
            self.assertEqual(dashboard.slot_inventory()['status'], 'unknown')

    def test_counts_are_snapshot_only_and_stale_is_unknown(self):
        path = snapshot_path(self.root / "state", "o/one")
        atomic_json(path, {"status": "ok", "observed_at": time.time(), "counts": {"audit": 2}})
        with patch.object(dashboard, "sh", side_effect=AssertionError("No GitHub calls")):
            self.assertEqual(dashboard.gh_counts("o/one"), {"audit": 2})
        atomic_json(path, {"status": "ok", "observed_at": 1, "counts": {"audit": 0}})
        self.assertEqual(dashboard.gh_counts("o/one")["audit"], "?")

    def test_focus_uses_same_snapshot_pr_metadata_before_history_collection(self):
        rows = [{'repo': 'o/one', 'queue_status': 'ok', 'observed_at': 123,
                 'build': {'up': False}, 'review': {'up': True}, 'audit': {'up': False},
                 'work': {'review': [22]}, 'schedule': {},
                 'item_details': {'22': {'number': 22, 'kind': 'pr', 'title': 'New PR',
                                         'body': 'Closes #16', 'updated_at': '2026-09-08T00:00:00Z'}}}]
        with patch.object(dashboard, 'sh', side_effect=AssertionError('No external reads')):
            dashboard.current_work_context(rows)
        focus = rows[0]['focus']['review']
        self.assertEqual(focus['items'][0]['title'], 'New PR')
        self.assertEqual(focus['items'][0]['metadata_observed_at'], 123)
        self.assertEqual(focus['basis'], 'queued')


class WorkerFaultContextTests(unittest.TestCase):
    def setUp(self):
        from forge.activity import record, stamp
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.state=Path(self.temporary.name)/'state'
        self.record=record
        record(self.state,'session_created','Map-review',repo='owner/Map',role='review',now=100)
        record(self.state,'pipeline_observed','Map-review',repo='owner/Map',role='review',metadata={'queued':[131]},now=110)
        record(self.state,'pipeline_observed','Map-review',repo='owner/Map',role='review',metadata={'queued':[]},now=120)
        self.identity='$7|100|%7|1000'
        self.blockers={'Map-review':{'kind':'cleanup_retained','evidence':{'generation_id':self.identity}}}
        self.activity={'sessions':{'Map-review':{'generation_id':self.identity,'session_started_at':stamp(100)}}}
        self.flow={'issues':[{'repo':'owner/Map','number':127,'state':'completed','pr_numbers':[131]}]}

    def context(self):
        dashboard.worker_fault_context(self.state,self.blockers,self.flow,self.activity)
        return self.blockers['Map-review'].get('context')

    def test_last_unique_generation_queue_links_completed_issue_without_assigning_it(self):
        context=self.context()
        self.assertEqual((context['pr'],context['issue']),(131,127))
        self.assertEqual(context['basis'],'Previously queued')
        self.assertNotIn('assigned',context)

    def test_replacement_or_mismatched_generation_never_reuses_old_queue(self):
        self.record(self.state,'session_created','Map-review',now=130)
        self.assertIsNone(self.context())
        self.blockers['Map-review']['evidence']['generation_id']='$8|130|%8|1001'
        self.assertIsNone(self.context())

    def test_unannounced_replacement_rejects_queue_older_than_current_birth(self):
        from forge.activity import stamp
        self.activity['sessions']['Map-review']['session_started_at']=stamp(130)
        self.assertIsNone(self.context())

    def test_ambiguous_latest_queue_never_selects_arbitrary_or_older_pr(self):
        self.record(self.state,'pipeline_observed','Map-review',repo='owner/Map',metadata={'queued':[131,132]},now=125)
        self.assertIsNone(self.context())

    def test_multiple_canonical_issues_keep_pr_context_without_guessing_one_issue(self):
        self.flow['issues'].append({'repo':'owner/Map','number':128,'pr_numbers':[131]})
        context=self.context()
        self.assertEqual(context['pr'],131)
        self.assertNotIn('issue',context)

    def test_canonical_join_is_repository_scoped(self):
        self.flow['issues'][0]['repo']='other/Map'
        self.assertNotIn('issue',self.context())


class DashboardHistoryTests(unittest.TestCase):
    def test_repository_details_are_local_only(self):
        with patch.object(dashboard, 'read_repository', return_value={'items': []}) as read, \
             patch.object(dashboard, 'sh', side_effect=AssertionError('No command execution')):
            self.assertEqual(dashboard.repository_detail('o/one'), {'items': []})
            read.assert_called_once_with(Path(dashboard.WD) / 'state', 'o/one')

    def request(self, path):
        handler = object.__new__(dashboard.H)
        handler.path = path
        handler._send = unittest.mock.Mock()
        handler.send_error = unittest.mock.Mock()
        handler.do_GET()
        return handler

    def test_history_filters_to_enabled_repositories(self):
        with patch.object(dashboard, 'repos', return_value=['o/one']), \
             patch.object(dashboard, 'read_overview', return_value={'status': 'ok'}) as read:
            handler = self.request('/api/history?days=7')
            read.assert_called_once_with(Path(dashboard.WD) / 'state', days=7, repos=['o/one'])
            self.assertEqual(json.loads(handler._send.call_args.args[0]), {'status': 'ok'})

    def test_history_rejects_unbounded_range(self):
        with patch.object(dashboard, 'read_overview') as read:
            handler = self.request('/api/history?days=999999')
            handler.send_error.assert_called_once_with(400)
            read.assert_not_called()

    def test_repository_details_reject_disabled_or_arbitrary_repo(self):
        with patch.object(dashboard, 'repos', return_value=['o/one']), \
             patch.object(dashboard, 'repository_detail') as read:
            handler = self.request('/api/repository?repo=o/private')
            handler.send_error.assert_called_once_with(400)
            read.assert_not_called()

    def test_repository_endpoint_reads_local_data(self):
        with patch.object(dashboard, 'repos', return_value=['o/one']), \
             patch.object(dashboard, 'repository_detail', return_value={'items': []}) as read:
            handler = self.request('/api/repository?repo=o%2Fone')
            read.assert_called_once_with('o/one')
            self.assertEqual(json.loads(handler._send.call_args.args[0]), {'items': []})


class WorkflowApiTests(unittest.TestCase):
    def request(self,path):
        handler=object.__new__(dashboard.H);handler.path=path
        handler._send=unittest.mock.Mock();handler.send_error=unittest.mock.Mock()
        handler.do_GET();return handler

    def test_route_validates_bounded_cursor_limit_and_configured_repo(self):
        with patch.object(dashboard,'repos',return_value=['Owner/One']),patch.object(dashboard,'workflow_view',return_value={'status':'ok'}) as read:
            handler=self.request('/api/workflow?repo=owner/one&after=12&limit=20')
            read.assert_called_once_with('owner/one',12,20)
            self.assertEqual(json.loads(handler._send.call_args.args[0])['status'],'ok')
            for query in ('limit=0','limit=101','after=-1','after=abc','repo=private/repo','after=1&after=2','unknown=true','recent=2','before=3','recent=1&after=2','recent=1&before=-1'):
                with self.subTest(query=query):
                    read.reset_mock();handler=self.request('/api/workflow?'+query)
                    handler.send_error.assert_called_once_with(400);read.assert_not_called()

    def test_state_and_history_filter_disabled_repositories_and_keep_cursor_progress(self):
        value={'status':'ok','issues':[{'repo':'owner/one','number':1},{'repo':'private/repo','number':9}],
               'assignments':[],'slots':[],'repositories':[{'repo':'owner/one'}]}
        events=[{'id':13,'repo':'private/repo'},{'id':14,'repo':'owner/one'},{'id':15,'repo':'owner/one'}]
        with patch.object(dashboard,'repos',return_value=['Owner/One']),patch('forge.workflow.read_state',return_value=value),patch('forge.workflow.history',return_value=events) as history:
            result=dashboard.workflow_view(after=12,limit=2)
        self.assertEqual(result['issues'],[{'repo':'owner/one','number':1}])
        self.assertEqual(result['events'],[events[1]])
        self.assertEqual(result['next_after'],14);self.assertTrue(result['has_more'])
        self.assertEqual(history.call_args.kwargs,{'repo':None,'after':12,'limit':3})

    def test_recent_history_starts_at_newest_enabled_event_and_pages_backwards(self):
        import sqlite3
        value={'status':'ok','issues':[],'assignments':[],'slots':[],'repositories':[]}
        with tempfile.TemporaryDirectory() as temporary,patch.object(dashboard,'WD',temporary),patch.object(dashboard,'repos',return_value=['Owner/One']),patch('forge.workflow.read_state',return_value=value):
            state=Path(temporary)/'state';state.mkdir()
            with sqlite3.connect(state/'workflow.sqlite3') as db:
                db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,repo TEXT,payload TEXT,result TEXT)')
                db.executemany('INSERT INTO events VALUES(?,?,?,?)',[(n,'owner/one','{}','{}') for n in range(1,131)]+[(131,'private/repo','{}','{}')])
            latest=dashboard.workflow_view(recent=True,limit=100)
            self.assertEqual([e['id'] for e in latest['events']],list(range(130,30,-1)))
            self.assertEqual(latest['next_before'],31);self.assertTrue(latest['has_more'])
            older=dashboard.workflow_view(recent=True,before=31,limit=100)
            self.assertEqual([e['id'] for e in older['events']],list(range(30,0,-1)))
            self.assertFalse(older['has_more'])
            with patch.object(dashboard,'workflow_view',return_value=latest) as read:
                self.request('/api/workflow?recent=1&before=131&limit=100')
                read.assert_called_once_with(None,0,100,recent=True,before=131)

    def test_uninitialized_read_never_creates_authority_and_corruption_is_unavailable(self):
        with tempfile.TemporaryDirectory() as temporary,patch.object(dashboard,'WD',temporary),patch.object(dashboard,'repos',return_value=['o/one']):
            result=dashboard.workflow_view()
            self.assertEqual(result['status'],'uninitialized')
            self.assertFalse((Path(temporary)/'state').exists())
            with patch('forge.workflow.read_state',side_effect=ValueError('corrupt')):
                self.assertEqual(dashboard.workflow_view()['status'],'unavailable')
