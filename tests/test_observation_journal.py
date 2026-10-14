"""Only changed observations enter the journal; freshness and replay stay exact."""
import json
from pathlib import Path
import tempfile
import unittest

from forge.factory.store_inventory import StoreInventory
from forge.store.db import Store
from forge.store.models import Command, IdempotencyConflict, TransitionError
from forge.store.repository_readiness import read_revision
from forge.store.transitions import Lifecycle, manifest_hash


class ObservationJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = Store.create(Path(temporary.name)); self.now = 100
        self.life = Lifecycle(self.store, clock=lambda: self.now)
        self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='unit', issue_id='1:1',
            repository_id=1, repository_name='owner/repo', issue_number=1, title='Issue', body='Body',
            requirements={'acceptance':['works']}, observed_at=90)
        self.facts = dict(state='open', title='Issue', body='Body', requirements={'acceptance':['works']},
            remote_updated_at='2026-10-10T00:00:00Z', generation=1)

    def command(self, request):
        with self.store.read_snapshot() as db:
            version = db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0]
        return Command(request, 'supervisor:observation', 'unit', version)

    def counts(self):
        with self.store.read_snapshot() as db:
            return tuple(db.execute('SELECT count(*) FROM '+table).fetchone()[0] for table in ('decisions','events'))

    def test_noop_and_stale_observations_keep_freshness_without_journal_growth(self):
        before = self.counts()
        first = self.life.observe_issue(self.command('first'), '1:1', **self.facts)
        self.assertTrue(first['unchanged'])
        self.now += 10
        self.life.observe_issue(self.command('fresh'), '1:1', **{**self.facts,'generation':2})
        self.now += 10
        stale = self.life.observe_issue(self.command('stale'), '1:1', **self.facts)
        self.assertTrue(stale['stale'])
        self.assertEqual(self.counts(), before)
        with self.store.read_snapshot() as db:
            self.assertEqual(tuple(db.execute('SELECT collection_generation,observed_at,version FROM issues').fetchone()), (2,110,1))
            self.assertEqual(db.execute('SELECT version FROM units').fetchone()[0], 1)

    def test_noop_request_is_unreserved_but_real_change_replays_exactly(self):
        command = self.command('request')
        self.life.observe_issue(command, '1:1', **self.facts)
        facts = {**self.facts, 'generation':2, 'labels':['hold']}
        result = self.life.observe_issue(command, '1:1', **facts)
        self.assertFalse(result['unchanged'])
        count = self.counts()
        self.assertEqual(self.life.observe_issue(command, '1:1', **facts), result)
        self.assertEqual(self.counts(), count)
        with self.assertRaises(IdempotencyConflict):
            self.life.observe_issue(command, '1:1', **{**facts,'body':'different'})
        self.assertEqual(self.counts(), count)

    def test_changed_journals_pin_revisions_without_copying_text_and_replay_after_later_changes(self):
        facts = {**self.facts, 'title':'UNIQUE TITLE', 'body':'PRIVATE BODY '+('x'*20000),
                 'requirements':{'acceptance':['UNIQUE REQUIREMENT']}, 'labels':['ready']}
        command = self.command('changed')
        result = self.life.observe_issue(command, '1:1', **facts)
        with self.store.read_snapshot() as db:
            payload = json.loads(db.execute("SELECT payload_json FROM decisions WHERE request_id='changed'").fetchone()[0])
            event = json.loads(db.execute("SELECT payload_json FROM events WHERE transaction_id='changed' AND kind='issue_observed'").fetchone()[0])
            revision = dict(db.execute('SELECT * FROM issue_revisions WHERE id=?',(result['revision_id'],)).fetchone())
        for value in (payload,event):
            self.assertEqual(value['revision_id'], revision['id'])
            self.assertEqual(value['content_hash'], manifest_hash([facts['title'],facts['body']]))
            self.assertEqual(value['requirements_hash'], manifest_hash(facts['requirements']))
            self.assertEqual(value['labels'], ['ready'])
            self.assertEqual(value['state'], 'open')
            self.assertFalse({'title','body','requirements'} & value.keys())
            self.assertLess(len(json.dumps(value)), 1000)
        self.assertEqual(revision['body'], facts['body'])
        self.life.observe_issue(self.command('later'), '1:1', **{**facts,'title':'Later title','generation':2})
        count = self.counts()
        self.assertEqual(self.life.observe_issue(command, '1:1', **facts), result)
        self.assertEqual(self.counts(), count)

    def test_retained_full_text_decision_replays_without_rewriting_history(self):
        command = self.command('old')
        facts = {**self.facts,'title':'Old format change'}
        result = self.life.observe_issue(command,'1:1',**facts)
        legacy = dict(issue_id='1:1', **facts, state_reason=None, labels=[], assignees=[])
        legacy['remote_updated_at'] = self.life._remote_time(legacy['remote_updated_at'])
        raw = json.dumps(legacy,sort_keys=True,separators=(',',':'))
        with self.store.write() as db:
            db.execute("UPDATE decisions SET payload_json=? WHERE request_id='old'",(raw,))
        before = self.counts()
        self.assertEqual(self.life.observe_issue(command,'1:1',**facts),result)
        with self.assertRaises(IdempotencyConflict):
            self.life.observe_issue(command,'1:1',**{**facts,'requirements':'different'})
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT payload_json FROM decisions WHERE request_id='old'").fetchone()[0],raw)
        self.assertEqual(self.counts(),before)

    def test_same_facts_with_changed_gate_authority_still_journal(self):
        self.life.observe_issue(self.command('initial'),'1:1',**self.facts)
        blocked = self.life.observe_issue(self.command('block'),'1:1',**self.facts,admission=False)
        self.assertFalse(blocked['unchanged']); before=self.counts()
        self.assertTrue(self.life.observe_issue(self.command('repeat'),'1:1',**self.facts,admission=False)['unchanged'])
        self.assertEqual(self.counts(),before)
        self.assertFalse(self.life.observe_issue(self.command('resolve'),'1:1',**self.facts,admission=True)['unchanged'])
        self.assertGreater(self.counts()[1],before[1])

    def readiness(self, command, *, allowed):
        with self.store.read_snapshot() as db:
            authority = dict(domain='live:factory',scope='live',
                revision=read_revision(db,domain='live:factory',scope='live',repository_id=1))
        return self.life.observe_repository_readiness(command,repository_id=1,base_branch='main',
            allowed=allowed,access_allowed=True,reason='ready' if allowed else 'protection_needs_setup',
            source_revision='stable-source',read_authority=authority,valid_until=1000)

    def test_readiness_only_journals_gate_changes_and_replays_those_changes(self):
        before=self.counts()
        self.assertTrue(self.readiness(self.command('ready'),allowed=True)['unchanged'])
        self.assertEqual(self.counts(),before)
        command=self.command('blocked'); blocked=self.readiness(command,allowed=False)
        count=self.counts();self.assertFalse(blocked['unchanged'])
        self.assertTrue(self.readiness(self.command('same-block'),allowed=False)['unchanged'])
        self.assertEqual(self.counts(),count)
        self.assertFalse(self.readiness(self.command('resolve'),allowed=True)['unchanged'])
        count=self.counts()
        self.assertEqual(self.readiness(command,allowed=False),blocked)
        self.assertEqual(self.counts(),count)

    def test_rejected_observation_keeps_compact_exact_replay(self):
        self.life.observe_issue(self.command('initial'),'1:1',**self.facts)
        command=self.command('conflict'); facts={**self.facts,'title':'conflicting ordering'}
        with self.assertRaisesRegex(TransitionError,'conflicting observations'):
            self.life.observe_issue(command,'1:1',**facts)
        # Another request can later persist the same content. Its revision ID
        # must not change the identity/result of this retained rejection.
        self.life.observe_issue(self.command('accepted-later'),'1:1',**{**facts,'generation':2})
        with self.assertRaisesRegex(TransitionError,'conflicting observations'):
            self.life.observe_issue(command,'1:1',**facts)
        with self.store.read_snapshot() as db:
            rows=db.execute("SELECT payload_json FROM decisions WHERE request_id='conflict'").fetchall()
            self.assertEqual(len(rows),1)
            self.assertNotIn('conflicting ordering',rows[0][0])

    def test_untracked_inventory_timestamp_only_updates_freshness_without_event(self):
        inventory=StoreInventory(self.store,None,None,config={'hold_labels':['hold']},clock=lambda:self.now)
        remote=dict(number=2,state='open',title='Inventory',body='Text',labels=[],assignees=[],
                    updated_at='2026-10-10T00:00:00Z')
        repo=dict(id=1,name='owner/repo')
        inventory.record(repo,remote)
        before=self.counts();self.now+=10
        inventory.record(repo,{**remote,'updated_at':'2026-10-10T00:00:01Z'})
        self.assertEqual(self.counts(),before)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute("SELECT observed_at FROM issues WHERE id='1:2'").fetchone()[0],110)
            event=json.loads(db.execute("SELECT payload_json FROM events WHERE issue_id='1:2'").fetchone()[0])
            self.assertEqual(event['content_hash'],manifest_hash(['Inventory','Text']))
            self.assertEqual(event['labels'],[])
