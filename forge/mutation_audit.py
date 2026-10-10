"""Private, digest-only mutation receipts and independently traceable effects.

The request context is thread-local (ContextVar), never an authentication source.
Only verified identity adapters bind a principal. Caller identities are scoped to
that principal and route; retrying an identical request retains one receipt.

Request lookup currently scans the retained audit table: a request_id index is
a follow-up schema/performance change, not an in-process schema mutation.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import re
import time
import threading
import uuid

_CURRENT = ContextVar('forge_mutation_audit', default=None)
_ACTIVE = set()
_ACTIVE_LOCK = threading.RLock()
_ID = re.compile(r'(?:[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})')
_REVISION = re.compile(r'[a-f0-9]{16,64}')


def current():
    return _CURRENT.get()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def _insert(db, clock, *, request_id, action, outcome, details, operator=None, session=None, connection=None):
    db.execute('INSERT INTO operator_audit(operator_id,session_id,connection_id,request_id,action,outcome,at,details_json) '
               'VALUES(?,?,?,?,?,?,?,?)', (operator, session, connection, request_id, action, outcome, clock(),
                                         json.dumps(details, sort_keys=True, separators=(',', ':'))))


class RequestReplay(ValueError):
    def __init__(self, receipt):
        self.receipt = receipt
        super().__init__('This request was already recorded; reload before sending a new request.')


class MutationAudit:
    def __init__(self, store, route, *, clock=time.time, request_id=None):
        self.store, self.clock = store, clock
        self.route = route if re.fullmatch(r'/api/[a-z/]+', route) else '/api/unknown'
        self.request_id = str(uuid.uuid4())
        self.operator = self.session = self.connection = None
        self.details = {'kind': 'request_outcome', 'actor': 'unverified'}
        self.caller_id = request_id if isinstance(request_id, str) and _ID.fullmatch(request_id) else str(uuid.uuid4())
        self.finished = False
        self.response = None
        self.started = False
        self.failure = None
        self._identity()

    def bind(self, *, session=None, session_id=None, grant=None):
        if session:
            self.operator = str(session['operator']['id'])
            self.session = session_id
            self.details['actor'] = 'github:' + self.operator
        if grant:
            self.operator = str(grant['operator']['id'])
            self.connection = grant['id']
            self.details['actor'] = 'github:' + self.operator
        self._identity()

    def _identity(self):
        self.details['caller_request_id'] = self.caller_id
        if self.caller_id:
            self.request_id = 'mutation:' + digest([self.details['actor'], self.connection, self.route, self.caller_id])

    def describe(self, value):
        # Never persist input values, free-form reasons, cookies, tokens or errors.
        if not isinstance(value, dict):
            return
        identifier = value.get('request_id') or (value.get('id') if self.route == '/api/client/operation' else None)
        if isinstance(identifier, str) and _ID.fullmatch(identifier):
            self.caller_id = identifier
            self.details['caller_request_id'] = identifier
        key = value.get('key')
        repo = value.get('repo_id')
        if isinstance(key, str) and re.fullmatch(r'[0-9]+:[0-9]+', key):
            self.details['target'] = 'issue:' + key
        elif type(repo) is int and repo > 0:
            self.details['target'] = 'repository:' + str(repo)
        elif isinstance(value.get('repository'), dict) and type(value['repository'].get('id')) is int:
            self.details['target'] = 'repository:' + str(value['repository']['id'])
        elif self.route == '/api/runtime':
            self.details['target'] = 'runtime'
        action = value.get('action')
        if isinstance(action, str) and action in {'queue', 'backlog', 'pause', 'resume', 'retry', 'recheck', 'start_anyway', 'environment', 'close', 'assign_reviewer', 'send', 'cancel', 'apply', 'undo', 'enable', 'disable', 'verify', 'check', 'start', 'reset'}:
            self.details['action'] = action
        revision = value.get('revision')
        if isinstance(revision, str) and _REVISION.fullmatch(revision):
            self.details['expected_revision'] = revision
        self._identity()

    def failed(self, reason, *, uncertain=False):
        self.failure = ('unknown' if uncertain or self.details.get('effect_recorded') else 'rejected', reason)

    def result(self, value, status=200):
        accepted = status < 400 and isinstance(value, dict) and value.get('ok') is True
        self.response = ('accepted' if accepted else 'rejected',
                         'accepted' if accepted else 'operation_rejected' if status < 400 else 'http_' + str(status))
        if not accepted and self.failure:
            self.response = self.failure
        if isinstance(value, dict):
            identifier = value.get('request') or value.get('id')
            if isinstance(identifier, str) and _ID.fullmatch(identifier):
                self.details['command_request_id'] = identifier
            error = value.get('error')
            if isinstance(error, str) and re.fullmatch(r'[a-z_]{1,64}', error):
                self.response = (self.response[0], error)

    @property
    def active_key(self):
        return (str(self.store.root), self.request_id)

    def start(self):
        """Durable claim before dispatch; uncertain retries never repeat an effect."""
        self._identity()
        receipt = None
        with _ACTIVE_LOCK, self.store.write() as db:
            row = db.execute("SELECT outcome,details_json FROM operator_audit WHERE request_id=? "
                             "AND json_extract(details_json,'$.kind')='request_outcome' ORDER BY sequence LIMIT 1",
                             (self.request_id,)).fetchone()
            started = db.execute("SELECT * FROM operator_audit WHERE request_id=? "
                                 "AND json_extract(details_json,'$.kind')='request_started' ORDER BY sequence LIMIT 1",
                                 (self.request_id,)).fetchone()
            if row or started:
                prior = json.loads(row['details_json']) if row else {}
                outcome = row['outcome'] if row else 'in_progress' if self.active_key in _ACTIVE else 'unknown'
                if not row and outcome == 'unknown':
                    # This outcome belongs to the interrupted original claim,
                    # never to a changed body or a later session on its retry.
                    self.operator = started['operator_id']
                    self.session = started['session_id']
                    self.connection = started['connection_id']
                    self.route = started['action']
                    self.details = {**json.loads(started['details_json']), 'kind': 'request_outcome'}
                    self.finish_in(db, 'unknown', 'request_interrupted')
                    prior = self.details
                receipt = dict(request_id=self.request_id, outcome=outcome,
                               command_request_id=prior.get('command_request_id'))
            else:
                _insert(db, self.clock, request_id=self.request_id, action=self.route, outcome='started',
                        details={**self.details, 'kind': 'request_started'}, operator=self.operator,
                        session=self.session, connection=self.connection)
                _ACTIVE.add(self.active_key)
                self.started = True
        if receipt:
            self.finished = True
            raise RequestReplay(receipt)

    def release(self):
        if self.started:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(self.active_key)

    def finish_in(self, db, outcome, reason):
        # The same writer transaction serializes receipt insertion and retries.
        # Identity/lifecycle effects have their own durable operation records.
        # No request body, response body, exception text or credential is copied.
        if db.execute("SELECT 1 FROM operator_audit WHERE request_id=? AND json_extract(details_json,'$.kind')='request_outcome'",
                      (self.request_id,)).fetchone():
            return
        linked = db.execute("SELECT details_json FROM operator_audit WHERE request_id=? AND json_extract(details_json,'$.kind')='command_effect' ORDER BY sequence DESC LIMIT 1", (self.request_id,)).fetchone()
        if linked and 'command_request_id' not in self.details:
            self.details['command_request_id'] = json.loads(linked[0])['command_request_id']
        _insert(db, self.clock, request_id=self.request_id, action=self.route, outcome=outcome,
                details={**self.details, 'reason_code': reason}, operator=self.operator,
                session=self.session, connection=self.connection)

    def finish(self):
        if not self.finished:
            with self.store.write() as db:
                self.finish_in(db, *(self.response or ('unknown' if self.started else 'rejected', 'request_interrupted')))
            self.finished = True


@contextmanager
def request_context(context):
    token = _CURRENT.set(context)
    try:
        yield context
    finally:
        if context:
            context.release()
        _CURRENT.reset(token)


def config_change_in(db, *, domain, subject, before, after, revision, clock=time.time):
    """Write within the same transaction as the configuration revision."""
    context = current()
    if context:
        context.details['effect_recorded'] = True
    details = dict(kind='configuration_change', actor=context.details['actor'] if context else 'local:host',
                   domain=domain, subject=subject, revision=revision,
                   before_digest=digest(before), after_digest=digest(after))
    _insert(db, clock, request_id=context.request_id if context else str(uuid.uuid4()),
            action='configuration_changed', outcome='applied', details=details,
            operator=context.operator if context else None, session=context.session if context else None,
            connection=context.connection if context else None)


def command_effect_in(db, command, *, clock=time.time):
    """Link request to a committed lifecycle decision without copying its payload."""
    context = current()
    if context is None:
        return
    context.details['effect_recorded'] = True
    context.details['command_request_id'] = command.request_id
    _insert(db, clock, request_id=context.request_id, action='lifecycle_command', outcome='applied',
            details=dict(kind='command_effect', command_request_id=command.request_id,
                         unit_id=command.unit_id, expected_version=command.expected_version),
            operator=context.operator, session=context.session, connection=context.connection)
