"""Bounded snapshot reads, with output fetched only by explicit artifact request."""
import json
import math

from .artifacts import (
    Artifact, ArtifactCorrupt, ArtifactExpired, ArtifactTooLarge, Artifacts,
    MAX_ARTIFACT_SIZE,
)
from .transitions import project_unit, issue_state, _collect_gates, _scope_gates, TERMINAL
from . import status


MAX_PAGE_SIZE = 100


def _limit(value):
    if type(value) is not int or not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError(f'limit must be an integer between 1 and {MAX_PAGE_SIZE}')
    return value


class IssueGateFacts:
    """Read-only unit facts owned by one projection in a held SQL snapshot.

    This owner never belongs to Store or a lifecycle writer. Discard it before
    leaving the snapshot; member/candidate scoping stays in the shared evaluator.
    """

    def __init__(self, connection):
        self.connection = connection
        self._units = {}

    def for_assignment(self, unit, member_id, candidate_id):
        if unit['id'] not in self._units:
            self._units[unit['id']] = _collect_gates(self.connection, unit)
        return _scope_gates(self._units[unit['id']], unit, member_id, candidate_id)


class Queries:
    """Read models for future adapters. Every response uses one SQL snapshot.

    Keyset cursors identify the last returned row. Successive pages can observe
    newer commits; an individual page is internally consistent. No cache, raw
    connection, credential table or whole-store dictionary is exposed.
    """

    def __init__(self, store):
        self.store = store

    def unit(self, unit_id):
        with self.store.read_snapshot() as connection:
            unit = project_unit(connection, unit_id)
            return unit

    @staticmethod
    def _issue(connection, issue_id, *, include_body=False, include_gates=False):
        return Queries._issue_context(connection, issue_id, include_body=include_body,
                                      include_gates=include_gates)[0]

    @staticmethod
    def _issue_context(connection, issue_id, *, include_body=False, include_gates=False):
        """Canonical issue and its latest membership's unit in one snapshot.

        The unit remains available as historical context when a removed or
        reopened member no longer inherits its lifecycle. It grants no authority.
        """
        return Queries._issue_contexts(connection, [issue_id], include_body=include_body,
                                       include_gates=include_gates)[issue_id]

    @staticmethod
    def _issue_contexts(connection, issue_ids, *, include_body=False, include_gates=False, gate_facts=None):
        """Hydrate one bounded page; lifecycle decisions still share one projector."""
        identifiers = tuple(dict.fromkeys(issue_ids))
        if not identifiers:
            return {}
        _limit(len(identifiers))
        placeholders = ','.join('?' for _ in identifiers)
        # Bodies and requirements remain exclusive to explicit detail reads.
        body = ', r.body, r.requirements_json' if include_body else ''
        rows = connection.execute(
            "SELECT i.*, r.title, r.content_hash, r.requirements_hash, COALESCE(v.body_truncated,0) AS body_truncated, p.full_name AS repository"
            + body + ' FROM issues i JOIN issue_revisions r ON r.id=i.current_revision_id LEFT JOIN revision_presentation v ON v.revision_id=r.id '
            'JOIN repositories p ON p.id=i.repository_id WHERE i.id IN (' + placeholders + ')', identifiers,
        ).fetchall()
        issues = {row['id']: dict(row) for row in rows}
        for identifier in identifiers:
            if identifier not in issues:
                raise KeyError(identifier)
        members = {row['issue_id']: dict(row) for row in connection.execute(
            'SELECT m.* FROM issues i JOIN unit_issues m ON m.id=('
            'SELECT id FROM unit_issues WHERE issue_id=i.id '
            'ORDER BY (left_at IS NULL) DESC, joined_at DESC, id DESC LIMIT 1) '
            'WHERE i.id IN (' + placeholders + ')', identifiers)}
        unit_ids = tuple({member['unit_id'] for member in members.values()})
        units = {row['id']: dict(row) for row in connection.execute(
            'SELECT * FROM units WHERE id IN (' + ','.join('?' for _ in unit_ids) + ')', unit_ids)} if unit_ids else {}
        gate_facts = gate_facts if gate_facts is not None else IssueGateFacts(connection)
        return {identifier: Queries._project_issue(connection, issues[identifier], members.get(identifier),
                    units[members[identifier]['unit_id']] if identifier in members else None,
                    include_body=include_body, include_gates=include_gates, gate_facts=gate_facts) for identifier in identifiers}

    @staticmethod
    def _project_issue(connection, issue, member, historical_unit, *, include_body=False, include_gates=False, gate_facts):
        issue_id = issue['id']
        for field in ('labels', 'assignees', 'assignee_types'):
            issue[field] = json.loads(issue.pop(field + '_json'))
        if include_body:
            issue['requirements'] = json.loads(issue.pop('requirements_json'))
        unit = historical_unit
        blocker = None
        gates = []
        if member:
            if member['left_at'] is None and unit['state'] not in TERMINAL:
                gates = gate_facts.for_assignment(unit, member['id'], unit['current_candidate_id'])
                blocker = gates[0] if gates else None
            elif unit['state'] not in TERMINAL:
                # A removed member does not inherit the still-running batch's phase.
                unit = None
        projected = issue_state(issue, unit, blocker, dict(member) if member else None)
        current = bool(member and member['left_at'] is None and unit and unit['state'] not in TERMINAL)
        lifecycle = dict(state=projected, unit_id=unit['id'] if current else None,
                         unit_version=unit['version'] if current else None,
                         member_id=member['id'] if current else None,
                         historical_unit_id=member['unit_id'] if member and not current else None,
                         stage_code=unit['state'] if current or projected == 'done' and unit and unit['state'] == 'merged' else None,
                         reason_code=blocker['code'] if blocker else None,
                         current_blocker=blocker,
                         candidate_id=unit['current_candidate_id'] if current else None)
        if projected in {'done', 'dropped', 'untracked'}:
            lifecycle['display_status'] = {'done': 'Done', 'dropped': 'Dropped', 'untracked': 'Not queued'}[projected]
        elif issue['github_state'] == 'closed' and issue['state_reason'] is None and blocker is None:
            lifecycle.update(reason_code='observation_unavailable',
                             display_status=status.describe('observation_unavailable'))
        else:
            lifecycle['display_status'] = status.describe_gate(blocker) if blocker else status.describe(unit['state'])
        if projected == 'untracked' and issue['github_state'] == 'open':
            failed = connection.execute('SELECT reason_code,observed_at,retry_after FROM issue_discovery_observations '
                                        'WHERE issue_id=? AND revision_id=?',
                                        (issue_id, issue['current_revision_id'])).fetchone()
            if failed:
                lifecycle.update(reason_code=failed['reason_code'], display_status=status.describe(failed['reason_code']))
                issue['discovery_observation'] = dict(failed)
        from .scheduling import current_in, execution_condition_in
        lifecycle['next_action'] = current_in(connection, unit, member['id']) if current else None
        lifecycle['execution_condition'] = execution_condition_in(connection, unit, member['id'],
            gates=gates, action=lifecycle['next_action']) if current else None
        if include_gates:
            lifecycle['gates'] = gates
        issue['lifecycle'] = lifecycle
        return issue, historical_unit

    def issue(self, issue_id):
        with self.store.read_snapshot() as connection:
            return self._issue(connection, issue_id, include_body=True)

    def issues(self, *, repository_id=None, after=None, limit=50, github_state=None):
        limit = _limit(limit)
        if repository_id is not None and (type(repository_id) is not int or repository_id < 1):
            raise ValueError('repository_id must be a positive integer')
        if github_state not in {None, 'open', 'closed'}:
            raise ValueError('github_state must be open or closed')
        clauses, parameters = [], []
        if repository_id is not None:
            clauses.append('repository_id=?')
            parameters.append(repository_id)
        if github_state is not None:
            clauses.append('github_state=?')
            parameters.append(github_state)
        if after is not None:
            if (not isinstance(after, (list, tuple)) or len(after) != 2
                    or any(type(value) is not int or value < 1 for value in after)):
                raise ValueError('issue cursor must contain repository ID and issue number')
            clauses.append('(repository_id,github_number)>(?,?)')
            parameters.extend(after)
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.store.read_snapshot() as connection:
            rows = connection.execute('SELECT id,repository_id,github_number FROM issues' + where
                                      + ' ORDER BY repository_id,github_number LIMIT ?', (*parameters, limit + 1)).fetchall()
            page = rows[:limit]
            return dict(items=[self._issue(connection, row['id']) for row in page],
                        next_cursor=[page[-1]['repository_id'], page[-1]['github_number']] if len(rows) > limit else None)

    def jobs(self, unit_id, *, before=None, limit=20):
        """Job metadata only; artifact bytes have a separate explicit query."""
        limit = _limit(limit)
        clauses, parameters = ['unit_id=?'], [unit_id]
        if before is not None:
            if (not isinstance(before, (tuple, list)) or len(before) != 2
                    or type(before[0]) not in (int, float) or not math.isfinite(before[0]) or not isinstance(before[1], str)):
                raise ValueError('job cursor must contain creation time and job ID')
            clauses.append('(created_at,id)<(?,?)')
            parameters.extend(before)
        with self.store.read_snapshot() as connection:
            rows = connection.execute('SELECT id,unit_id,attempt_id,member_id,candidate_id,role,phase,outcome,fenced,cleanup,created_at '
                                      'FROM jobs WHERE ' + ' AND '.join(clauses)
                                      + ' ORDER BY created_at DESC,id DESC LIMIT ?', (*parameters, limit + 1)).fetchall()
            items = [dict(row) for row in rows[:limit]]
            return dict(items=items, next_cursor=[items[-1]['created_at'], items[-1]['id']] if len(rows) > limit else None)

    def units(self, *, after=None, limit=50, include_terminal=False):
        limit = _limit(limit)
        if after is not None and not isinstance(after, str):
            raise ValueError('after must be a unit ID')
        if type(include_terminal) is not bool:
            raise ValueError('include_terminal must be a boolean')
        clauses, parameters = [], []
        if after is not None:
            clauses.append('id > ?')
            parameters.append(after)
        if not include_terminal:
            clauses.append("state NOT IN ('merged', 'closed')")
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.store.read_snapshot() as connection:
            rows = connection.execute(
                'SELECT id FROM units' + where + ' ORDER BY id LIMIT ?',
                (*parameters, limit + 1),
            ).fetchall()
            page = rows[:limit]
            return {
                'items': [project_unit(connection, row['id']) for row in page],
                'next_cursor': page[-1]['id'] if len(rows) > limit else None,
            }

    def events(self, *, after=0, limit=50, unit_id=None, include_payload=True):
        limit = _limit(limit)
        if type(after) is not int or after < 0:
            raise ValueError('after must be a nonnegative event sequence')
        clauses, parameters = ['sequence > ?'], [after]
        if unit_id is not None:
            clauses.append('unit_id = ?')
            parameters.append(unit_id)
        with self.store.read_snapshot() as connection:
            rows = connection.execute(
                'SELECT sequence, transaction_id, unit_id, issue_id, job_id, '
                'kind, at, actor_id' + (', payload_json' if include_payload else '') + ' FROM events WHERE '
                + ' AND '.join(clauses) + ' ORDER BY sequence LIMIT ?',
                (*parameters, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                item = dict(row)
                if include_payload:
                    item['payload'] = json.loads(item.pop('payload_json'))
                items.append(item)
            return {
                'items': items,
                'next_cursor': items[-1]['sequence'] if len(rows) > limit else None,
            }

    def artifacts(self, job_id, *, after=None, limit=50):
        """List metadata without loading output BLOBs; cursor is (kind, sequence)."""
        limit = _limit(limit)
        clauses, parameters = ['job_id = ?'], [job_id]
        if after is not None:
            if (not isinstance(after, (tuple, list)) or len(after) != 2
                    or not isinstance(after[0], str)
                    or type(after[1]) is not int or after[1] < 0):
                raise ValueError('after must contain an artifact kind and sequence')
            clauses.append('(kind, sequence) > (?, ?)')
            parameters.extend(after)
        with self.store.read_snapshot() as connection:
            rows = connection.execute(
                'SELECT job_id, kind, sequence, media_type, digest, size, codec, raw_size, expired_at '
                'FROM job_artifacts WHERE ' + ' AND '.join(clauses)
                + ' ORDER BY kind, sequence LIMIT ?', (*parameters, limit + 1),
            ).fetchall()
            items = [dict(row) for row in rows[:limit]]
            return {
                'items': items,
                'next_cursor': [items[-1]['kind'], items[-1]['sequence']]
                if len(rows) > limit else None,
            }

    def artifact(self, job_id, kind, sequence=0):
        return Artifacts(self.store).read(job_id, kind, sequence)
