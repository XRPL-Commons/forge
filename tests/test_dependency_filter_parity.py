"""Canonical #209 regression: canonical member progress must match SQL filters.

Canonical filtering must precede page limits.
"""
import unittest

from forge.factory.issue_selection import IssueSelection
from forge.factory.config import validate
from forge.factory.store_views import PipelineViews
from forge.store.delivery_inputs import current_output_in
from forge.store.scheduling import Scheduling
from tests import test_delivery_execution as fixtures


class DependencyFilterParityTests(unittest.TestCase):
    setUp = fixtures.DeliveryExecutionTests.setUp
    command = fixtures.DeliveryExecutionTests.command
    compose = fixtures.DeliveryExecutionTests.compose
    manifest = fixtures.DeliveryExecutionTests.manifest
    clean = fixtures.DeliveryExecutionTests.clean
    def schedule(self):
        # Store transitions invalidate the previous assignment's schedule. These
        # fixtures exercise queued filtering after the controller has recorded
        # its next scan, as the production dispatch loop does after a decline.
        Scheduling(self.store, clock=lambda:100).record('batch', owner='dispatch', reason='role_capacity')

    def plan(self):
        result = fixtures.DeliveryExecutionTests.plan(self)
        self.schedule()
        return result

    def child(self, *args):
        result = fixtures.DeliveryExecutionTests.child(self, *args)
        self.schedule()
        return result

    def accept(self, *args):
        result = fixtures.DeliveryExecutionTests.accept(self, *args)
        self.schedule()
        return result

    def prepare(self):
        self.compose(3, [dict(issue_id='issue-1', prerequisite_issue_id='issue-0'),
                         dict(issue_id='issue-2', prerequisite_issue_id='issue-1')])
        _, plan, _ = self.plan()
        self.views = PipelineViews(self.store, runtime=validate({}), clock=lambda:100)
        return plan

    def check(self, phase, expected):
        with self.store.read_snapshot() as db:
            index = {row['id']: row['public_status'] for row in IssueSelection(db).execute('SELECT id,public_status FROM public_issue_index')}
        for identifier, status in expected.items():
            canonical = self.views.issues(key=identifier)['issue']['status']
            with self.subTest(phase=phase, issue=identifier, source='canonical'):
                self.assertEqual(canonical, status)
            with self.subTest(phase=phase, issue=identifier, source='SQL filter'):
                self.assertEqual(index[identifier], canonical)
        for status in ('queued', 'blocked', 'waiting'):
            page = self.views.issues(state=status)
            with self.subTest(phase=phase, filter=status):
                self.assertTrue(all(row['status'] == status for row in page['issues']),
                                [(row['key'], row['status']) for row in page['issues']])

    def test_acceptance_and_invalidated_ancestor_agree_with_canonical_status(self):
        plan = self.prepare()
        self.check('no outputs', {'issue-0':'queued', 'issue-1':'blocked', 'issue-2':'blocked'})
        child, manifest = self.child(plan, '0'); self.accept(child, manifest, '0')
        self.check('first output accepted', {'issue-0':'queued', 'issue-1':'queued', 'issue-2':'blocked'})
        child, manifest = self.child(plan, '1'); self.accept(child, manifest, '1')
        self.check('transitive predecessor accepted', {'issue-0':'queued', 'issue-1':'queued', 'issue-2':'queued'})
        # A real operator-approved member change invalidates retained outputs.
        self.delivery.drop_member(self.command(), self.members['issue-0'], membership_generation=1,
            dependent_decisions={self.members['issue-1']:'wait_external'}, evidence={'approved':True})
        with self.store.read_snapshot() as db:
            unit = dict(db.execute("SELECT * FROM units WHERE id='batch'").fetchone())
            self.assertIsNone(current_output_in(db, unit, self.members['issue-1']))
            self.assertEqual(db.execute('SELECT COUNT(*) FROM part_output_invalidations').fetchone()[0], 2)
        self.check('ancestor removed and outputs invalidated', {'issue-0':'queued', 'issue-1':'blocked', 'issue-2':'blocked'})

    def test_unscheduled_members_are_waiting_in_both_projections(self):
        self.prepare()
        Scheduling(self.store).clear('batch', owner='dispatch')
        self.check('no scheduled controller', {'issue-0':'waiting', 'issue-1':'blocked', 'issue-2':'blocked'})
        self.assertEqual(self.views.issues(state='queued')['total'], 0)
        self.assertEqual(self.views.issues(state='waiting')['total'], 1)

    def test_accepted_dependencies_are_counted_before_page_limit(self):
        plan = self.prepare()
        for key in ('0', '1'):
            child, manifest = self.child(plan, key); self.accept(child, manifest, key)
        for number in range(4, 53):
            self.life.admit_issue(request_id=f'admit-extra-{number}', actor_id='operator', unit_id=f'unit-{number}',
                issue_id=f'extra-{number}', repository_id=1, repository_name='example/repo', issue_number=number,
                title='Independent work', body='Scope', requirements={'acceptance':['works']}, observed_at=90)
        all_rows = self.views.issues(state='all')['issues'] + self.views.issues(state='all', page=2)['issues']
        self.assertEqual(len(all_rows), 52)
        self.assertTrue(all(row['status'] == 'queued' for row in all_rows))
        first = self.views.issues(state='queued')
        second = self.views.issues(state='queued', page=2)
        with self.subTest(field='total'):
            self.assertEqual(first['total'], 52)
        with self.subTest(field='pages'):
            self.assertEqual(first['pages'], 2)
        with self.subTest(field='second page'):
            self.assertEqual(len(second['issues']), 2)

    def test_scoped_running_member_and_recovery_routing_match_cards(self):
        plan = self.prepare()
        for key in ('0', '1'):
            child, manifest = self.child(plan, key); self.accept(child, manifest, key)
        third, _ = self.child(plan, '2')
        self.check('independent scoped activity', {'issue-0':'queued', 'issue-1':'queued', 'issue-2':'working'})
        # A dependency-free sibling still shares the delivery's unit gate.
        self.life.pause(self.command())
        self.check('shared pause', {'issue-0':'backlog', 'issue-1':'backlog', 'issue-2':'backlog'})

    def test_dependency_clearing_reveals_recovery_before_board_limit(self):
        plan = self.prepare()
        child, manifest = self.child(plan, '0'); self.accept(child, manifest, '0')
        failed, inputs = self.child(plan, '1')
        self.life.record_failure(self.command(), failed['job_id'], execution_fence='fence-1',
            input_manifest=inputs, evidence={'reason':'checks failed'})
        self.clean(failed, 'fence-1')
        self.schedule()
        self.check('accepted dependency reveals recovery', {'issue-0':'queued', 'issue-1':'working', 'issue-2':'blocked'})
        runners = self.views.factory(section='runners')['issues']
        self.assertIn('issue-1', [row['key'] for row in runners])
        board = self.views.factory(section='board')['issues']
        self.assertNotIn('issue-1', [row['key'] for row in board])

    def test_scope_narrowing_does_not_classify_unrelated_issues_or_load_bodies(self):
        from unittest.mock import patch
        from forge.store.queries import Queries
        self.prepare()
        for repository_id in (1, 2):
            self.life.admit_issue(request_id=f'unrelated-{repository_id}', actor_id='operator', unit_id=f'unrelated-{repository_id}',
                issue_id=f'unrelated-{repository_id}', repository_id=repository_id,
                repository_name='example/repo' if repository_id==1 else 'example/other', issue_number=100,
                title='Independent work', body='private body', requirements={'acceptance':['works']}, observed_at=90)
        with self.store.read_snapshot() as db, patch.object(Queries, '_issue_contexts', wraps=Queries._issue_contexts) as facts:
            unrelated = IssueSelection(db, repository_id=2)
            self.assertEqual(unrelated.parameters, ())
            facts.assert_not_called()
            narrowed = IssueSelection(db, repository_id=1, query='#2')
            self.assertEqual(set(narrowed._contexts), {'issue-1'})
            self.assertEqual(facts.call_args.args[1], ['issue-1'])
            self.assertNotIn('body', narrowed._contexts['issue-1'][0])
            self.assertNotIn('requirements', narrowed._contexts['issue-1'][0])
            all_dependent = IssueSelection(db, repository_id=1)
            self.assertEqual(set(all_dependent._contexts), {'issue-0','issue-1','issue-2'})

    def test_completed_external_prerequisite_clears_without_a_stale_dependency_check(self):
        plan = self.prepare()
        for key in ('0', '1'):
            child, manifest = self.child(plan, key); self.accept(child, manifest, key)
        self.life.admit_issue(request_id='external', actor_id='operator', unit_id='external',
            issue_id='external', repository_id=2, repository_name='example/other', issue_number=1,
            title='External work', body='Scope', requirements={'acceptance':['works']}, observed_at=90)
        with self.store.write() as db:
            revision = db.execute('SELECT requirements_revision_id FROM unit_issues WHERE id=?', (self.members['issue-1'],)).fetchone()[0]
            db.execute("INSERT INTO issue_dependencies VALUES('external-edge','issue-1',?,'external','requirements','Requires external work',100)", (revision,))
        self.check('external prerequisite open', {'issue-0':'queued', 'issue-1':'blocked', 'issue-2':'queued'})
        self.life.observe_issue(self.command('external'), 'external', state='closed', state_reason='completed',
            title='External work', body='Scope', requirements={'acceptance':['works']}, generation=1,
            remote_updated_at='2026-10-10T00:00:01Z')
        self.check('external prerequisite completed', {'issue-0':'queued', 'issue-1':'queued', 'issue-2':'queued'})
        selected = self.views.issues(repo_id=1, state='queued')
        self.assertEqual(selected['total'], 3)

    def test_approved_group_without_edges_scopes_active_child_before_filtering(self):
        self.compose(3)
        _, plan, _ = self.plan()
        self.views = PipelineViews(self.store, runtime=validate({}), clock=lambda:100)
        self.child(plan, '0')
        self.check('zero-edge group child activity', {'issue-0':'working', 'issue-1':'queued', 'issue-2':'queued'})
        working = self.views.issues(state='working')
        self.assertEqual(working['total'], 1)
        self.assertEqual([row['key'] for row in working['issues']], ['issue-0'])
        queued = self.views.issues(state='queued')
        self.assertEqual(queued['total'], 2)
        self.assertEqual([row['key'] for row in queued['issues']], ['issue-1','issue-2'])
