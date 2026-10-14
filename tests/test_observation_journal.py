"""Observations journal only real changes, and journals carry hashes, not issue text (#281)."""
import json
import tempfile
import unittest

from forge.store.db import Store
from forge.store.models import Command
from forge.store.transitions import Lifecycle, manifest_hash

TITLE, BODY = 'A distinctive issue title', 'A distinctive issue body ' * 40


class ObservationJournalTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = Store.create(temporary.name); self.now = 100.0
        self.life = Lifecycle(self.store, clock=lambda: self.now)
        self.life.admit_issue(request_id='admit', actor_id='operator', unit_id='unit', issue_id='1:1',
            repository_id=1, repository_name='example/project', issue_number=1, title=TITLE, body=BODY,
            requirements='digest', observed_at=self.now)
        self.generation = 0

    def counts(self):
        with self.store.read_snapshot() as db:
            return (db.execute('SELECT count(*) FROM decisions').fetchone()[0],
                    db.execute('SELECT count(*) FROM events').fetchone()[0])

    def observe(self, request, **changes):
        self.generation += 1
        facts = dict(state='open', title=TITLE, body=BODY, requirements='digest',
                     remote_updated_at='2026-10-10T00:00:00Z', generation=self.generation)
        facts.update(changes)
        with self.store.read_snapshot() as db:
            version = db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0]
        return self.life.observe_issue(Command(request, 'supervisor:inventory', 'unit', version), '1:1', **facts)

    def issue(self):
        with self.store.read_snapshot() as db:
            return dict(db.execute("SELECT * FROM issues WHERE id='1:1'").fetchone())

    def test_unchanged_observation_writes_no_decision_or_event_but_renews_freshness(self):
        before = self.counts()
        self.now += 30
        result = self.observe('noop-1', remote_updated_at='2026-10-10T00:00:05Z')
        self.assertTrue(result['unchanged'])
        self.assertEqual(self.counts(), before)
        issue = self.issue()
        # Freshness and ordering still advance on the entity row.
        self.assertEqual(issue['observed_at'], self.now)
        self.assertEqual(issue['collection_generation'], 1)
        # The same request is safe to retry and stays a no-op.
        self.assertTrue(self.observe('noop-1', remote_updated_at='2026-10-10T00:00:05Z')['unchanged'])
        self.assertEqual(self.counts(), before)

    def test_stale_observation_is_not_journalled(self):
        self.observe('newer', remote_updated_at='2026-10-11T00:00:00Z', labels=['bug'])
        before = self.counts()
        self.assertTrue(self.observe('older', remote_updated_at='2026-10-09T00:00:00Z')['stale'])
        self.assertEqual(self.counts(), before)

    def test_changed_observation_still_journals_and_replays_idempotently(self):
        before = self.counts()
        self.now += 1
        first = self.observe('change', labels=['hold'], remote_updated_at='2026-10-10T00:01:00Z')
        self.assertFalse(first['unchanged'])
        after = self.counts()
        self.assertEqual(after[0], before[0] + 1)
        self.assertGreater(after[1], before[1])
        with self.store.read_snapshot() as db:
            decision = db.execute("SELECT * FROM decisions WHERE request_id='change'").fetchone()
        self.assertEqual(decision['status'], 'applied')
        # Exact replay of the applied command returns the stored result.
        command = Command('change', 'supervisor:inventory', 'unit', decision['expected_version'])
        replay = self.life.observe_issue(command, '1:1', state='open', title=TITLE, body=BODY, requirements='digest',
                                         remote_updated_at='2026-10-10T00:01:00Z', generation=self.generation,
                                         labels=['hold'])
        self.assertEqual(replay, first)
        self.assertEqual(self.counts(), after)

    def test_journal_payloads_carry_hashes_not_issue_text(self):
        self.observe('edit', title=TITLE + ' (edited)', body=BODY + ' more', labels=['bug'],
                     remote_updated_at='2026-10-10T00:02:00Z')
        with self.store.read_snapshot() as db:
            decision = db.execute("SELECT payload_json FROM decisions WHERE command='observe_issue'").fetchone()[0]
            event = db.execute("SELECT payload_json FROM events WHERE kind='issue_observed'").fetchone()[0]
            revision = db.execute("SELECT i.current_revision_id,r.title,r.content_hash FROM issues i "
                                  "JOIN issue_revisions r ON r.id=i.current_revision_id WHERE i.id='1:1'").fetchone()
            observed = [row[0] for row in db.execute(
                "SELECT payload_json FROM events WHERE kind IN ('issue_observed','decision_applied')")]
        for text in (decision, event, *observed):
            self.assertNotIn('distinctive', text)
            self.assertNotIn('"title"', text); self.assertNotIn('"body"', text)
        event = json.loads(event)
        self.assertEqual(event['revision_id'], revision['current_revision_id'])
        self.assertEqual(event['content_hash'], revision['content_hash'])
        self.assertEqual(event['content_hash'], manifest_hash([TITLE + ' (edited)', BODY + ' more']))
        self.assertEqual(event['labels'], ['bug'])
        self.assertEqual(event['state'], 'open')
        # The text itself is stored once, in the revision.
        self.assertEqual(revision['title'], TITLE + ' (edited)')


    def test_unchanged_dependency_observation_is_not_journalled(self):
        from forge.store.runtime_requirements import RuntimeRequirements
        requirements = RuntimeRequirements(self.store, clock=lambda: self.now)
        with self.store.read_snapshot() as db:
            revision = db.execute("SELECT requirements_revision_id FROM unit_issues WHERE unit_id='unit'").fetchone()[0]
        def observe(generation):
            with self.store.read_snapshot() as db:
                version = db.execute("SELECT version FROM units WHERE id='unit'").fetchone()[0]
            return requirements.dependencies(Command(f'deps-{generation}', 'supervisor', 'unit', version),
                                             requirements_revision_id=revision, generation=generation, facts=[])
        before = self.counts()
        observe(1)
        first = self.counts()
        self.assertGreater(first[0], before[0])
        self.now += 60
        self.assertTrue(observe(2)['clear'])
        self.assertEqual(self.counts(), first)
        with self.store.read_snapshot() as db:
            # Freshness: the newest check generation is still recorded.
            self.assertEqual(db.execute('SELECT max(generation) FROM dependency_checks').fetchone()[0], 2)

if __name__ == '__main__':
    unittest.main()
