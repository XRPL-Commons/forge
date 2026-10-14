"""Operator control of repository steward autonomy with a revision guard."""
from .steward_contract import require
from .store.configuration import Configuration
from .store.runtime import writable
from .store.steward_dossier import autonomy_in


class StewardPolicy:
    def __init__(self, settings, *, actor_id=None):
        self.settings, self.actor_id = settings, actor_id
        self.store = settings.authority.database
        require(self.store is not None, 'A selected store is required')

    def _repository(self, db, repo_id):
        require(type(repo_id) is int and repo_id > 0, 'Expected a repository identity')
        configured = next((repo for repo in self.settings.load()['repositories'] if repo['id'] == repo_id), None)
        row = db.execute('SELECT full_name FROM repositories WHERE id=?', (repo_id,)).fetchone()
        require(configured and row and configured['name'].lower() == row[0].lower(), 'Repository is not configured')
        return row[0]

    def view(self, repo_id):
        with self.store.read_snapshot() as db:
            name = self._repository(db, repo_id)
            return {'repo_id': repo_id, 'name': name, **autonomy_in(db, repo_id)}

    def set(self, *, repo_id, revision, policy):
        require(isinstance(self.actor_id, str) and self.actor_id.startswith('github:'), 'An authenticated operator is required')
        require(type(revision) is int and revision >= 0, 'Expected a policy revision')
        require(isinstance(policy, str) and policy in {'manual', 'reversible'}, 'Choose manual or reversible autonomy')
        with self.store.write() as db:
            writable(db)
            name = self._repository(db, repo_id)
            current = autonomy_in(db, repo_id)
            require(current['revision'] == revision, 'Policy changed; refresh before saving')
            if current['policy'] != policy:
                revision = Configuration(self.store, clock=self.settings.clock).replace_in(db, 'steward', {'autonomy': policy},
                    expected_revision=revision, subject=str(repo_id))
        return {'repo_id': repo_id, 'name': name, 'policy': policy, 'revision': revision}
