"""Dependency cards preserve their exact scope and numeric page boundary."""
import tempfile
import unittest
from unittest.mock import Mock

from forge.factory.config import validate
from forge.factory.store_inventory import StoreInventory
from forge.factory.store_views import PipelineViews
from forge.store.db import Store


class PreviousDependencyJoin:
    """Run the same projection through the pre-optimization join for parity."""
    def __init__(self, db):
        self.db = db

    def execute(self, sql, *args):
        return self.db.execute(sql.replace('issue_dependencies d CROSS JOIN issues i',
                                           'issue_dependencies d JOIN issues i'), *args)

    def __getattr__(self, name):
        return getattr(self.db, name)


class DependencyCardQueryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.store = Store.create(temporary.name)
        self.inventory = StoreInventory(self.store, Mock(), Mock(), config=validate({}), clock=lambda: 100)
        self.views = PipelineViews(self.store, clock=lambda: 100)
        self.issue(1)

    def issue(self, number, repository=1, *, revised=False):
        self.inventory.record({'id': repository, 'name': f'example/repo-{repository}'},
            {'number': number, 'title': 'Revised' if revised else 'Work', 'body': '', 'state': 'open',
             'updated_at': '2026-10-10T00:01:00Z' if revised else '2026-10-10T00:00:00Z',
             'labels': [], 'assignees': []}, 'revised' if revised else 'original')
        with self.store.read_snapshot() as db:
            return db.execute('SELECT current_revision_id FROM issues WHERE id=?',
                              (f'{repository}:{number}',)).fetchone()[0]

    def edges(self, source, revision, targets):
        # The normalized historical edge table is also populated by migration;
        # fixture rows obey its real issue/revision foreign keys and uniqueness.
        with self.store.write() as db:
            db.executemany('INSERT INTO issue_dependencies VALUES(?,?,?,?,?,?,?)',
                [(f'{revision}:{target}', source, revision, target, 'requirements', 'Synthetic prerequisite', 100)
                 for target in targets])

    def assert_projection(self, expected):
        current = self.views.issues(key='1:1')
        self.assertEqual(current['issue']['dependencies'], expected)
        connect = self.store._connect_path
        self.store._connect_path = lambda *args, **kwargs: PreviousDependencyJoin(connect(*args, **kwargs))
        try:
            previous = self.views.issues(key='1:1')
        finally:
            self.store._connect_path = connect
        self.assertEqual(current, previous)

    def test_nonempty_edges_exclude_other_repositories_revisions_and_issues(self):
        old = self.issue(1)
        for number in (3, 4, 5, 8, 10): self.issue(number)
        self.issue(2, repository=2)
        self.edges('1:1', old, ['1:4'])
        current = self.issue(1, revised=True)
        self.edges('1:1', current, ['1:10', '2:2', '1:8', '1:3'])
        other = self.issue(3)
        self.edges('1:3', other, ['1:5'])
        self.assert_projection([3, 8, 10])

    def test_numeric_order_and_101_item_limit_survive_large_dependency_set(self):
        revision = self.issue(1)
        numbers = list(range(106, 1, -1))
        for number in numbers: self.issue(number)
        self.edges('1:1', revision, [f'1:{number}' for number in numbers])
        self.assert_projection(list(range(2, 103)))

    def test_empty_dependency_set_preserves_public_projection(self):
        self.assert_projection([])
