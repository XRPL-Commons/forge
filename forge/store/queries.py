"""Bounded snapshot reads, with output fetched only by explicit artifact request."""
import json

from .artifacts import (
    Artifact, ArtifactCorrupt, ArtifactExpired, ArtifactTooLarge, Artifacts,
    MAX_ARTIFACT_SIZE,
)
from .transitions import project_unit


MAX_PAGE_SIZE = 100


def _limit(value):
    if type(value) is not int or not 1 <= value <= MAX_PAGE_SIZE:
        raise ValueError(f'limit must be an integer between 1 and {MAX_PAGE_SIZE}')
    return value


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
            return project_unit(connection, unit_id)

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
            clauses.append("state NOT IN ('merged', 'closed', 'dissolved')")
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

    def events(self, *, after=0, limit=50, unit_id=None):
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
                'kind, at, actor_id, payload_json FROM events WHERE '
                + ' AND '.join(clauses) + ' ORDER BY sequence LIMIT ?',
                (*parameters, limit + 1),
            ).fetchall()
            items = []
            for row in rows[:limit]:
                item = dict(row)
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
