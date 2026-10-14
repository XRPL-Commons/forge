"""Immutable mirror input, revocable sessions and job-associated fetch evidence."""
import json
import time
import uuid

from ..factory.source_inputs import validate, digest, encoded
from ..mirror.policy import MirrorError, policy_digest, validate_policy

CLAIMS = frozenset({'policy_denied', 'project_denied', 'installation_access_missing',
                    'repository_unavailable', 'revision_unavailable', 'dependency_format_unsupported', 'mirror_capacity'})
RECEIPT_FIELDS = frozenset({'repository', 'repository_id', 'request_kind', 'outcome', 'reason',
    'cache', 'host_fetch', 'request_bytes', 'response_bytes', 'requested_oids', 'requested_oids_digest',
    'refs_digest', 'started_at', 'finished_at', 'transient'})


def view_in(db, job_id, *, limit=20):
    """Bounded diagnostic evidence; claims are observations, never grants."""
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('Mirror evidence limit must be between 1 and 50')
    fields = ('repository', 'repository_id', 'request_kind', 'outcome', 'reason', 'phase',
              'cache', 'host_fetch', 'request_bytes', 'response_bytes', 'transient',
              'source_digest', 'policy_digest', 'manifest_digest', 'refs_digest')
    result = {}
    for name, key in (('receipts', 'id'), ('claims', 'receipt_id')):
        table = 'mirror_' + name
        total = db.execute(f'SELECT count(*) FROM {table} WHERE job_id=?', (job_id,)).fetchone()[0]
        rows = db.execute(f'SELECT {key},created_at,details_json FROM {table} WHERE job_id=? '
                          f'ORDER BY created_at DESC,{key} DESC LIMIT ?', (job_id, limit))
        values = []
        for row in rows:
            details = json.loads(row['details_json'])
            safe = {field: value for field in fields if (value := details.get(field)) is not None
                    and (type(value) in (bool, int, float) or isinstance(value, str) and len(value) <= 256)}
            values.append(dict(id=row[key], created_at=row['created_at'], **safe))
        result[name] = values
        result[name + '_total'] = total
        result[name + '_truncated'] = total > len(values)
    return result


