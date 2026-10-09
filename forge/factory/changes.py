"""Bounded committed-event cursor reads for the operator UI.

A cursor belongs to one SQLite instance. Waiting uses autocommit reads, never a
long-lived WAL snapshot. Only public event headers leave this module; event
payloads can contain captured requirements and worker diagnostics.
"""
import time

from ..store.queries import Queries

MAX_EVENTS = 100
MAX_ISSUES = 100
MAX_WAIT = 25
EVENT_FIELDS = ('sequence', 'unit_id', 'issue_id', 'job_id', 'kind', 'at')


def cursor(store, db):
    sequence = db.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0]
    return f'{store.instance_id}:{sequence}'


def parse_cursor(value):
    if not isinstance(value, str) or len(value) > 240:
        raise ValueError('Invalid factory change cursor')
    instance, separator, sequence = value.rpartition(':')
    if not separator or not instance or not sequence.isascii() or not sequence.isdecimal() or len(sequence) > 19:
        raise ValueError('Invalid factory change cursor')
    number = int(sequence)
    if number > 9223372036854775807:
        raise ValueError('Invalid factory change cursor')
    return instance, number


class Changes:
    def __init__(self, views, *, monotonic=time.monotonic, sleep=time.sleep):
        self.views, self.store = views, views.store
        self.monotonic, self.sleep = monotonic, sleep

    def read(self, *, since=None, wait=MAX_WAIT):
        if type(wait) not in (int, float) or not 0 <= wait <= MAX_WAIT:
            raise ValueError('Factory change wait must be between 0 and 25 seconds')
        previous = parse_cursor(since) if since is not None else None
        deadline = self.monotonic() + wait
        # The connection is closed before opening the response snapshot. Each
        # MAX(sequence) statement ends its read transaction before sleeping.
        if previous and previous[0] == self.store.instance_id and wait:
            with self.store._connection(read_only=True) as db:
                db.execute('PRAGMA query_only=ON')
                while db.execute('SELECT COALESCE(MAX(sequence),0) FROM events').fetchone()[0] == previous[1]:
                    remaining = deadline - self.monotonic()
                    if remaining <= 0:
                        break
                    self.sleep(min(0.25, remaining))
        with self.store.read_session() as db:
            current = cursor(self.store, db)
            latest = parse_cursor(current)[1]
            resync = previous is None or previous[0] != self.store.instance_id or previous[1] > latest
            rows = [] if resync else Queries(self.store).events(after=previous[1], limit=MAX_EVENTS, include_payload=False)
            if rows and rows['next_cursor'] is not None:
                resync = True
            events = [] if resync else [{key: row[key] for key in EVENT_FIELDS} for row in (rows['items'] if rows else [])]
            affected = {event['issue_id'] for event in events if event['issue_id']}
            units = {event['unit_id'] for event in events if event['unit_id']}
            if units:
                marks = ','.join('?' for _ in units)
                affected.update(row[0] for row in db.execute(
                    'SELECT DISTINCT issue_id FROM unit_issues WHERE unit_id IN (' + marks + ') LIMIT ?',
                    (*sorted(units), MAX_ISSUES + 1)))
            if len(affected) > MAX_ISSUES:
                resync, events, affected = True, [], set()
            config = self.views._config()
            issues = {key: self.views._public(self.views._row(db, key, config=config)) for key in sorted(affected)}
            # This also refreshes clock-derived fields and global observations
            # (heartbeat/access health) on an idle connection at most every 25s.
            board = self.views.factory(section='board')
            return dict(cursor=current, events=events, issues=issues, board=board, resync=resync)
