"""Candidate verification observations and explicit operator confirmation.

CI can complete only the exact checks configured by an operator. A GitHub review
never substitutes for a person's explicit confirmation that verification ran.
"""
import json
import time
import uuid

from ..store.db import StoreError
from ..store.models import Command, IdempotencyConflict, TransitionError
from ..store.transitions import manifest_hash
from ..store.verification import Verification, VerificationQueries, trusted_checks
from .contracts import digest
from .github import GitHubUnavailable
from .ownership import ownership
from .verification import ci_result


class VerificationController:
    def __init__(self, store, github, *, profiles, policy, clock=time.time,
                 hold_labels=('hold', 'blocked')):
        self.store, self.github = store, github
        self.profiles, self.policy, self.clock = profiles, policy, clock
        self.hold_labels = frozenset(hold_labels)
        self.commands = Verification(store, clock=clock)
        self.queries = VerificationQueries(store)

    @staticmethod
    def _current(item, pr):
        identity = item['identity']
        return (isinstance(pr, dict) and pr.get('number') == identity['pull_number']
                and pr.get('state') == 'open' and pr.get('draft') is False and pr.get('merged') is False
                and (pr.get('head') or {}).get('sha') == identity['head_oid']
                and (pr.get('base') or {}).get('sha') == identity['base_oid']
                and (pr.get('base') or {}).get('ref') == item['target_branch']
                and ((pr.get('head') or {}).get('repo') or {}).get('id') == item['repo_id']
                and ((pr.get('base') or {}).get('repo') or {}).get('id') == item['repo_id'])

    def _issue_current(self, item):
        issue, requirements = self.github.issue(item)
        labels = {row.get('name') if isinstance(row, dict) else row for row in issue.get('labels', [])}
        return (issue.get('number') == item['number'] and issue.get('state') == 'open'
                and requirements == item['requirements'] and not labels.intersection(self.hold_labels)
                and manifest_hash(requirements) == item['identity']['requirements_hash']
                and ownership(issue, item, item['repo']) is None)

    def _observe(self, item):
        if (item['unit']['current_candidate_id'] != item['identity']['id']
                or item['unit']['state'] in {'merged', 'closed', 'dissolved'}
                or digest(self.policy(item)) != item['identity']['policy_hash']
                or not self._issue_current(item)):
            return False
        pr = self.github.pr(item, item['identity']['pull_number'])
        return self._current(item, pr) and not any(
            row.get('state') == 'CHANGES_REQUESTED' for row in self.github.approvals(item, pr))

    def _proof(self, item):
        return dict(verified=True, candidate_id=item['identity']['id'], observed_at=self.clock(),
                    **{key: item['identity'][key] for key in
                       ('head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')},
                    **{key: item['unit'][key] for key in ('membership_generation', 'fencing_generation')})

    def step(self, gate_id):
        item = self.queries.gate(gate_id)
        if item['gate']['resolved_at'] is not None:
            return {'gate_id': gate_id, 'resolved': True}
        if item['gate']['code'] == 'verification_human':
            return {'gate_id': gate_id, 'pending': 'operator_confirmation'}
        checks = item['details'].get('ci')
        allowed = self.profiles(item['repo'])
        if not checks or not allowed or not trusted_checks(checks) <= trusted_checks(allowed):
            return {'gate_id': gate_id, 'pending': 'verification_configuration'}
        if not self._observe(item):
            return {'gate_id': gate_id, 'pending': 'candidate_reconciliation'}
        snapshot = self.github.ci_evidence(item)
        if not ci_result(snapshot, item['candidate'], checks):
            return {'gate_id': gate_id, 'pending': 'ci_checks'}
        selected = {(row['name'], row['app_id']) for row in checks}
        rows = [row for row in snapshot['checks'][item['candidate']['head']]
                if (row.get('name'), row.get('app_id')) in selected]
        if any(row.get('head_sha', item['candidate']['head']) != item['candidate']['head'] for row in rows):
            return {'gate_id': gate_id, 'pending': 'ci_checks'}
        if self.profiles(item['repo']) != allowed or not self._observe(item):
            return {'gate_id': gate_id, 'pending': 'candidate_reconciliation'}
        proof = {**self._proof(item), 'via': 'ci', 'profile_hash': manifest_hash(allowed),
                 'observed_at': snapshot.get('observed_at'), 'checks': [
                     {**{key: row.get(key) for key in ('id', 'name', 'app_id', 'status', 'conclusion')},
                      'head_oid': item['candidate']['head']} for row in rows]}
        command = Command(str(uuid.uuid4()), 'supervisor:verification', item['unit']['id'], item['unit']['version'])
        return self.commands.complete_ci(command, gate_id, proof=proof, allowed_checks=allowed)

    def confirm_human(self, command, gate_id, *, statement):
        """Authenticated operator entrypoint; caller enforces operator permission.

        Repeated requests return the recorded receipt without collecting new
        evidence or accidentally changing an idempotency key's payload.
        """
        with self.store.read_snapshot() as db:
            previous = db.execute('SELECT * FROM decisions WHERE request_id=?', (command.request_id,)).fetchone()
        if previous:
            payload = json.loads(previous['payload_json'])
            proof = payload.get('proof', {})
            if (previous['command'] != 'complete_human_verification' or previous['unit_id'] != command.unit_id
                    or previous['actor_id'] != command.actor_id or previous['expected_version'] != command.expected_version
                    or payload.get('gate_id') != gate_id or proof.get('statement') != statement):
                raise IdempotencyConflict('operator verification request already has another payload')
            return self.commands.complete_human(command, gate_id, proof=proof)
        item = self.queries.gate(gate_id)
        if item['gate']['code'] != 'verification_human' or not self._observe(item):
            raise TransitionError('human verification requires the current candidate and an explicit human gate')
        proof = {**self._proof(item), 'via': 'human', 'operator_confirmed': True,
                 'actor_id': command.actor_id, 'statement': statement}
        return self.commands.complete_human(command, gate_id, proof=proof)

    def cycle(self, *, after='', limit=50):
        page = self.queries.pending(after=after, limit=limit)
        results = []
        for gate_id in page['items']:
            try:
                results.append(self.step(gate_id))
            except (GitHubUnavailable, TransitionError, StoreError, OSError) as error:
                results.append({'gate_id': gate_id, 'error': getattr(error, 'code', type(error).__name__)})
        return {'items': results, 'next_cursor': page['next_cursor']}
