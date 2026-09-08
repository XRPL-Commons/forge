import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from forge.notifications import enqueue, drain, deliver


class NotificationTests(unittest.TestCase):
    def test_enqueue_is_durable_and_independent_channels_retry(self):
        with tempfile.TemporaryDirectory() as state:
            event_id = enqueue(state, 'an alert', now=1)
            path = Path(state, 'outbox', event_id + '.json')
            self.assertEqual(json.loads(path.read_text())['message'], 'an alert')
            calls = []
            def sender(channel, event):
                self.assertTrue(path.exists())
                calls.append(channel)
                return 'failed' if channel == 'relay' else 'accepted_by_relay'
            self.assertEqual(drain(state, sender, now=1), 1)
            self.assertCountEqual(calls, ['ntfy', 'gchat', 'relay'])
            record = json.loads(path.read_text())
            self.assertEqual(record['channels']['relay']['status'], 'retry')
            calls.clear()
            drain(state, sender, now=2)
            self.assertEqual(calls, [])
            drain(state, sender, now=32)
            self.assertEqual(calls, ['relay'])

    def test_channels_start_independently(self):
        with tempfile.TemporaryDirectory() as state:
            enqueue(state, 'hello')
            rendezvous = threading.Barrier(3)
            def sender(channel, event):
                rendezvous.wait(timeout=2)
                return 'accepted_by_relay'
            drain(state, sender)
            event = json.loads(next(Path(state, 'outbox').glob('*.json')).read_text())
            self.assertTrue(all(c['status'] == 'accepted_by_relay' for c in event['channels'].values()))

    def test_http_timeout_is_failure_without_secret_in_state(self):
        def runner(cmd, **kwargs):
            self.assertEqual(kwargs['timeout'], 20)
            raise subprocess.TimeoutExpired(cmd, 20)
        self.assertEqual(deliver('ntfy', {'message': 'hi'}, {'NTFY_TOPIC': 'secret'}, runner), 'failed')

    def test_bounded_retry_survives_reopening(self):
        with tempfile.TemporaryDirectory() as state:
            enqueue(state, 'hello')
            for n in range(10):
                drain(state, lambda c, e: 'failed', now=100000 * n)
            event = json.loads(next(Path(state, 'outbox').glob('*.json')).read_text())
            self.assertTrue(all(c['attempts'] == 8 and c['status'] == 'failed' for c in event['channels'].values()))

    def test_event_key_deduplicates_without_resetting_delivery(self):
        with tempfile.TemporaryDirectory() as state:
            first = enqueue(state, 'hello', event_key='same-incident')
            drain(state, lambda c, e: 'accepted_by_relay')
            second = enqueue(state, 'hello', event_key='same-incident')
            self.assertEqual(first, second)
            record = json.loads(Path(state, 'outbox', first + '.json').read_text())
            self.assertEqual(record['channels']['ntfy']['status'], 'accepted_by_relay')

    def test_enqueue_cli_wrapper_does_not_deliver(self):
        import os
        with tempfile.TemporaryDirectory() as state:
            env = dict(os.environ, FORGE_NOTIFY_STATE=state)
            result = subprocess.run(['./notify.sh', '--event-key', 'stable', 'test alert'], env=env, text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            event = json.loads(next(Path(state, 'outbox').glob('*.json')).read_text())
            self.assertTrue(all(c['attempts'] == 0 for c in event['channels'].values()))

    def test_corrupt_event_does_not_block_valid_event(self):
        with tempfile.TemporaryDirectory() as state:
            event_id = enqueue(state, 'valid')
            Path(state, 'outbox', '000-corrupt.json').write_text('{')
            calls = []
            drain(state, lambda c, e: calls.append(c) or 'accepted_by_relay')
            self.assertEqual(len(calls), 3)
            self.assertTrue(Path(state, 'quarantine', '000-corrupt.json').exists())
            event = json.loads(Path(state, 'outbox', event_id + '.json').read_text())
            self.assertEqual(event['channels']['ntfy']['status'], 'accepted_by_relay')

    def test_concurrent_same_event_keeps_one_record(self):
        from concurrent.futures import ThreadPoolExecutor
        with tempfile.TemporaryDirectory() as state:
            with ThreadPoolExecutor(max_workers=8) as pool:
                ids = list(pool.map(lambda n: enqueue(state, 'same', event_key='same'), range(30)))
            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(len(list(Path(state, 'outbox').glob('*.json'))), 1)
            drain(state, lambda c, e: 'accepted_by_relay')
            with ThreadPoolExecutor(max_workers=8) as pool:
                list(pool.map(lambda n: enqueue(state, 'same', event_key='same'), range(30)))
            event = json.loads(next(Path(state, 'outbox').glob('*.json')).read_text())
            self.assertEqual(event['channels']['ntfy']['attempts'], 1)

    def test_default_drain_bounds_delivery_work(self):
        with tempfile.TemporaryDirectory() as state:
            for _ in range(4): enqueue(state, 'alert')
            self.assertEqual(drain(state, lambda c, e: 'accepted_by_relay'), 2)
