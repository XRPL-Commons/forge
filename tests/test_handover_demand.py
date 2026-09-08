import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest

from forge import activity
from forge.control import atomic_json,snapshot_path
from forge.handover_demand import read_demand


class HandoverDemandTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.config=self.root/'repos';self.config.mkdir()
        (self.config/'one.repo').write_text('REPO=o/one\nOWNER_LABEL=auto:owned-by-fixture\n')
        self.state=self.root/'state'
        activity.record(self.state,'session_created','one-build',now=100)
        with sqlite3.connect(self.state/'activity.sqlite3') as db:
            self.creation=db.execute('SELECT max(id) FROM events').fetchone()[0]
        self.manifest=dict(session='one-build',repo='o/one',role='build',released=True,released_at=120,
                           creation_event_id=self.creation,claims=[127,800])
        self.snapshot=dict(repo='o/one',enabled=True,status='ok',observed_at=150,counts={'builder':0},
                           schedule={'active':[127,800],'handed_off':[]},work={'wip':[127,800],'review':[],'audit':[]})
        self.write()

    def write(self):
        atomic_json(self.state/'provider-handovers/one-build.json',self.manifest)
        atomic_json(snapshot_path(self.state,'o/one'),self.snapshot)

    def demand(self):
        return read_demand(self.config,self.state,'one-build',now=160)

    def test_preserved_claims_restore_zero_builder_admission_without_new_claims(self):
        self.assertEqual(self.demand()['claims'],[127,800])
        self.assertEqual(self.demand()['demand'],2)
        self.snapshot['schedule']['active'].append(999);self.snapshot['work']['wip'].append(999);self.write()
        self.assertEqual(self.demand()['claims'],[127,800])

    def test_new_generation_consumes_manifest_even_when_same_claims_remain(self):
        activity.record(self.state,'session_created','one-build',now=155)
        self.assertEqual(self.demand()['demand'],0)
        activity.record(self.state,'session_stopped','one-build',now=156)
        self.assertEqual(self.demand()['demand'],0)

    def test_unrelated_session_events_do_not_consume_handover(self):
        activity.record(self.state,'session_created','one-review',now=155)
        self.assertEqual(self.demand()['demand'],2)

    def test_closed_or_published_claims_do_not_restart_builder(self):
        self.snapshot['schedule']['handed_off']=[127];self.snapshot['schedule']['active']=[127];self.write()
        self.assertEqual(self.demand()['demand'],0)
        self.snapshot['schedule']['active']=[127,800];self.write()
        self.assertEqual(self.demand()['claims'],[800])

    def test_manifest_not_released_identity_mismatch_or_unknown_generation_fail_closed(self):
        for field,value in [('released',False),('repo','other/one'),('session','private-build'),('role','audit'),
                            ('creation_event_id',None),('creation_event_id',True),('released_at',float('nan'))]:
            with self.subTest(field=field):
                old=self.manifest[field];self.manifest[field]=value;self.write()
                self.assertEqual(self.demand()['demand'],0)
                self.manifest[field]=old
        self.write()
        self.assertEqual(read_demand(self.config,self.state,'one-audit',now=160)['demand'],0)
        self.assertEqual(read_demand(self.config,self.state,'private-build',now=160)['demand'],0)

    def test_stale_future_unknown_or_malformed_snapshot_has_no_demand(self):
        for field,value in [('observed_at',-200),('observed_at',170),('status','unknown'),('enabled',False)]:
            with self.subTest(field=field):
                old=self.snapshot[field];self.snapshot[field]=value;self.write()
                self.assertEqual(self.demand()['demand'],0)
                self.snapshot[field]=old
        self.snapshot['schedule']['handed_off']=None;self.write()
        self.assertEqual(self.demand()['demand'],0)

    def test_read_only_missing_store_and_real_cli_bounded_integer(self):
        missing=self.root/'missing'
        self.assertEqual(read_demand(self.config,missing,'one-build',now=160)['demand'],0)
        self.assertFalse(missing.exists())
        self.snapshot['observed_at']=__import__('time').time();self.write()
        result=subprocess.run([os.sys.executable,'-m','forge.handover_demand','--config',str(self.config),
            '--state',str(self.state),'--session','one-build'],capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout,'2\n')
