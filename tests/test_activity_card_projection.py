"""Activity owns its read fields without changing the public card contract."""
import json
import unittest
from unittest.mock import patch

from forge.factory.config import validate
from forge.factory.store_views import PipelineViews
from tests import test_completed_selection as completed
from tests import test_store_completion as lifecycle
from tests import test_store_public_parity as imported


class RichIssueCards(PipelineViews):
    """The original Activity path remains available through issue detail reads."""
    def _activity_row(self, db, issue_id):
        return self._row(db, issue_id, config=self._config())


def assert_views(test, store, *, keys=(), clock=1000):
    current = PipelineViews(store, runtime=validate({}), clock=lambda: clock)
    previous = RichIssueCards(store, runtime=validate({}), clock=lambda: clock)
    for section in ('board', 'runners', 'completed'):
        before = None
        while True:
            options = dict(section=section)
            if section == 'completed': options.update(limit=2, before=before)
            with test.subTest(section=section, before=before):
                actual = current.factory(**options)
                test.assertEqual(actual, previous.factory(**options))
            before = actual.get('next') if section == 'completed' else None
            if before is None: break
    for key in keys:
        with test.subTest(issue=key):
            test.assertEqual(current.factory(issue=key), previous.factory(issue=key))
    return current


class CompletedActivityParityTests(unittest.TestCase):
    setUp = completed.CompletedSelectionTests.setUp

    def test_full_envelopes_include_former_members_reopened_and_external_closures(self):
        assert_views(self, self.store, keys=['1:'+str(number) for number in range(1, 14)])

    def test_card_reads_do_not_fetch_discarded_issue_editing_collections(self):
        statements = []
        original = self.store._connect_path
        def connect(*args, **kwargs):
            db = original(*args, **kwargs)
            db.set_trace_callback(statements.append)
            return db
        with patch.object(self.store, '_connect_path', side_effect=connect):
            PipelineViews(self.store, clock=lambda:1000).factory(section='completed')
        reads = '\n'.join(sql.lower() for sql in statements if sql.lstrip().upper().startswith('SELECT'))
        for table in ('issue_label_attributions', 'issue_environment_observations',
                      'revision_display_dependencies', 'inventory_pull_issue_links', 'unit_publication_targets'):
            self.assertNotIn(table, reads)
        self.assertNotIn('requirements_json', reads)
        self.assertNotIn('r.body', reads)
        # The envelope still reads the bounded action list; no per-card pending-action query.
        self.assertNotIn("issue_action_requests where issue_id=", reads)


class LifecycleActivityParityTests(unittest.TestCase):
    setUp = lifecycle.StoreCompletionTests.setUp
    admit = lifecycle.StoreCompletionTests.admit
    view = lifecycle.StoreCompletionTests.view
    command = lifecycle.StoreCompletionTests.command
    reserve = lifecycle.StoreCompletionTests.reserve
    cleanup = lifecycle.StoreCompletionTests.cleanup
    fail_job = lifecycle.StoreCompletionTests.fail
    observe = lifecycle.StoreCompletionTests.observe
    candidate = lifecycle.StoreCompletionTests.candidate
    adopt = lifecycle.StoreCompletionTests.adopt
    merge = lifecycle.StoreCompletionTests.merge

    def check(self):
        return assert_views(self, self.store, keys=['issue-1'], clock=100)

    def test_running_worker_then_failure_cleanup_and_independent_holds(self):
        self.check()
        job = self.reserve()
        views = self.check()
        self.assertEqual(views.factory(issue='issue-1')['issues'][0]['active'], job['job_id'])
        self.fail_job(job)
        self.check()
        self.cleanup(job)
        self.lifecycle.open_gate(self.command(), 'dependency', member_id=self.member,
            details={'blocked':[{'reference':'owner/repository#7','reason':'prerequisite_open'}]})
        self.observe(labels=['hold'])
        self.lifecycle.pause(self.command())
        views = self.check()
        card = views.factory(issue='issue-1')['issues'][0]
        self.assertEqual(card['failure_count'], 1)
        self.assertEqual(card['status'], 'backlog')
        self.assertIsNone(card['active'])
        self.observe(labels=[], generation=2)
        self.check()

    def test_candidate_verification_and_merge_preserve_exact_card_identity(self):
        candidate = self.adopt()['candidate_id']
        self.lifecycle.open_gate(self.command(), 'verification_human', candidate_id=candidate,
                                 details={'instructions':'Check the exact preview'})
        views = self.check()
        self.assertEqual(views.factory(issue='issue-1')['issues'][0]['candidate']['pr'], 10)
        self.merge(candidate)
        views = self.check()
        card = views.factory(issue='issue-1')['issues'][0]
        self.assertEqual(card['candidate']['merge_commit'], 'merge-sha')
        self.assertIsNone(card['closed_as'])
        self.observe(state='open', reason='reopened')
        self.check()


