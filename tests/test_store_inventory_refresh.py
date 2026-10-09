from unittest.mock import Mock

from tests.test_sqlite_runtime_support import RuntimeFixture
from forge.factory.config import validate
from forge.factory.github import GitHubUnavailable
from forge.factory.store_inventory import StoreInventory


class InventoryRefreshTests(RuntimeFixture):
    def setUp(self):
        super().setUp()
        self.prepare()
        self.github = Mock()
        self.github.deadline = float('inf')
        self.github.call.return_value = []
        self.github.collaborators.return_value = [{'login': 'reviewer', 'name': 'Reviewer'}]
        self.github.recent_authors.return_value = ['author']
        self.github.pulls.return_value = [dict(number=2, title='Candidate', body='Closes #1',
            user={'login': 'builder[bot]'}, head={'ref': 'forge/issue-1-a'}, forge_owned=True)]
        self.inventory = StoreInventory(self.database, self.settings, self.github,
            config=validate({}), clock=lambda: self.now)
        self.remote = dict(number=1, title='Issue', body='Original', state='open',
            updated_at='2026-10-08T10:00:00Z', labels=[], assignees=[])

    def test_paged_refresh_persists_people_and_pulls_then_preserves_them_on_failure(self):
        self.inventory.refresh()
        self.inventory.refresh()
        with self.database.read_snapshot() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT login FROM inventory_collaborators ORDER BY login')], ['author', 'reviewer'])
            self.assertEqual(db.execute('SELECT github_number FROM inventory_pull_summaries').fetchone()[0], 2)
        self.now += 4000
        self.github.collaborators.side_effect = GitHubUnavailable('Unavailable')
        self.inventory.refresh()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM inventory_collaborators').fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM inventory_pull_summaries').fetchone()[0], 1)

    def test_requested_refresh_retries_and_preserves_new_request_racing_response(self):
        self.inventory.record(dict(id=10, name='example/project', enabled=True), self.remote)
        with self.database.write() as db:
            db.execute('INSERT INTO operator_observation_requests VALUES(?,?,?)', ('10:1', 'operator', self.now))
        self.github.issue.side_effect = GitHubUnavailable('Unavailable')
        self.inventory.requested()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operator_observation_requests').fetchone()[0], 1)
        def response(item):
            with self.database.write() as db:
                db.execute('UPDATE operator_observation_requests SET requested_at=requested_at+1')
            return {**self.remote, 'body': 'Fresh requirement'}, {'complete': True}
        self.github.issue.side_effect = response
        self.inventory.requested()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT requested_at FROM operator_observation_requests').fetchone()[0], self.now + 1)
            self.assertEqual(db.execute('SELECT body FROM issue_revisions WHERE id=(SELECT current_revision_id FROM issues WHERE id=?)', ('10:1',)).fetchone()[0], 'Fresh requirement')
        self.github.issue.side_effect = None
        self.github.issue.return_value = self.remote, {'complete': True}
        self.inventory.requested()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM operator_observation_requests').fetchone()[0], 0)

    def test_interrupted_imported_operator_intent_requires_explicit_queue(self):
        from forge.factory.store_inventory import DiscoveryController
        remote = {**self.remote, 'labels': ['ready']}
        self.inventory.record(dict(id=10, name='example/project', enabled=True), remote)
        with self.database.write() as db:
            db.execute("INSERT INTO issue_action_requests(id,issue_id,action,expected_revision,status,reason,created_at) VALUES('old-pause','10:1','pause','old','rejected','Keep this for later',1)")
            db.execute("INSERT INTO imported_operator_requests(request_id,source_manifest,source_status,interrupted_at) VALUES('old-pause','source','pending',2)")
        self.github.call.return_value = [remote]
        config = validate({'enabled': True, 'image': 'sha256:' + 'a' * 64})
        result = DiscoveryController(self.database, self.settings, self.github, config=config, clock=lambda: self.now).cycle()
        self.assertEqual(result['items'], [])
        self.github.issue.assert_not_called()
        with self.database.read_snapshot() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM units').fetchone()[0], 0)

    def test_hold_actor_refresh_is_cached_and_hidden_when_labels_change(self):
        from forge.factory.store_views import PipelineViews
        self.remote['labels']=[{'name':'hold'}]
        self.github.label_events.return_value=[{'label':'hold','login':'operator','at':'2026-10-08T10:00:00Z'}]
        self.inventory.record(self.settings.view()['repositories'][0],self.remote)
        views=PipelineViews(self.database,runtime=validate({}),clock=lambda:self.now)
        key=str(self.settings.view()['repositories'][0]['id'])+':1'
        self.assertEqual(views.issues(key=key)['issue']['held_by'][0]['login'],'operator')
        self.inventory.record(self.settings.view()['repositories'][0],self.remote)
        self.github.label_events.assert_called_once()
        self.remote.update(labels=[],updated_at='2026-10-08T10:01:00Z')
        self.inventory.record(self.settings.view()['repositories'][0],self.remote)
        self.assertIsNone(views.issues(key=key)['issue']['held_by'])

        self.remote.update(labels=[{'name':'hold'}],updated_at='2026-10-08T10:02:00Z')
        self.github.label_events.return_value=[{'label':'hold','login':'second-operator','at':'2026-10-08T10:02:00Z'}]
        self.inventory.record(self.settings.view()['repositories'][0],self.remote)
        self.assertEqual(views.issues(key=key)['issue']['held_by'][0]['login'],'second-operator')
        self.assertEqual(self.github.label_events.call_count,2)

    def test_inventory_and_discovery_changes_append_public_entity_events_once(self):
        repo = dict(id=10, name='example/project', enabled=True)
        self.inventory.record(repo, self.remote)
        def events():
            with self.database.read_snapshot() as db:
                return [dict(row) for row in db.execute("SELECT * FROM events WHERE issue_id='10:1' ORDER BY sequence")]
        first = events()
        self.assertEqual(first[-1]['kind'], 'issue_observed')
        self.inventory.record(repo, self.remote)
        self.assertEqual(events(), first)
        self.inventory.failure('10:1', 'requirements_stale')
        failed = events()
        self.assertEqual(len(failed), len(first)+1)
        self.inventory.failure('10:1', 'requirements_stale')
        self.assertEqual(events(), failed)
        self.inventory.clear_failure('10:1')
        self.assertEqual(len(events()), len(failed)+1)
        self.inventory.clear_failure('10:1')
        self.assertEqual(len(events()), len(failed)+1)
