"""Authenticated choice selection; the supervisor applies the recorded intent."""
import uuid

from ..steward_contract import digest as choice_digest, require
from ..store.models import Command
from ..store.queries import Queries
from ..store.runtime import required
from ..store.steward import Steward, decision_in
from ..store.steward_views import view_in
from ..store.transitions import _unit


class StewardService(Steward):
    def __init__(self, root=None, *, store=None, actor_id=None, **kwargs):
        super().__init__(store if store is not None else required(root), **kwargs)
        self.actor_id = actor_id

    def view(self, key):
        require(isinstance(key, str) and 0 < len(key) <= 200, 'Expected an issue key')
        with self.store.read_snapshot() as db:
            return view_in(db, key)

    def choose(self, *, key, decision_id, choice_id, digest, request_id):
        require(isinstance(self.actor_id, str) and self.actor_id.startswith('github:'),
                'An authenticated operator identity is required')
        require(all(isinstance(value, str) and 0 < len(value) <= 200
                    for value in (key, decision_id, choice_id, digest, request_id)), 'Expected stored choice identifiers')
        try:
            uuid.UUID(request_id)
        except ValueError as exc:
            raise ValueError('Expected a UUID request identity') from exc
        payload = dict(key=key, decision_id=decision_id, choice_id=choice_id, digest=digest)
        with self.store.read_snapshot() as db:
            row, _, _ = decision_in(db, decision_id)
            previous = db.execute('SELECT expected_version FROM decisions WHERE request_id=?', (request_id,)).fetchone()
            version = previous[0] if previous else _unit(db, row['unit_id'])['version']
        command = Command(request_id, self.actor_id, row['unit_id'], version)

        def select(db, unit):
            issue = Queries._issue(db, key)
            require(issue['lifecycle']['unit_id'] == unit['id'], 'Choice belongs to another delivery')
            row, output, _ = decision_in(db, decision_id)
            require(row['output_digest'] == digest, 'Steward decision changed; refresh before choosing')
            choice = next((choice for choice in output['choices'] if choice['id'] == choice_id), None)
            require(choice is not None, 'Unknown steward choice')
            return self.select_in(db, command, unit, decision_id=decision_id, choice_id=choice_id,
                                  choice_digest=choice_digest(choice), selection='operator')

        self._apply(command, 'operator_select_steward', payload, select)
        return self.view(key)