class ImportedActivityParityTests(unittest.TestCase):
    setUp = imported.PublicParityTests.setUp
    write = imported.PublicParityTests.write
    snapshot = imported.PublicParityTests.snapshot
    prepared = imported.PublicParityTests.prepared

    def test_historical_candidate_summary_dependencies_reviewers_and_deployment(self):
        self.issue.update(state='merged', last_observed=18,
            last_summary={'headline':'Delivered the change','plain':'Historical worker summary.'},
            blocked_dependencies=[7], dependency_reason='Historical prerequisite',
            reviewers_requested={'logins':['reviewer'],'at':15,'request':'old-request'})
        self.issue['candidate']=dict(pr=12,head='head',base='base',policy='policy',
            requirements='requirements-hash',key='identity',build_attempt='build',accepted_at=12,branch='work')
        self.issue['evidence']={'merge':{'pr':12,'sha':'commit'}}
        self.issue['github_observation']={'at':20,'issue':{'number':1,'title':'Change','body':'',
            'state':'closed','state_reason':'completed','labels':[],'assignees':[]}}
        self.ledger['jobs']['build']=dict(id='build',issue_key='1:1',role='build',cleanup=True,
            fenced=True,slot='slot',created_at=11,phase='cleaned',outcome='accepted',branch='work',target_branch='main')
        self.ledger['events'].append({'sequence':2,'at':17,'kind':'merged','issue':'1:1'})
        _, store = self.prepared()
        with store.write() as db:
            candidate = db.execute('SELECT id FROM candidates').fetchone()[0]
            db.execute("INSERT INTO deployment_observations(id,repository_id,environment,candidate_id,commit_oid,status,observed_at,details_json) VALUES(?,1,'production',?,?,?,?,?)",
                       ('failed',candidate,'commit','failure',22,json.dumps({'run_url':'https://example.test/failed'})))
            db.execute("INSERT INTO deployment_observations(id,repository_id,environment,candidate_id,commit_oid,status,observed_at,details_json) VALUES(?,1,'production',?,?,?,?,?)",
                       ('deployed',candidate,'commit','success',21,json.dumps({'via':'github'})))
        views = assert_views(self, store, keys=['1:1'], clock=25)
        card = views.factory(section='completed')['issues'][0]
        self.assertEqual(card['last_summary'], self.issue['last_summary'])
        self.assertEqual(card['reviewers_requested']['logins'], ['reviewer'])
        self.assertEqual(card['deployed']['at'], 21)
        self.assertIsNone(card['deploy_failed'])
        self.assertEqual(card['completed_at'], 17)
        self.assertEqual(card['blocked_dependencies'], [7])

    def test_legacy_reconciliation_hold_preserves_historical_observation_timestamp(self):
        self.issue.update(state='hold', last_observed=18,
                          last_summary={'headline':'Waiting','plain':'Keep the imported decision.'})
        _, store = self.prepared()
        views = assert_views(self, store, keys=['1:1'], clock=25)
        card = views.factory(issue='1:1')['issues'][0]
        with store.read_snapshot() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM unit_gates WHERE code='legacy_reconciliation' AND resolved_at IS NULL").fetchone())
        self.assertEqual(card['last_observed'], 18)
        self.assertEqual(card['last_summary'], self.issue['last_summary'])
