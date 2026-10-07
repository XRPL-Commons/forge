"""Ordered GitHub observations for current SQLite units of one.

Collection generations are reserved before network reads. A late response cannot
replace a newer observation. All issue facts, holds and readiness changes commit
with one lifecycle command; imported reconciliation gates are never auto-cleared.
"""
import time
import uuid

from ..store.db import StoreError
from ..store.models import Command, TransitionError
from ..store.queries import Queries
from ..store.transitions import Lifecycle, TERMINAL
from .github import GitHubUnavailable
from .ownership import ownership


class ObservationController:
    def __init__(self, store, github, *, admission, policy=None,
                 hold_labels=('hold', 'blocked'), clock=time.time):
        self.store, self.github, self.admission = store, github, admission
        self.policy, self.hold_labels, self.clock = policy, tuple(hold_labels), clock
        self.commands = Lifecycle(store, clock=clock)
        self.queries = Queries(store)

    def _snapshot(self, unit_id):
        unit = self.queries.unit(unit_id)
        if unit['state'] in TERMINAL:
            return None
        if len(unit['members']) != 1:
            raise TransitionError('observation currently requires a unit of one')
        issue = self.queries.issue(unit['members'][0]['issue_id'])
        with self.store.read_snapshot() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot observe live GitHub state')
            selected = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
        candidate = dict(selected) if selected else None
        item = dict(repo=issue['repository'], repo_id=issue['repository_id'], number=issue['github_number'],
                    candidate={'pr': candidate['pull_number']} if candidate else None)
        return unit, issue, candidate, item

    def _generation(self, issue):
        key = 'observation_generation:' + issue['id']
        with self.store.write() as db:
            if db.execute("SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'").fetchone():
                raise TransitionError('staging imports cannot reserve observations')
            row = db.execute('SELECT value FROM store_metadata WHERE key=?', (key,)).fetchone()
            current = db.execute('SELECT collection_generation FROM issues WHERE id=?', (issue['id'],)).fetchone()[0]
            generation = max(int(row[0]) if row else 0, current) + 1
            db.execute('INSERT INTO store_metadata(key,value) VALUES(?,?) '
                       'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, str(generation)))
        return generation

    @staticmethod
    def _command(unit):
        return Command(str(uuid.uuid4()), 'supervisor:observation', unit['id'], unit['version'])

    def _external_change(self, unit, detail):
        with self.store.read_snapshot() as db:
            existing = db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='external_change' "
                                  'AND resolved_at IS NULL', (unit['id'],)).fetchone()
        if not existing:
            self.commands.open_gate(self._command(unit), 'external_change', details={'reason': detail})
        return {'unit_id': unit['id'], 'pending': 'external_change'}

    def step(self, unit_id):
        snapshot = self._snapshot(unit_id)
        if snapshot is None:
            return {'unit_id': unit_id, 'terminal': True}
        unit, issue, candidate, item = snapshot
        generation = self._generation(issue)
        remote, requirements = self.github.issue(item)
        if (remote.get('number') != item['number'] or 'pull_request' in remote
                or not isinstance(remote.get('updated_at'), str)):
            raise GitHubUnavailable('Issue observation identity or ordering is unverified')
        labels = [row['name'] if isinstance(row, dict) else row for row in remote.get('labels', [])]
        assignees = [row['login'] if isinstance(row, dict) else row for row in remote.get('assignees', [])]
        remote_pr, pr_error = None, None
        if candidate:
            try:
                remote_pr = self.github.pr(item, candidate['pull_number'])
            except GitHubUnavailable as error:
                pr_error = error
        terminal = remote['state'] == 'closed' or remote_pr is not None and remote_pr.get('merged') is True
        # A verified closure must fence its worker even when unrelated inventory
        # or candidate reads are unavailable. It cannot clear ownership/readiness.
        conflict = allowed = None
        if not terminal and pr_error is None:
            conflict = ownership(remote, item, item['repo'], self.github.pulls(item)) is not None
            allowed = self.admission(item)
            if type(allowed) is not bool:
                raise TransitionError('admission adapter must return a verified boolean')
        outcome = self.commands.observe_issue(self._command(unit), issue['id'], state=remote['state'],
            state_reason=remote.get('state_reason'), title=remote['title'], body=remote.get('body') or '',
            requirements=requirements, remote_updated_at=remote['updated_at'], generation=generation,
            labels=labels, assignees=assignees, hold_labels=self.hold_labels,
            admission=allowed, ownership_conflict=conflict,
            assignee_types={row['login']: row['type'] for row in remote.get('assignees', [])
                            if isinstance(row, dict) and row.get('type')})
        if outcome.get('stale'):
            return {'unit_id': unit_id, 'stale': True}
        current = self._snapshot(unit_id)
        if current is None:
            return {'unit_id': unit_id, 'terminal': True}
        unit, issue, selected, item = current
        # A requirements change may already have superseded the candidate.
        if candidate and (not selected or selected['id'] != candidate['id']):
            return {'unit_id': unit_id, 'invalidated': 'requirements'}
        proof = {'issue_version': issue['version']}
        if pr_error:
            raise pr_error
        if selected:
            pr = remote_pr if remote_pr is not None else self.github.pr(item, selected['pull_number'])
            if (pr.get('number') != selected['pull_number']
                    or (pr.get('head') or {}).get('repo', {}).get('id') != item['repo_id']
                    or (pr.get('base') or {}).get('repo', {}).get('id') != item['repo_id']):
                raise GitHubUnavailable('Candidate repository identity is unverified')
            exact_head = pr.get('head', {}).get('sha') == selected['head_oid']
            exact_base = pr.get('base', {}).get('sha') == selected['base_oid']
            if pr.get('merged') is True:
                if not exact_head or not exact_base:
                    return self._external_change(unit, 'merged_candidate_changed')
                with self.store.read_snapshot() as db:
                    busy = db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0', (unit_id,)).fetchone()
                if busy:
                    return self._external_change(unit, 'external_merge_observed')
                merge_sha = pr.get('merge_commit_sha')
                return {'unit_id': unit_id, **self.commands.confirm_merged(self._command(unit), selected['id'],
                    merge_sha=merge_sha, evidence={'verified': True, 'merged': True, 'merge_sha': merge_sha,
                        'pull_number': selected['pull_number'], 'head_oid': selected['head_oid'],
                        'base_oid': selected['base_oid']})}
            if pr.get('merged') is not False:
                raise GitHubUnavailable('Candidate merge state is unverified')
            if not exact_head:
                return self._external_change(unit, 'candidate_head_changed')
            proof.update(verified=True, merged=False, candidate_id=selected['id'],
                         pull_number=selected['pull_number'], head_oid=selected['head_oid'], observed_at=self.clock())
            if remote['state'] == 'open':
                if pr.get('state') != 'open' or pr.get('draft') is not False:
                    return self._external_change(unit, 'candidate_unavailable')
                if pr.get('base', {}).get('ref') != unit['base_branch']:
                    return self._external_change(unit, 'candidate_target_changed')
                if not exact_base or self.policy is not None and self.policy(item) != selected['policy_hash']:
                    reason = 'base_changed' if not exact_base else 'policy_changed'
                    return {'unit_id': unit_id, **self.commands.invalidate_candidate(self._command(unit),
                        selected['id'], reason=reason, evidence=proof)}
        if remote['state'] == 'closed' and remote.get('state_reason') in {'completed', 'not_planned', 'duplicate'}:
            return {'unit_id': unit_id, **self.commands.close_unit(self._command(unit),
                close_code='github_' + remote['state_reason'], evidence=proof)}
        return {'unit_id': unit_id, 'observed': True}

    def cycle(self, *, after=None, limit=50):
        page = self.queries.units(after=after, limit=limit)
        results = []
        for unit in page['items']:
            try:
                results.append(self.step(unit['id']))
            except (GitHubUnavailable, TransitionError, StoreError, OSError) as error:
                results.append({'unit_id': unit['id'], 'error': getattr(error, 'code', type(error).__name__)})
        return {'items': results, 'next_cursor': page['next_cursor']}
