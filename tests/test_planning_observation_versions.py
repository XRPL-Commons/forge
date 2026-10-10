"""Observation freshness cannot revoke unchanged operator proposal inputs."""
import tempfile
import unittest
from copy import deepcopy
from unittest.mock import Mock

from forge.factory.planning_service import PlanningService
from forge.factory.store_inventory import StoreInventory
from forge.store.db import Store
from forge.store.delivery import Delivery
from forge.store.models import Command, VersionConflict
from forge.store.repository_planning import RepositoryPlanning, pin_in
from forge.store.transitions import Lifecycle


class PlanningObservationVersionsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = temporary.name; self.store = Store.create(self.root); self.now = 100
        self.config = {'hold_labels': ['hold'], 'ready_label': 'ready'}
        self.life = Lifecycle(self.store, clock=lambda: self.now)
        self.plans = RepositoryPlanning(self.store, clock=lambda: self.now)
        self.github = Mock()
        self.inventory = StoreInventory(self.store, Mock(), self.github, config=self.config, clock=lambda: self.now)
        self.repo = {'id': 1, 'name': 'example/project'}
        self.remote = {'number': 1, 'state': 'open', 'state_reason': None, 'title': 'Work', 'body': 'Details',
                       'updated_at': '2026-10-10T00:00:00Z', 'labels': [], 'assignees': []}

    def pin(self, number=1):
        with self.store.read_snapshot() as db:
            return pin_in(db, f'1:{number}')

    def admit(self, number=1):
        self.life.admit_issue(request_id=f'admit-{number}', actor_id='operator', unit_id=f'unit-{number}',
            issue_id=f'1:{number}', repository_id=1, repository_name=self.repo['name'], issue_number=number,
            title='Work', body='Details', requirements='digest', observed_at=self.now)
        self.observe(number=number, request=f'initial-{number}')

    def observe(self, *, number=1, generation=1, request='observe', **changes):
        facts = dict(state='open', title='Work', body='Details', requirements='digest',
                     remote_updated_at=self.remote['updated_at'], generation=generation)
        facts.update(changes)
        return self.life.observe_issue(Command(request, 'supervisor', f'unit-{number}', self.pin(number)['unit_version']),
                                       f'1:{number}', **facts)

    def proposal(self, *, group=False):
        self.plans.begin(1, proposal_id='proposal', request_id='begin', actor_id='planner', base_branch='main')
        members = ['1:1', '1:2'] if group else ['1:1']
        parameters = {'dependencies': [], 'existing_prs': [], 'publication_target': {'kind': 'new'}} if group else {}
        self.plans.publish('proposal', expected_version=1, request_id='publish', actor_id='planner',
            rationale='Useful work', rows=[dict(action='group' if group else 'keep', members=members,
                                                reason='Useful work', rule_quote=None, parameters=parameters)])
        return self.plans.proposal('proposal')['rows'][0]['id']

    def accept(self, row):
        return self.plans.accept('proposal', expected_version=2, accept_rows=[row], reject_rows=[],
            request_id='accept', actor_id='operator', config=self.config,
            compose_in=Delivery(self.store, clock=lambda: self.now).compose_in)

    def test_owned_new_generation_same_order_and_stale_scan_preserve_keep_pin(self):
        self.admit(); row = self.proposal(); before = self.pin(); self.now += 1
        for request, generation in [('new', 2), ('same', 2), ('stale', 1)]:
            with self.subTest(request=request):
                first = self.observe(generation=generation, request=request)
                self.assertEqual(self.pin(), before)
                self.assertEqual(self.observe(generation=generation, request=request), first)
                self.now += 1
        with self.store.read_snapshot() as db:
            issue = db.execute('SELECT collection_generation,observed_at FROM issues WHERE id=?', ('1:1',)).fetchone()
            self.assertEqual(tuple(issue), (2, 101))
            self.assertEqual(db.execute("SELECT count(*) FROM decisions WHERE command='observe_issue'").fetchone()[0], 4)
        self.assertEqual(self.accept(row)['status'], 'accepted')

    def test_group_approval_keeps_exact_pins_across_background_observations(self):
        self.admit(); self.admit(2); row = self.proposal(group=True)
        before = [self.pin(n) for n in (1, 2)]
        for n in (1, 2):
            self.observe(number=n, generation=2, request=f'background-{n}')
        self.assertEqual([self.pin(n) for n in (1, 2)], before)
        result = self.accept(row)
        self.assertEqual(len(result['units']), 1)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM unit_issues WHERE unit_id=? AND left_at IS NULL',
                                        (result['units'][0],)).fetchone()[0], 2)

    def test_actual_issue_changes_still_invalidate_pins(self):
        self.admit(); row = self.proposal(); before = self.pin()
        self.observe(generation=2, title='Changed requirement', requirements='changed')
        self.assertEqual(self.pin()['issue_version'], before['issue_version'] + 1)
        self.assertEqual(self.pin()['unit_version'], before['unit_version'] + 1)
        with self.assertRaises(VersionConflict): self.accept(row)

    def test_policy_gate_changes_invalidate_even_same_issue_order(self):
        self.admit(); row = self.proposal(); before = self.pin()
        self.observe(admission=False, ownership_conflict=True)
        blocked = self.pin()
        self.assertEqual(blocked['issue_version'], before['issue_version'])
        self.assertEqual(blocked['unit_version'], before['unit_version'] + 1)
        with self.assertRaises(VersionConflict): self.accept(row)
        self.observe(request='clear-gates', admission=True, ownership_conflict=False)
        self.assertEqual(self.pin()['unit_version'], blocked['unit_version'] + 1)
        self.observe(request='already-clear', admission=True, ownership_conflict=False)
        self.assertEqual(self.pin()['unit_version'], blocked['unit_version'] + 1)

    def test_hold_policy_change_with_identical_issue_facts_invalidates_pin(self):
        self.admit(); self.observe(generation=2, request='label-only', labels=['hold'], hold_labels=[])
        row = self.proposal(); before = self.pin()
        self.observe(generation=2, request='enforce-hold', labels=['hold'], hold_labels=['hold'])
        self.assertEqual(self.pin()['issue_version'], before['issue_version'])
        self.assertEqual(self.pin()['unit_version'], before['unit_version'] + 1)
        with self.assertRaises(VersionConflict): self.accept(row)

    def test_historical_identical_partial_listing_keeps_last_complete_evidence(self):
        self.admit()
        self.life.close_unit(Command('close', 'operator', 'unit-1', self.pin()['unit_version']),
                             close_code='operator_closed', evidence={'approved': True})
        before = self.pin(); self.now += 1000
        self.inventory.record(self.repo, self.remote)
        self.assertEqual(self.pin(), before)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT observed_at FROM issues WHERE id=?', ('1:1',)).fetchone()[0], 100)

    def test_other_commands_still_invalidate_and_enforce_cas(self):
        self.admit(); row = self.proposal(); version = self.pin()['unit_version']
        self.observe(generation=2)
        self.life.pause(Command('pause', 'operator', 'unit-1', version))
        with self.assertRaises(VersionConflict):
            self.life.pause(Command('stale-pause', 'operator', 'unit-1', version))
        with self.assertRaises(VersionConflict): self.accept(row)

    def test_unowned_identical_full_reads_keep_pin_and_accept_fresh_github_proof(self):
        self.inventory.record(self.repo, self.remote, 'digest'); row = self.proposal(); before = self.pin()
        self.now += 1; self.inventory.record(self.repo, self.remote, 'digest')
        self.assertEqual(self.pin(), before)
        self.github.issue.return_value = (deepcopy(self.remote), 'digest')
        service = PlanningService(self.root, actor_id='operator', clock=lambda: self.now,
            parts={'store': self.store, 'github': self.github, 'config': self.config})
        result = service.action(1, dict(action='accept', proposal_id='proposal', expected_version=2,
                                       request_id='accept', accept_rows=[row], reject_rows=[]))
        self.assertEqual(result['status'], 'accepted'); self.assertEqual(self.pin(), before)
        self.github.issue.assert_called_once()

    def test_partial_listing_preserves_complete_revision_without_refreshing_evidence(self):
        self.inventory.record(self.repo, self.remote, 'digest'); row = self.proposal(); before = self.pin()
        self.now += 1000; self.inventory.record(self.repo, self.remote)
        self.assertEqual(self.pin(), before)
        with self.store.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT observed_at FROM issues WHERE id=?', ('1:1',)).fetchone()[0], 100)
        from forge.store.models import TransitionError
        with self.assertRaises(TransitionError): self.accept(row)
        # A real complete refresh is still authoritative even when only external
        # repository requirements changed, without an issue timestamp change.
        self.inventory.record(self.repo, self.remote, 'changed-repository-requirements')
        self.assertEqual(self.pin()['issue_version'], before['issue_version'] + 1)
        with self.assertRaises(VersionConflict): self.accept(row)

    def test_changed_listing_timestamp_requires_new_complete_evidence(self):
        self.inventory.record(self.repo, self.remote, 'digest'); before = self.pin()
        self.inventory.record(self.repo, {**self.remote, 'updated_at': '2026-10-10T00:01:00Z'})
        self.assertNotEqual(self.pin()['requirements_revision_id'], before['requirements_revision_id'])
        self.assertEqual(self.pin()['issue_version'], before['issue_version'] + 1)

    def test_unowned_reopened_repeat_does_not_invent_reopening_version(self):
        self.inventory.record(self.repo, {**self.remote, 'state': 'closed', 'state_reason': 'completed'}, 'digest')
        reopened = {**self.remote, 'state_reason': 'reopened'}
        self.inventory.record(self.repo, reopened, 'digest')
        with self.store.read_snapshot() as db:
            before = tuple(db.execute('SELECT version,reopened_version FROM issues WHERE id=?', ('1:1',)).fetchone())
        self.inventory.record(self.repo, reopened, 'digest')
        with self.store.read_snapshot() as db:
            self.assertEqual(tuple(db.execute('SELECT version,reopened_version FROM issues WHERE id=?', ('1:1',)).fetchone()), before)
