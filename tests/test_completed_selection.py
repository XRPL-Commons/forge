"""Completed selection narrows candidates while canonical lifecycle owns matches."""
import tempfile
import unittest
from unittest.mock import Mock
from forge.store.db import Store
from forge.store.transitions import Lifecycle, manifest_hash
from forge.store.delivery import Delivery
from forge.store.models import Command, Candidate
from forge.factory.config import validate
from forge.factory.store_inventory import StoreInventory
from forge.factory.store_views import PipelineViews, COMPLETED_MEMBERS_SQL, COMPLETED_SOURCE_SQL


class BroadCanonicalConnection:
    """Compare public responses against the original unrestricted index input."""
    def __init__(self, connection):
        self.connection = connection
    def execute(self, sql, *args):
        if sql.startswith(COMPLETED_MEMBERS_SQL):
            sql = sql[len(COMPLETED_MEMBERS_SQL):].replace(COMPLETED_SOURCE_SQL, 'public_issue_index x')
        return self.connection.execute(sql, *args)
    def __getattr__(self, key):
        return getattr(self.connection, key)


class CompletedSelectionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        store = Store.create(self.directory.name)
        now, serial = [100], [0]
        life = Lifecycle(store, clock=lambda: now[0])
        delivery = Delivery(store, clock=lambda: now[0])
        inventory = StoreInventory(store, Mock(), Mock(), config=validate({}), clock=lambda: now[0])

        def command(unit):
            serial[0] += 1
            now[0] += 1
            with store.read_snapshot() as db:
                version = db.execute('SELECT version FROM units WHERE id=?', (unit,)).fetchone()[0]
            return Command('edge-' + str(serial[0]), 'fixture', unit, version)

        def admit(number):
            now[0] += 1
            unit, issue = 'u' + str(number), '1:' + str(number)
            result = life.admit_issue(request_id='admit-' + str(number), actor_id='fixture',
                unit_id=unit, issue_id=issue, repository_id=1, repository_name='example/repo',
                issue_number=number, title='Edge ' + str(number), body='', requirements='req', observed_at=now[0])
            return dict(issue_id=issue, issue_version=1, requirements_revision_id=result['revision_id'],
                        unit_id=unit, unit_version=1, member_id=result['member_id'])

        def observe(unit, number, state, reason, generation=1):
            life.observe_issue(command(unit), '1:' + str(number), state=state, state_reason=reason,
                title='Edge ' + str(number), body='', requirements='req', generation=generation,
                remote_updated_at='2026-10-10T01:00:' + str(generation).zfill(2) + 'Z')

        def close(unit):
            life.close_unit(command(unit), close_code='operator_closed', evidence={'approved': True})

        def group(unit, numbers):
            pins = [admit(number) for number in numbers]
            now[0] += 1
            proposal = dict(repository_id=1, base_branch='main', members=pins,
                            dependencies=[], existing_prs=[], rationale='Fixture')
            return delivery.compose(Command('compose-' + unit, 'fixture', unit, 1), **proposal,
                approval={'approved': True, 'composition_hash': delivery.composition_hash(**proposal)})

        def drop(unit, member, disposition):
            with store.read_snapshot() as db:
                generation = db.execute('SELECT membership_generation FROM units WHERE id=?', (unit,)).fetchone()[0]
            return delivery.drop_member(command(unit), member, membership_generation=generation,
                dependent_decisions={}, evidence={'approved': True}, disposition=disposition)

        # Closed managed outcomes, an unknown closure and a reopened terminal issue.
        for number, reason in [(1, 'completed'), (2, 'not_planned'), (3, 'duplicate'), (4, None)]:
            admit(number)
            observe('u' + str(number), number, 'closed', reason)
            close('u' + str(number))
        admit(5)
        observe('u5', 5, 'closed', 'completed')
        close('u5')
        observe('u5', 5, 'open', 'reopened', 2)
        # Closed untracked issues have no delivery and remain absent here.
        for number, reason in [(6, 'completed'), (7, 'not_planned')]:
            inventory.record({'id': 1, 'name': 'example/repo'},
                dict(number=number, state='closed', state_reason=reason, title='Untracked', body='',
                     updated_at='2026-10-10T01:00:00Z', labels=[], assignees=[]), 'req')
        # Removed closed member survives terminal group; released member moves to a live singleton.
        composed = group('batch', [8, 9, 10])
        members = {row['issue_id']: row['member_id'] for row in composed['members']}
        observe('batch', 8, 'closed', 'completed')
        drop('batch', members['1:8'], 'close')
        drop('batch', members['1:9'], 'release')
        observe('batch', 10, 'closed', 'not_planned')
        drop('batch', members['1:10'], 'close')
        # Dissolved old memberships cannot mask new active singleton memberships.
        group('split', [11, 12])
        delivery.dissolve(command('split'), evidence={'approved': True})
        # Merged then reopened is excluded despite a retained merged unit.
        admit(13)
        candidate = Candidate(13, 'forge/13', 'h13', 'base', 'policy', manifest_hash('req'), 'inputs')
        adopted = life.adopt_candidate(command('u13'), candidate, evidence=dict(approved=True, verified=True,
            pull_number=13, head_oid='h13', base_oid='base', policy_hash='policy', input_manifest_hash='inputs'))
        life.confirm_merged(command('u13'), adopted['candidate_id'], merge_sha='m13',
            evidence=dict(verified=True, merged=True, merge_sha='m13', pull_number=13, head_oid='h13', base_oid='base'))
        observe('u13', 13, 'open', 'reopened')
        self.store = store

    def pages(self):
        view = PipelineViews(self.store, runtime=validate({}), clock=lambda: 1000)
        result = []; before = None
        while True:
            page = view.factory(section='completed', limit=2, before=before)
            result.append(page)
            before = page['next']
            if before is None:
                return result

    def test_full_responses_preserve_former_membership_reopening_and_keysets(self):
        selected = self.pages()
        original = self.store._connect_path
        self.store._connect_path = lambda *args, **kwargs: BroadCanonicalConnection(original(*args, **kwargs))
        try:
            self.assertEqual(self.pages(), selected)
        finally:
            self.store._connect_path = original
        self.assertEqual([row['key'] for page in selected for row in page['issues']],
                         ['1:8', '1:10', '1:3', '1:2', '1:1'])
        self.assertEqual([page['summary']['completed_count'] for page in selected], [5, 5, 5])
        self.assertIsNone(selected[-1]['next'])
        with self.store.read_snapshot() as db:
            # Removed and settled memberships are essential to this seed.
            self.assertTrue(all(row[0] is not None for row in db.execute(
                "SELECT left_at FROM unit_issues WHERE issue_id='1:8'")))

    def test_actual_selection_and_count_use_terminal_and_issue_indexes(self):
        captured = []; original = self.store._connect_path
        class CaptureConnection:
            def __init__(self, connection): self.connection = connection
            def execute(self, sql, *args):
                if sql.startswith(COMPLETED_MEMBERS_SQL): captured.append((sql, args))
                return self.connection.execute(sql, *args)
            def __getattr__(self, key): return getattr(self.connection, key)
        self.store._connect_path = lambda *args, **kwargs: CaptureConnection(original(*args, **kwargs))
        try:
            PipelineViews(self.store, runtime=validate({}), clock=lambda: 1000).factory(section='completed')
        finally:
            self.store._connect_path = original
        self.assertEqual(len(captured), 2)
        with self.store.read_snapshot() as db:
            for sql, args in captured:
                details = [row['detail'] for row in db.execute('EXPLAIN QUERY PLAN '+sql, *args)]
                with self.subTest(sql=sql):
                    self.assertTrue(any('SEARCH terminal USING COVERING INDEX open_units' in detail for detail in details), details)
                    self.assertTrue(any('SEARCH m USING INDEX unit_members' in detail for detail in details), details)
                    self.assertFalse(any(detail.startswith('SCAN i ') for detail in details), details)
