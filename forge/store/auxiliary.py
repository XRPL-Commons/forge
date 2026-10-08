"""Durable setup/deployment execution ownership, with separate capture artifacts."""
import hashlib
import json

from .models import TransitionError
from .runtime import writable

MUTABLE = {'deadline', 'container_id', 'capacity_wait_since', 'capture', 'fenced', 'cleanup'}


def save_in(db, domain, owner, job, now):
    writable(db)
    identifier = job['id']
    existing = db.execute('SELECT * FROM auxiliary_executions WHERE id=?', (identifier,)).fetchone()
    manifest = json.dumps({k: v for k, v in job.items() if k not in MUTABLE}, sort_keys=True, separators=(',', ':'), allow_nan=False)
    if len(manifest.encode()) > 65536:
        raise TransitionError('auxiliary launch inputs exceed their size limit')
    if existing is None:
        if job.get('cleanup') or job.get('fenced'):
            raise TransitionError('new auxiliary execution must begin with owned launch intent')
        position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM auxiliary_executions WHERE domain=? AND owner_id=?',
                              (domain, str(owner))).fetchone()[0]
        db.execute("INSERT INTO auxiliary_executions VALUES(?,?,?,?,'reserved',0,0,?)", (identifier, domain, str(owner), position, now))
        db.execute('INSERT INTO auxiliary_runtime(execution_id,manifest_json,deadline) VALUES(?,?,?)',
                   (identifier, manifest, job['deadline']))
        db.execute('INSERT INTO auxiliary_leases VALUES(?,?,?,?)', (job['slot'], identifier, identifier, now))
    elif existing['domain'] != domain or existing['owner_id'] != str(owner):
        raise TransitionError('execution belongs to another operational domain')
    runtime = db.execute('SELECT * FROM auxiliary_runtime WHERE execution_id=?', (identifier,)).fetchone()
    if runtime is None or runtime['manifest_json'] != manifest:
        raise TransitionError('auxiliary execution inputs changed')
    capture = job.get('capture')
    capture_digest = runtime['capture_digest']
    if capture is not None:
        payload = json.dumps(capture, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
        if len(payload) > 8 * 1048576:
            raise TransitionError('auxiliary capture exceeds its size limit')
        observed = hashlib.sha256(payload).hexdigest()
        if capture_digest and observed != capture_digest:
            raise TransitionError('auxiliary capture cannot be replaced')
        if not capture_digest:
            for sequence, start in enumerate(range(0, len(payload), 1048576)):
                part = payload[start:start + 1048576]
                db.execute("INSERT INTO auxiliary_artifacts VALUES(?,'capture',?,?,?)",
                           (identifier, sequence, hashlib.sha256(part).hexdigest(), part))
            capture_digest = observed
    fenced, cleanup = bool(job.get('fenced')), bool(job.get('cleanup'))
    if cleanup and not fenced:
        raise TransitionError('cleanup cannot release an unfenced execution')
    phase = 'cleaned' if cleanup else 'collecting' if fenced else 'running' if job.get('container_id') else 'reserved'
    db.execute('UPDATE auxiliary_runtime SET deadline=?,container_id=?,capacity_wait_since=?,capture_digest=? WHERE execution_id=?',
               (job['deadline'], job.get('container_id'), job.get('capacity_wait_since'), capture_digest, identifier))
    db.execute('UPDATE auxiliary_executions SET phase=?,fenced=?,cleanup=? WHERE id=?', (phase, int(fenced), int(cleanup), identifier))
    if cleanup:
        db.execute('DELETE FROM auxiliary_leases WHERE execution_id=?', (identifier,))


def _artifact(db, identifier, kind):
    rows = db.execute('SELECT * FROM auxiliary_artifacts WHERE execution_id=? AND kind=? ORDER BY sequence LIMIT 9',
                      (identifier, kind)).fetchall()
    if len(rows) > 8 or any(row['sequence'] != index or hashlib.sha256(row['content']).hexdigest() != row['digest']
                            for index, row in enumerate(rows)):
        raise TransitionError('auxiliary execution evidence is corrupt')
    return b''.join(row['content'] for row in rows)


def job_in(db, identifier, *, include_capture=True):
    execution = db.execute('SELECT * FROM auxiliary_executions WHERE id=?', (identifier,)).fetchone()
    if execution is None:
        raise KeyError(identifier)
    row = db.execute('SELECT * FROM auxiliary_runtime WHERE execution_id=?', (identifier,)).fetchone()
    if row is None:
        # An imported completed execution is diagnostic evidence, never launch input.
        if not execution['cleanup']:
            raise TransitionError('unqualified legacy auxiliary execution cannot run')
        payload = _artifact(db, identifier, 'legacy_execution')
        if not payload:
            raise TransitionError('legacy auxiliary execution evidence is missing')
        value = json.loads(payload)
        if not include_capture:
            value.pop('capture', None)
        return value
    value = json.loads(row['manifest_json'])
    value.update(deadline=row['deadline'], fenced=bool(execution['fenced']), cleanup=bool(execution['cleanup']))
    for field in ('container_id', 'capacity_wait_since'):
        if row[field] is not None:
            value[field] = row[field]
    if include_capture and row['capture_digest']:
        payload = _artifact(db, identifier, 'capture')
        if hashlib.sha256(payload).hexdigest() != row['capture_digest']:
            raise TransitionError('auxiliary capture digest mismatch')
        value['capture'] = json.loads(payload)
    else:
        value['capture'] = None
    return value


def active(store, domain=None):
    with store.read_snapshot() as db:
        rows = db.execute('SELECT id FROM auxiliary_executions WHERE cleanup=0 ' + ('AND domain=? ' if domain else '') +
                          'ORDER BY created_at,id LIMIT 101', (domain,) if domain else ()).fetchall()
        if len(rows) > 100:
            raise TransitionError('auxiliary execution capacity exceeded')
        return [job_in(db, row[0]) for row in rows]