class MirrorStore:
    def __init__(self, store, *, clock=time.time, max_requests=1024, max_request_bytes=64 * 1024 ** 2, max_response_bytes=1024 ** 3):
        self.store, self.clock = store, clock
        self.max_requests, self.max_request_bytes, self.max_response_bytes = max_requests, max_request_bytes, max_response_bytes

    def prepare(self, source_input, policy):
        source, policy = validate(source_input), validate_policy(policy)
        key, source_digest = policy_digest(policy), digest(source)
        if (source['dependency_policy_digest'] != key or source['manifest_digest'] != policy['manifest_digest']
                or source['repository_id'] != policy['project_repository_id']):
            raise MirrorError('policy_denied')
        with self.store.write() as db:
            self._source_in(db, source, policy)
        return key

    def _source_in(self, db, source, policy):
        key, source_digest = policy_digest(policy), digest(source)
        db.execute('INSERT OR IGNORE INTO mirror_policies VALUES(?,?,?)',
                   (key, encoded(policy).decode(), self.clock()))
        db.execute('INSERT OR IGNORE INTO mirror_sources VALUES(?,?,?,?)',
                   (source_digest, key, encoded(source).decode(), self.clock()))

    def bind_session(self, *, job_id, execution_fence, network_generation, deadline, source_digest, policy_digest):
        if (any(not isinstance(v, str) or not 0 < len(v) <= 200 for v in (job_id, execution_fence, network_generation))
                or type(deadline) not in (int, float) or not self.clock() < deadline <= self.clock() + 86400):
            raise MirrorError('session_invalid')
        with self.store.write() as db:
            policy = db.execute('SELECT 1 FROM mirror_sources WHERE digest=? AND policy_digest=?', (source_digest, policy_digest)).fetchone()
            if policy is None:
                raise MirrorError('session_invalid')
            previous = db.execute('SELECT * FROM mirror_sessions WHERE job_id=? AND execution_fence=? AND network_generation=?',
                                  (job_id, execution_fence, network_generation)).fetchone()
            if previous:
                if previous['source_digest'] != source_digest or previous['policy_digest'] != policy_digest or previous['deadline'] != deadline:
                    raise MirrorError('session_invalid')
                return previous['id']
            identifier = str(uuid.uuid4())
            db.execute('INSERT INTO mirror_sessions VALUES(?,?,?,?,?,?,?,?,NULL)',
                       (identifier, job_id, execution_fence, network_generation, source_digest, policy_digest, deadline, self.clock()))
        return identifier

    def session(self, identifier):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT s.*,p.policy_json,x.source_json FROM mirror_sessions s JOIN mirror_policies p ON p.digest=s.policy_digest JOIN mirror_sources x ON x.digest=s.source_digest AND x.policy_digest=s.policy_digest WHERE s.id=?',
                             (identifier,)).fetchone()
            if row is None:
                raise MirrorError('session_invalid')
            value = dict(row)
        value['policy'] = json.loads(value.pop('policy_json'))
        value['source_input'] = json.loads(value.pop('source_json'))
        return value

    def revoke(self, identifier):
        with self.store.write() as db:
            db.execute('UPDATE mirror_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL', (self.clock(), identifier))

    def _receipt_in(self, db, session, details, request_id=None):
        identifier = str(uuid.uuid4())
        safe = {**details, 'source_digest': session['source_digest'], 'policy_digest': session['policy_digest'],
                'execution_fence': session['execution_fence'], 'network_generation': session['network_generation']}
        db.execute('INSERT INTO mirror_receipts VALUES(?,?,?,?,?,?)',
                   (identifier, session['id'], request_id, session['job_id'], encoded(safe).decode(), self.clock()))
        if details.get('outcome') == 'denied' and details.get('reason') in CLAIMS:
            db.execute('INSERT INTO mirror_claims VALUES(?,?,?,?,?)',
                       (identifier, session['job_id'], details['reason'], encoded(safe).decode(), self.clock()))
        return identifier

    def append_receipt(self, identifier, details, *, request_id=None):
        """Admit an attempt or finish one exact admitted request, under one lock.

        Terminal-only host receipts count as attempts too. The first capacity
        refusal is durable and revokes admission; subsequent probes add nothing.
        Already admitted requests may each record one terminal cancellation.
        """
        if (not isinstance(details, dict) or not set(details) <= RECEIPT_FIELDS or len(encoded(details)) > 60000
                or details.get('outcome') not in {'started', 'served', 'denied', 'failed'}
                or any(type(details.get(key, 0)) is not int or details.get(key, 0) < 0 for key in ('request_bytes', 'response_bytes'))
                or request_id is not None and details['outcome'] == 'started'):
            raise MirrorError('receipt_invalid')
        refusal = None
        with self.store.write() as db:
            session = db.execute('SELECT * FROM mirror_sessions WHERE id=?', (identifier,)).fetchone()
            if session is None:
                raise MirrorError('session_invalid')
            if request_id is not None:
                started = db.execute("SELECT 1 FROM mirror_receipts WHERE id=? AND session_id=? AND request_id IS NULL "
                    "AND json_extract(details_json,'$.outcome')='started'", (request_id, identifier)).fetchone()
                if not started:
                    raise MirrorError('receipt_invalid')
                previous = db.execute('SELECT id FROM mirror_receipts WHERE request_id=?', (request_id,)).fetchone()
                if previous:
                    return previous['id']
            elif session['revoked_at'] is not None or session['deadline'] <= self.clock():
                raise MirrorError('session_revoked')
            usage = db.execute("SELECT sum(request_id IS NULL), "
                "sum(CASE WHEN request_id IS NULL THEN json_extract(details_json,'$.request_bytes') ELSE 0 END), "
                "sum(CASE WHEN json_extract(details_json,'$.outcome')='served' THEN json_extract(details_json,'$.response_bytes') ELSE 0 END) "
                'FROM mirror_receipts WHERE session_id=?', (identifier,)).fetchone()
            requests, received, sent = (value or 0 for value in usage)
            exceeded = (request_id is None and (requests >= self.max_requests or received + details.get('request_bytes', 0) > self.max_request_bytes)
                or details['outcome'] == 'served' and sent + details.get('response_bytes', 0) > self.max_response_bytes)
            if details['outcome'] == 'served' and (session['revoked_at'] is not None or session['deadline'] <= self.clock()):
                refusal = 'session_revoked'
                receipt_id = self._receipt_in(db, session, {**details, 'outcome': 'denied', 'reason': refusal,
                    'response_bytes': 0, 'finished_at': self.clock()}, request_id)
            elif exceeded:
                refusal = 'mirror_capacity'
                if session['revoked_at'] is None:
                    receipt_id = self._receipt_in(db, session, {**details, 'outcome': 'denied', 'reason': 'mirror_capacity',
                        'response_bytes': 0, 'finished_at': self.clock()}, request_id)
                    db.execute('UPDATE mirror_sessions SET revoked_at=? WHERE id=?', (self.clock(), identifier))
                else:
                    # A concurrent request already persisted the sole capacity
                    # claim. This admitted request only records cancellation.
                    receipt_id = self._receipt_in(db, session, {**details, 'outcome': 'denied', 'reason': 'session_revoked',
                        'response_bytes': 0, 'finished_at': self.clock()}, request_id)
            else:
                receipt_id = self._receipt_in(db, session, details, request_id)
        if refusal:
            raise MirrorError(refusal)
        return receipt_id

    def preparation_receipt(self, authority, details):
        if not set(details) <= RECEIPT_FIELDS or len(encoded(details)) > 60000:
            raise MirrorError('receipt_invalid')
        identifier = str(uuid.uuid4())
        safe = {**details, 'phase': 'preparation', 'manifest_digest': digest(authority['manifest']),
                'execution_fence': authority['execution_fence']}
        with self.store.write() as db:
            db.execute('INSERT INTO mirror_receipts VALUES(?,NULL,NULL,?,?,?)',
                       (identifier, authority['job_id'], encoded(safe).decode(), self.clock()))
            if details.get('outcome') == 'denied' and details.get('reason') in CLAIMS:
                db.execute('INSERT INTO mirror_claims VALUES(?,?,?,?,?)',
                           (identifier, authority['job_id'], details['reason'], encoded(safe).decode(), self.clock()))
        return identifier

    def receipts(self, job_id):
        with self.store.read_snapshot() as db:
            return [{**dict(row), 'details': json.loads(row['details_json'])} for row in
                    db.execute('SELECT * FROM mirror_receipts WHERE job_id=? ORDER BY created_at,id', (job_id,))]

    def claims(self, job_id):
        with self.store.read_snapshot() as db:
            return [{**dict(row), 'details': json.loads(row['details_json'])} for row in
                    db.execute('SELECT * FROM mirror_claims WHERE job_id=? ORDER BY created_at,receipt_id', (job_id,))]

    def authority_in(self, db, job_id):
        """Read exact live ownership from all execution domains in one snapshot."""
        from .transitions import Lifecycle
        from .recovery import execution_gates
        from .models import TransitionError
        job = db.execute('SELECT j.*,l.execution_fence,l.deadline,a.input_manifest_json,u.repository_id,r.full_name '
            'FROM jobs j JOIN job_launches l ON l.job_id=j.id JOIN attempts a ON a.id=j.attempt_id '
            'JOIN units u ON u.id=j.unit_id JOIN repositories r ON r.id=u.repository_id WHERE j.id=?', (job_id,)).fetchone()
        if job is not None:
            unit = db.execute('SELECT * FROM units WHERE id=?', (job['unit_id'],)).fetchone()
            manifest = json.loads(job['input_manifest_json'])
            try:
                checked, current = Lifecycle(self.store)._job(db, unit, job_id, job['execution_fence'], manifest)
            except TransitionError:
                current = False
            live = (current and not job['fenced'] and not job['cleanup'] and job['outcome'] is None
                    and job['deadline'] > self.clock() and not execution_gates(db, unit, job)
                    and db.execute('SELECT 1 FROM job_leases WHERE job_id=? AND execution_fence=?',
                                   (job_id, job['execution_fence'])).fetchone() is not None)
            if not live:
                raise MirrorError('session_revoked')
            return dict(domain='pipeline', job_id=job_id, execution_fence=job['execution_fence'],
                        deadline=job['deadline'], manifest=manifest, repo_id=job['repository_id'], repo=job['full_name'])
        for domain in ('discussion', 'auxiliary'):
            row = db.execute(f'SELECT e.*,r.manifest_json,l.execution_fence FROM {domain}_executions e '
                f'JOIN {domain}_runtime r ON r.execution_id=e.id JOIN {domain}_leases l ON l.execution_id=e.id WHERE e.id=?',
                (job_id,)).fetchone()
            if row is None:
                continue
            runtime = db.execute(f'SELECT * FROM {domain}_runtime WHERE execution_id=?', (job_id,)).fetchone()
            manifest = json.loads(row['manifest_json'])
            deadline = runtime['deadline'] if domain == 'auxiliary' else manifest['deadline']
            if row['fenced'] or row['cleanup'] or deadline <= self.clock():
                raise MirrorError('session_revoked')
            if domain == 'discussion':
                thread = db.execute('SELECT current_execution_id,cancel_requested FROM discussions WHERE id=?', (row['discussion_id'],)).fetchone()
                if (thread is None or thread['current_execution_id'] != job_id or thread['cancel_requested']
                        or runtime['completed'] or runtime['cancelled'] or runtime['ended'] is not None):
                    raise MirrorError('session_revoked')
            elif runtime['capture_digest'] is not None:
                raise MirrorError('session_revoked')
            return dict(domain=domain, job_id=job_id, execution_fence=row['execution_fence'], deadline=deadline,
                        manifest=manifest, repo_id=manifest['repo_id'], repo=manifest['repo'])
        raise MirrorError('session_revoked')

    def authority(self, job_id):
        with self.store.read_snapshot() as db:
            return self.authority_in(db, job_id)

    def live_authority(self, session):
        try:
            current = self.authority(session['job_id'])
            return (current['execution_fence'] == session['execution_fence']
                    and digest(current['manifest']) == session['source_input']['manifest_digest'])
        except MirrorError:
            return False

    def prepared(self, job_id):
        with self.store.read_snapshot() as db:
            row = db.execute('SELECT i.*,x.source_json,p.policy_json,s.network_generation,s.deadline,s.revoked_at '
                'FROM mirror_job_inputs i JOIN mirror_policies p ON p.digest=i.policy_digest '
                'JOIN mirror_sources x ON x.digest=i.source_digest AND x.policy_digest=i.policy_digest '
                'JOIN mirror_sessions s ON s.id=i.session_id WHERE i.job_id=?', (job_id,)).fetchone()
        if row is None:
            return None
        result = dict(row)
        for old, new in [('source_json', 'source_input'), ('policy_json', 'policy'), ('notes_json', 'notes'),
                         ('publication_authorization_json', 'publication_authorization')]:
            result[new] = json.loads(result.pop(old))
        return result

    def admit(self, job_id, source_input, policy, notes, publication_authorization, *, execution_fence, network_generation, deadline):
        """Bind final immutable inputs and a socket generation under current SQL ownership."""
        source, policy = validate(source_input), validate_policy(policy)
        key, source_digest = policy_digest(policy), digest(source)
        if (source['dependency_policy_digest'] != key or source['manifest_digest'] != policy['manifest_digest']
                or source['repository_id'] != policy['project_repository_id'] or digest(notes) != source['notes_digest']
                or len(encoded(notes)) > 1024 * 1024 or not isinstance(publication_authorization, dict)
                or len(encoded(publication_authorization)) > 65536):
            raise MirrorError('policy_denied')
        with self.store.write() as db:
            authority = self.authority_in(db, job_id)
            if (authority['execution_fence'] != execution_fence or authority['repo_id'] != source['repository_id']
                    or digest(authority['manifest']) != source['manifest_digest'] or deadline != authority['deadline']):
                raise MirrorError('assignment_changed')
            previous = db.execute('SELECT source_digest,session_id FROM mirror_job_inputs WHERE job_id=?', (job_id,)).fetchone()
            if previous:
                if previous['source_digest'] != source_digest:
                    raise MirrorError('assignment_changed')
                return previous['session_id']
            self._source_in(db, source, policy)
            identifier = str(uuid.uuid4())
            db.execute('INSERT INTO mirror_sessions VALUES(?,?,?,?,?,?,?,?,NULL)',
                       (identifier, job_id, execution_fence, network_generation, source_digest, key, deadline, self.clock()))
            db.execute('INSERT INTO mirror_job_inputs VALUES(?,?,?,?,?,?,?,?)',
                       (job_id, execution_fence, source_digest, key, identifier, encoded(notes).decode(),
                        encoded(publication_authorization).decode(), self.clock()))
        return identifier

    def active_sessions(self):
        with self.store.read_snapshot() as db:
            identifiers = [row[0] for row in db.execute('SELECT id FROM mirror_sessions WHERE revoked_at IS NULL AND deadline>? LIMIT 201', (self.clock(),))]
        if len(identifiers) > 200:
            raise MirrorError('mirror_capacity')
        return [self.session(identifier) for identifier in identifiers]
