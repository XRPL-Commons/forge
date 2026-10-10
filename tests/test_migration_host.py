import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from forge.store.migration.host import Host
from forge.store.migration.snapshot import MigrationError
from forge.state import atomic_json


class HostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
        self.host=Host(self.root,self.root/'source')
        self.active=['forge-status.service','forge-supervisor.service']
        self.plan={'plan':'same','blockers':[],'services':list(self.active),'source_revision':'revision'}
        self.calls=[]
        def command(*args):
            self.calls.append(args)
            if 'stop' in args:self.active=[]
            if 'start' in args:self.active=list(args[args.index('start')+1:])
            return ''
        self.host.command=command
        self.host.units=lambda:list(self.active)
        self.host.inspect=lambda:self.plan
        self.host.quiet=lambda:None

    def test_initial_updater_executes_pinned_checkout_with_typed_receipt_protocol(self):
        from types import SimpleNamespace
        requests=[]
        def run(argv,**kwargs):
            requests.append((argv,kwargs))
            return SimpleNamespace(returncode=0,stdout='{"prepared_digest":"proof"}')
        self.host.run=run
        value={'id':'operation','source_revision':'a'*40}
        with patch('forge.store.migration.runtime.inspect',return_value={'library_directory':'/qualified/lib'}):
            receipt=self.host.initial_updater('prepare_initial',value)
            self.host.initial_updater('commit_initial',value,receipt)
        self.assertEqual(requests[0][0],['/usr/bin/python3','-m','forge.host_initial_update'])
        self.assertEqual(requests[0][1]['cwd'],self.host.source)
        self.assertEqual(requests[0][1]['env']['PYTHONPATH'],str(self.host.source))
        self.assertEqual(requests[0][1]['env']['LD_LIBRARY_PATH'],'/qualified/lib')
        self.assertNotIn('receipt',json.loads(requests[0][1]['input']))
        self.assertEqual(json.loads(requests[1][1]['input'])['receipt'],receipt)

    def test_failed_capture_restores_original_services_and_removes_journal(self):
        with patch('forge.store.migration.host.capture',side_effect=MigrationError('copy_failed')):
            with self.assertRaisesRegex(MigrationError,'copy_failed'):
                self.host.capture(self.root/'snapshot','same')
        self.assertEqual(self.active,self.plan['services'])
        self.assertFalse(self.host.journal.exists())
        self.assertTrue(any('stop' in c for c in self.calls))

    def test_changed_plan_does_not_stop_services(self):
        with self.assertRaisesRegex(MigrationError,'plan_changed'):
            self.host.capture(self.root/'snapshot','old')
        self.assertEqual(self.calls,[])

    def test_resume_restores_after_lost_process_without_recapturing_or_importing(self):
        atomic_json(self.host.journal,{'services':self.active})
        self.active=[]
        result=self.host.restore()
        self.assertEqual(result['status'],'restored')
        self.assertEqual(self.active,self.plan['services'])
        self.assertFalse(self.host.journal.exists())

    def test_failed_restore_keeps_recovery_journal(self):
        atomic_json(self.host.journal,{'services':self.active})
        self.host.units=lambda:[]
        with self.assertRaisesRegex(MigrationError,'service_restore_incomplete'):
            self.host.restore()
        self.assertTrue(self.host.journal.exists())

    def test_unfinished_maintenance_requires_resume_and_blocks_new_capture(self):
        atomic_json(self.host.journal,{'services':self.active})
        with self.assertRaisesRegex(MigrationError,'maintenance_resume_required'):
            self.host.capture(self.root/'snapshot','same')
        self.assertEqual(self.calls,[])

    def test_plan_reports_unqualified_sqlite_without_blocking_a_safe_capture(self):
        from types import SimpleNamespace
        host=Host(self.root,self.root/'source',run=lambda *args,**kwargs:SimpleNamespace(returncode=1,stdout=''))
        host.identity=lambda:'revision'
        host.units=lambda:[]
        ledger=dict(version=1,issues={},jobs={},slots={},events=[])
        with patch('forge.store.migration.host.sources',return_value={'state/factory/factory-ledger.json':json.dumps(ledger).encode()}), \
                patch('forge.store.migration.host.qualified_sqlite',return_value=False):
            plan=host.inspect()
        self.assertEqual(plan['blockers'],[])
        self.assertEqual(plan['cutover_blockers'],['sqlite_unqualified'])
        self.assertFalse(plan['activation'])
        self.assertGreater(plan['free_bytes'],0)
