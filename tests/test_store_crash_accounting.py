"""Crash the real command writer and check committed lifecycle accounting."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

from forge.store.db import Store
from forge.store.transitions import Lifecycle


WRITER = r'''
import sys, uuid, time
from forge.store.db import Store
from forge.store.models import Command
from forge.store.transitions import Lifecycle
store = Store.open(sys.argv[1])
life = Lifecycle(store)
ready = False
while True:
    with store.read_snapshot() as db:
        version = db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0]
        gate = db.execute("SELECT id FROM unit_gates WHERE unit_id='unit' AND code='environment' AND resolved_at IS NULL").fetchone()
    command = Command(uuid.uuid4().hex, 'operator', 'unit', version)
    if gate:
        life.resolve_gate(command, gate[0], release='recheck', evidence={'verified': True, 'observed_at': time.time()})
    else:
        life.open_gate(command, 'environment', details={'reason': 'test'})
    if not ready:
        print('ready', flush=True)
        ready = True
'''


class StoreCrashAccountingTests(unittest.TestCase):
    def test_killed_command_writers_leave_only_complete_decisions_and_events(self):
        with tempfile.TemporaryDirectory(prefix='forge-crash-accounting-') as folder:
            root = Path(folder).resolve()
            store = Store.create(root)
            Lifecycle(store).admit_issue(
                request_id='admit', actor_id='operator', unit_id='unit', issue_id='issue',
                repository_id=1, repository_name='example/repo', issue_number=1,
                title='A change', body='Requirements', requirements={}, observed_at=1,
            )
            for delay in (0.02, 0.05, 0.09):
                process = subprocess.Popen(
                    [sys.executable, '-c', WRITER, str(root)],
                    cwd=Path(__file__).resolve().parents[1], env=os.environ.copy(),
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                )
                try:
                    self.assertEqual(process.stdout.readline().strip(), 'ready')
                    time.sleep(delay)
                    if process.poll() is not None:
                        _, error = process.communicate(timeout=10)
                        self.fail('writer stopped before the kill: ' + error)
                    process.kill()
                    process.communicate(timeout=10)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)
                reopened = Store.open(root)
                with reopened.read_snapshot() as db:
                    version = db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0]
                    applied = db.execute("SELECT COUNT(*) FROM decisions WHERE unit_id='unit' AND status='applied'").fetchone()[0]
                    events = db.execute("SELECT COUNT(*) FROM events WHERE unit_id='unit' AND kind='decision_applied'").fetchone()[0]
                    self.assertGreater(version, 1)
                    self.assertEqual(version, applied)
                    self.assertEqual(events, applied)
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                    self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
