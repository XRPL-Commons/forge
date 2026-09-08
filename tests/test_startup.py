from tests.host_isolation import deny_host_tmux
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from forge.control import atomic_json, snapshot_path
from forge.startup import check, release


class StartupTests(unittest.TestCase):
    def setUp(self):
        deny_host_tmux(self)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.state = self.root / 'state'
        self.config = self.root / 'config'
        self.config.mkdir()
        self.clone = self.root / 'clone'
        (self.config / 'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_BUILD={self.clone}\n')
        self.snap = {'repo': 'o/one', 'enabled': True, 'status': 'ok', 'observed_at': 99,
                     'schedule': {'active': [], 'eligible': [1]}, 'counts': {'wip': 0},
                     'work': {k: [] for k in ['wip', 'review', 'audit', 'sentback', 'limbo']}}
        self.save()
        self.db = sqlite3.connect(self.state / 'activity.sqlite3')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,at TEXT,event TEXT,session TEXT,metadata TEXT)')
        self.event('session_created', '1970-01-01T00:00:10Z')
        self.head = 'a' * 40
        self.main = self.head
        self.status = ''
        self.branch = 'main'
        self.identity = f'one-build|$1|10|%1|123|bash|{self.clone}'
        self.pane = 'user@host:~/one$\n'
        self.processes = '123 1 bash\n456 1 unrelated'
        self.calls = []
        self.reads = 0
        self.second = None

    def save(self):
        atomic_json(snapshot_path(self.state, 'o/one'), self.snap)

    def event(self, event, at='1970-01-01T00:00:20Z', metadata=None):
        self.db.execute('INSERT INTO events(at,event,session,metadata) VALUES(?,?,?,?)', (at,event,'one-build',json.dumps(metadata or {})))
        self.db.commit()

    def fake_run(self, args, **kwargs):
        self.calls.append(args)
        if args[0] == 'git':
            cmd = args[3:]
            if cmd == ['rev-parse', '--show-toplevel']:out = str(self.clone)
            elif cmd == ['remote', 'get-url', 'origin']:out = 'git@github.com:o/one.git'
            elif cmd == ['symbolic-ref', '--short', 'HEAD']:out = self.branch
            elif cmd[0] == 'status':out = self.status
            elif cmd == ['rev-parse', 'HEAD']:out = self.head
            else:self.fail(args)
        elif args[:2] == ['gh','api']:
            self.reads += 1
            if self.reads == 2 and self.second:self.second()
            out = json.dumps({'object': {'sha': self.main}})
        elif args[:2] == ['tmux','list-panes']:out = self.identity
        elif args[:2] == ['tmux','capture-pane']:out = self.pane
        elif args[0] == 'ps':out = self.processes
        elif args[:2] == ['tmux','kill-session']:out = ''
        else:self.fail(args)
        return SimpleNamespace(returncode=0, stdout=out, stderr='')

    def decision(self, now=100):
        return check(self.config,self.state,'one-build',run=self.fake_run,now=now)

    def test_empty_never_dispatched_startup_is_recoverable_read_only(self):
        self.assertTrue(self.decision()['eligible'])
        self.assertFalse(any(c[:2] == ['tmux','kill-session'] for c in self.calls))

    def test_grace_period_and_generation_required(self):
        self.assertFalse(self.decision(now=40)['eligible'])
        self.identity = self.identity.replace('|10|','|20|')
        self.assertFalse(self.decision()['eligible'])

    def test_dirty_unpublished_nonmain_or_typed_shell_preserved(self):
        for key,value in [('status','?? work.txt'),('main','b'*40),('branch','working'),('pane','user@host:~/one$ unfinished'),('pane','user@host:~/one$ unfinished$'),('processes',self.processes+'\n789 123 claude')]:
            with self.subTest(key=key):
                old=getattr(self,key);setattr(self,key,value)
                self.assertFalse(self.decision()['eligible']);setattr(self,key,old)

    def test_claims_or_completed_startup_prevent_recovery(self):
        self.snap['schedule']['active']=[1];self.save()
        self.assertFalse(self.decision()['eligible'])
        self.snap['schedule']['active']=[];self.save()
        (self.state/'one-build.up').touch()
        self.assertFalse(self.decision()['eligible'])

    def test_recorded_dispatch_nudge_or_busy_prevents_recovery(self):
        for event,meta in [('dispatch',{}),('nudge',{}),('activity_changed',{'state':'busy'})]:
            with self.subTest(event=event):
                self.event(event,metadata=meta)
                self.assertFalse(self.decision()['eligible'])
                self.db.execute("DELETE FROM events WHERE event!='session_created'");self.db.commit()

    def test_locked_release_checks_twice_and_preserves_launch_budget(self):
        guard={'account_until':9000,'sessions':{'one-build':{'reason':'no_progress','until':8000,'launches':[1,2]}}}
        atomic_json(self.state/'legacy-health/guard.json',guard)
        with patch('forge.startup._locked',return_value=True),patch('forge.activity.record'):
            result=release(self.config,self.state,'one-build',run=self.fake_run,now=100)
        self.assertTrue(result['released']);self.assertEqual(self.reads,2)
        self.assertEqual(self.calls[-1],['tmux','kill-session','-t','$1'])
        saved=json.loads((self.state/'legacy-health/guard.json').read_text())
        self.assertEqual(saved['account_until'],9000)
        self.assertEqual(saved['sessions']['one-build'],{'until':0,'launches':[1,2]})

    def test_changed_identity_or_new_dispatch_never_killed(self):
        self.second=lambda:self.event('dispatch')
        with patch('forge.startup._locked',return_value=True):
            result=release(self.config,self.state,'one-build',run=self.fake_run,now=100)
        self.assertFalse(result['released'])
        self.assertFalse(any(c[:2]==['tmux','kill-session'] for c in self.calls))

    def test_never_dispatched_reviewer_can_recover_with_current_pr_demand(self):
        (self.config/'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_REVIEW={self.clone}\n')
        self.snap['work']['review']=[2];self.snap['counts']['review']=1
        self.snap['schedule']['active']=[1];self.snap['counts']['wip']=1;self.save()
        self.identity=self.identity.replace('one-build','one-review')
        self.db.execute("UPDATE events SET session='one-review'");self.db.commit()
        self.assertTrue(check(self.config,self.state,'one-review',run=self.fake_run,now=100)['eligible'])
        self.snap['work']['review']=[];self.save()
        self.assertFalse(check(self.config,self.state,'one-review',run=self.fake_run,now=100)['eligible'])

    def test_never_dispatched_auditor_requires_fresh_audit_queue(self):
        (self.config/'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_AUDIT={self.clone}\n')
        self.snap['work']['audit']=[2];self.snap['counts']['audit']=1
        self.snap['schedule']['active']=[1];self.snap['counts']['wip']=1;self.save()
        self.identity=self.identity.replace('one-build','one-audit')
        self.db.execute("UPDATE events SET session='one-audit'");self.db.commit()
        self.assertTrue(check(self.config,self.state,'one-audit',run=self.fake_run,now=100)['eligible'])
        self.snap['work']['audit']=[];self.save()
        self.assertFalse(check(self.config,self.state,'one-audit',run=self.fake_run,now=100)['eligible'])

    def test_auditor_with_previous_dispatch_is_never_recovered(self):
        (self.config/'one.repo').write_text(f'REPO=o/one\nOWNER_LABEL=auto:owned-by-luc\nCLONE_AUDIT={self.clone}\n')
        self.snap['work']['audit']=[2];self.snap['counts']['audit']=1;self.save()
        self.identity=self.identity.replace('one-build','one-audit')
        self.event('dispatch')
        self.db.execute("UPDATE events SET session='one-audit'");self.db.commit()
        decision=check(self.config,self.state,'one-audit',run=self.fake_run,now=100)
        self.assertFalse(decision['eligible'])
        self.assertEqual(decision['reason'],'startup_was_dispatched_or_active')

    def test_launch_command_uses_quoted_absolute_binary_and_exit_receipt(self):
        from forge.startup import launch_command
        import shlex
        binary=self.root/'provider name'
        binary.write_text('#!/bin/sh\nexit 0\n');binary.chmod(0o700)
        value=launch_command(str(binary),self.state,'one-review')
        self.assertTrue(value.startswith(shlex.quote(str(binary))+' --dangerously-skip-permissions'))
        self.assertIn('record-exit',value)
        self.assertIn('forge_provider_exit=$?',value)
        with self.assertRaises(ValueError):launch_command(str(self.root/'missing'),self.state,'one-review')

    def test_startup_failure_guard_is_short_and_preserves_failure_budget(self):
        from forge.legacy_health import LegacyGuard
        guard=LegacyGuard(self.state)
        guard.transact('launch','one-review',now=100)
        self.assertFalse(guard.transact('start_failed','one-review',now=101)[0])
        stored=json.loads((self.state/'legacy-health/guard.json').read_text())['sessions']['one-review']
        self.assertEqual(stored,{'launches':[100],'reason':'provider_start_failed','until':161})
        self.assertFalse(guard.transact('check','one-review',now=160)[0])
        self.assertTrue(guard.transact('check','one-review',now=162)[0])
        guard.transact('observe','one-review','authentication failed',now=163)
        guard.transact('start_failed','one-review',now=164)
        stored=json.loads((self.state/'legacy-health/guard.json').read_text())['sessions']['one-review']
        self.assertEqual(stored['reason'],'blocked_auth')
        self.assertEqual(stored['until'],3763)

    def test_provider_receipt_is_generation_specific_and_normal_exit_is_distinct(self):
        from forge.startup import record_failure
        import os
        generation=['$1|10']
        def run(args, **kwargs):
            return SimpleNamespace(returncode=0, stdout=generation[0] if args[1]=='display-message' else 'provider: failed', stderr='')
        result=record_failure(self.state,'one-review',exit_code=127,startup=False,run=run,now=20)
        self.assertEqual(result['reason'],'provider_start_failed')
        self.assertEqual(record_failure(self.state,'one-review',run=run)['exit_code'],127)
        generation[0]='$2|30'
        self.assertIsNone(record_failure(self.state,'one-review',run=run)['exit_code'])
        marker=self.state/'one-review.up';marker.touch();os.utime(marker,(35,35))
        result=record_failure(self.state,'one-review',exit_code=0,startup=False,run=run)
        self.assertEqual(result['reason'],'provider_exited')
        self.assertIsNone(result['diagnostic'])
        # An old startup marker cannot turn a new failed start into a normal exit.
        generation[0]='$3|40'
        self.assertEqual(record_failure(self.state,'one-review',exit_code=1,startup=False,run=run)['reason'],'provider_start_failed')

    def test_old_generation_marker_does_not_block_new_startup(self):
        import os
        marker=self.state/'one-build.up';marker.touch();os.utime(marker,(5,5))
        self.assertTrue(self.decision()['eligible'])
        marker.write_text(json.dumps({'generation':'$9|1','status':'submitted'}))
        self.assertTrue(self.decision()['eligible'])
        marker.write_text(json.dumps({'generation':'$1|10','status':'submitted'}))
        self.assertFalse(self.decision()['eligible'])

    def test_dispatch_receipts_bind_exact_generation_and_distinguish_uncertainty(self):
        from forge.startup import dispatch_receipt, dispatch_is_uncertain, marker_blocks
        def run(args,**kwargs):return SimpleNamespace(returncode=0,stdout='$1|10|%1',stderr='')
        dispatch_receipt(self.state,'one-build','$1|10|%1',uncertain=True,run=run,now=20)
        self.assertTrue(dispatch_is_uncertain(self.state,'one-build','$1|10|%1'))
        self.assertFalse(dispatch_is_uncertain(self.state,'one-build','$2|30|%2'))
        self.assertFalse((self.state/'one-build.up').exists())
        with self.assertRaises(ValueError):dispatch_receipt(self.state,'one-build','$2|30|%2',run=run)
        dispatch_receipt(self.state,'one-build','$1|10|%1',run=run,now=21)
        self.assertTrue(marker_blocks(self.state,'one-build','$1|10'))

    def test_requires_supervisor_lock(self):
        self.assertFalse(release(self.config,self.state,'one-build',lock_fd=-1,run=self.fake_run)['released'])
        self.assertEqual(self.calls,[])


if __name__ == '__main__':
    unittest.main()
