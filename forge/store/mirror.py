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
            row = db.execute('SELECT * FROM mirror_policies WHERE digest=?', (key,)).fetchone()
            if row is not None and row['source_digest'] != source_digest:
                # Policy includes assignment manifest identity. Reusing its key
                # for different immutable notes/inputs would destroy attribution.
                raise MirrorError('policy_denied')
            db.execute('INSERT OR IGNORE INTO mirror_policies VALUES(?,?,?,?,?)',
                       (key, source_digest, encoded(source).decode(), encoded(policy).decode(), self.clock()))
        return key

    def bind_session(self, *, job_id, execution_fence, network_generation, deadline, source_digest, policy_digest):
        if (any(not isinstance(v, str) or not 0 < len(v) <= 200 for v in (job_id, execution_fence, network_generation))
                or type(deadline) not in (int, float) or not self.clock() < deadline <= self.clock() + 86400):
            raise MirrorError('session_invalid')
        with self.store.write() as db:
            policy = db.execute('SELECT source_digest FROM mirror_policies WHERE digest=?', (policy_digest,)).fetchone()
            if policy is None or policy[0] != source_digest:
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
            row = db.execute('SELECT s.*,p.policy_json,p.source_json FROM mirror_sessions s JOIN mirror_policies p ON p.digest=s.policy_digest WHERE s.id=?',
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

    def append_receipt(self, identifier, details):
        if not isinstance(details, dict) or not set(details) <= RECEIPT_FIELDS or len(encoded(details)) > 60000:
            raise MirrorError('receipt_invalid')
        receipt_id = str(uuid.uuid4())
        with self.store.write() as db:
            session = db.execute('SELECT * FROM mirror_sessions WHERE id=?', (identifier,)).fetchone()
            if session is None:
                raise MirrorError('session_invalid')
            if details.get('outcome') in ('started', 'served'):
                usage = db.execute("SELECT sum(json_extract(details_json,'$.outcome')='started'), "
                    "sum(CASE WHEN json_extract(details_json,'$.outcome')='started' THEN json_extract(details_json,'$.request_bytes') ELSE 0 END), "
                    "sum(CASE WHEN json_extract(details_json,'$.outcome')='served' THEN json_extract(details_json,'$.response_bytes') ELSE 0 END) "
                    'FROM mirror_receipts WHERE session_id=?', (identifier,)).fetchone()
                requests, received, sent = (value or 0 for value in usage)
                if (details['outcome'] == 'started' and (requests >= self.max_requests or received + details.get('request_bytes', 0) > self.max_request_bytes)
                        or details['outcome'] == 'served' and sent + details.get('response_bytes', 0) > self.max_response_bytes):
                    raise MirrorError('mirror_capacity')
            safe = {**details, 'source_digest': session['source_digest'], 'policy_digest': session['policy_digest'],
                    'execution_fence': session['execution_fence'], 'network_generation': session['network_generation']}
            db.execute('INSERT INTO mirror_receipts VALUES(?,?,?,?,?)',
                       (receipt_id, identifier, session['job_id'], encoded(safe).decode(), self.clock()))
            if details.get('outcome') == 'denied' and details.get('reason') in CLAIMS:
                db.execute('INSERT INTO mirror_claims VALUES(?,?,?,?,?)',
                           (receipt_id, session['job_id'], details['reason'], encoded(safe).decode(), self.clock()))
        return receipt_id

    def receipts(self, job_id):
        with self.store.read_snapshot() as db:
            return [{**dict(row), 'details': json.loads(row['details_json'])} for row in
                    db.execute('SELECT * FROM mirror_receipts WHERE job_id=? ORDER BY created_at,id', (job_id,))]

    def claims(self, job_id):
        with self.store.read_snapshot() as db:
            return [{**dict(row), 'details': json.loads(row['details_json'])} for row in
                    db.execute('SELECT * FROM mirror_claims WHERE job_id=? ORDER BY created_at,receipt_id', (job_id,))]
