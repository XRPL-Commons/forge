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
