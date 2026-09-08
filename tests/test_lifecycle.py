import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.lifecycle import generation, observe_generation, record_decision, retained_blocker, progress_key
from forge.control import snapshot_path


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.identity = generation('$10|100|%20|300')
        self.row = {'repo': 'o/r', 'build': {'up': True, 'busy': False}}
        self.activity = {'observed_at': '1970-01-01T00:04:00Z', 'sessions': {
            'r-build': {'state': 'idle', 'session_started_at': '1970-01-01T00:01:40Z',
                        'generation_id': self.identity['id']}}}
        self.events = []
        recorder = patch('forge.activity.record', side_effect=lambda *a, **k: self.events.append((a, k)))
        recorder.start()
        self.addCleanup(recorder.stop)

    def decision(self, now=110, reason='worktree_not_clean', **kwargs):
        return record_decision(self.state, 'r-build', 'o/r', 'build', 'cleanup',
                               {'released': False, 'reason': reason, 'diagnostic': 'SECRET'},
                               identity=self.identity, now=now, **kwargs)

    def blocker(self, now=240):
        return retained_blocker(self.state, 'r-build', self.row, 'build', self.activity, now)

    def test_persistent_retention_becomes_actionable_without_event_spam(self):
        self.decision()
        self.assertIsNone(self.blocker())
        self.decision(240)
        result = self.blocker()
        self.assertEqual(result['kind'], 'cleanup_retained')
        self.assertIn('Uncommitted', result['why'])
        self.assertEqual(len(self.events), 1)
        self.assertNotIn('SECRET', (self.state/'lifecycle/r-build.json').read_text())
        self.assertNotIn('SECRET', str(self.events))

    def test_success_clears_retained_alert_and_records_resolution(self):
        self.decision(); self.decision(240)
        record_decision(self.state, 'r-build', 'o/r', 'build', 'cleanup',
                        {'released': True, 'reason': 'verified_handoff'}, identity=self.identity, now=241)
        self.assertIsNone(self.blocker(241))
        self.assertEqual(len(self.events), 2)

    def test_worker_replacement_does_not_inherit_retention(self):
        self.decision(); self.decision(240)
        self.activity['sessions']['r-build']['session_started_at'] = '1970-01-01T00:03:00Z'
        self.assertIsNone(self.blocker())
        self.activity['sessions']['r-build']['session_started_at'] = '1970-01-01T00:01:40Z'
        self.activity['sessions']['r-build']['generation_id'] = '$11|100|%21|301'
        self.assertIsNone(self.blocker())

    def test_stale_or_busy_observation_never_reports_retained_slot(self):
        self.decision(); self.decision(240)
        self.row['build']['busy'] = True
        self.assertIsNone(self.blocker())
        self.row['build']['busy'] = False
        self.activity['sessions']['r-build']['state'] = 'unknown'
        self.assertIsNone(self.blocker())
        self.activity['sessions']['r-build']['state'] = 'idle'
        self.assertIsNone(self.blocker(600))
        self.row['build']['up'] = False
        self.assertIsNone(self.blocker())

    def test_reason_change_and_observation_gap_restart_wait(self):
        self.decision(); self.decision(240, 'read_failed')
        self.assertIsNone(self.blocker())
        value = self.decision(600, 'read_failed')
        self.assertEqual(value['since'], 600)
        self.assertEqual(value['observations'], 1)

    def test_background_jobs_only_raise_attention_after_sustained_idle_retention(self):
        self.decision(reason='processes_not_quiescent')
        self.decision(240, 'processes_not_quiescent')
        self.assertIsNone(self.blocker())
        for at in (450, 650, 850, 1050, 1250, 1350):
            self.decision(at, 'processes_not_quiescent')
        self.activity['observed_at'] = 1350
        self.assertIn('Background', self.blocker(1350)['why'])

    def test_corrupt_prior_receipts_are_replaced_safely(self):
        directory = self.state/'lifecycle'; directory.mkdir()
        path = directory/'r-build.json'
        for raw in ('[]', 'null', '{broken'):
            path.write_text(raw)
            self.assertEqual(self.decision()['observations'], 1)
        old = self.decision()
        old['observations'] = 'invalid'
        path.write_text(json.dumps(old))
        self.assertEqual(self.decision(120)['observations'], 1)

    def test_inconsistent_identity_and_prebirth_since_are_not_current_evidence(self):
        self.decision(); self.decision(240)
        path = self.state/'lifecycle/r-build.json'
        value = json.loads(path.read_text())
        value['generation']['started_at'] = 99
        path.write_text(json.dumps(value))
        self.assertIsNone(self.blocker())
        value['generation'] = self.identity
        value['since'] = 99
        path.write_text(json.dumps(value))
        self.assertIsNone(self.blocker())

    def test_timeout_is_retained_without_invented_failure_content(self):
        self.decision(exit_code=124)
        self.decision(240, exit_code=124)
        self.assertEqual(self.blocker()['reason'], 'verification_timeout')

    def test_progress_fingerprint_tracks_work_identity_not_collection_time(self):
        snapshot = {'repo': 'o/r', 'status': 'ok', 'enabled': True, 'observed_at': 200,
                    'work': {'review': [12]}, 'schedule': {'active': []},
                    'item_details': {'12': {'state': 'open', 'labels': ['ready-for-review'],
                                            'updated_at': '1970-01-01T00:03:00Z'}}}
        path = snapshot_path(self.state, 'o/r'); path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(snapshot))
        first = progress_key(self.state, 'o/r', 'review', 240)
        self.assertIsNotNone(first)
        snapshot['observed_at'] = 230
        path.write_text(json.dumps(snapshot))
        self.assertEqual(first, progress_key(self.state, 'o/r', 'review', 240))
        snapshot['item_details']['12']['updated_at'] = '1970-01-01T00:03:50Z'
        path.write_text(json.dumps(snapshot))
        self.assertNotEqual(first, progress_key(self.state, 'o/r', 'review', 240))
        self.assertIsNone(progress_key(self.state, 'o/r', 'review', 700))
        snapshot['status'] = 'unknown'
        path.write_text(json.dumps(snapshot))
        self.assertIsNone(progress_key(self.state, 'o/r', 'review', 240))

    def test_generation_lookup_is_single_pane_and_independent_of_window_index(self):
        class Result:
            returncode = 0
            stdout = '$10|100|%20|300\n'
        calls = []
        def run(args, **kw):
            calls.append(args)
            return Result()
        self.assertEqual(observe_generation('r-build', run), self.identity)
        self.assertIn('list-panes', calls[0])
        self.assertNotIn('=r-build:1.1', calls[0])
        Result.stdout += '$10|100|%21|301\n'
        self.assertIsNone(observe_generation('r-build', run))


if __name__ == '__main__':
    unittest.main()
