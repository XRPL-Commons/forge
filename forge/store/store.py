"""Forge's private SQLite store (#209, phase 1).

Inactive: nothing in the supervisor, API or UI reads or writes it yet.

One database at `<root>/state/forge.sqlite3`, mode 0600 inside a 0700 directory,
in WAL mode with `synchronous=FULL` and foreign keys on. Writers take
`BEGIN IMMEDIATE`; readers take a snapshot. Never call GitHub, a container or a
provider while holding a transaction. One `Store` object serves one thread.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import time
from urllib.parse import quote
import uuid
import zlib

from . import codes, schema, status
from forge.state import atomic_json

DATABASE = 'forge.sqlite3'
ANCHOR = 'forge.sqlite3.anchor'
FORMAT = 'forge-store/1'
MIN_SQLITE = (3, 37, 0)  # STRICT tables
BUSY_TIMEOUT = 5.0
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
LIVE = codes.LIVE_JOB_PHASES


class StoreError(RuntimeError):
    """A coded store failure. `retryable` errors may be retried with the same request id."""
    code = 'store_error'
    retryable = False

    def __init__(self, message, *, code=None):
        super().__init__(message)
        if code:
            self.code = code


class StoreBusy(StoreError):
    code = 'store_busy'
    retryable = True


class StaleVersion(StoreError):
    code = 'stale_version'


class IllegalTransition(StoreError):
    code = 'illegal_transition'


class InvariantViolation(StoreError):
    code = 'invariant_violation'


class NotFound(StoreError):
    code = 'not_found'


REJECTIONS = {cls.code: cls for cls in (StaleVersion, IllegalTransition, InvariantViolation, NotFound)}


def _dumps(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _require_private(path: Path, info, kind):
    if stat.S_ISLNK(info.st_mode):
        raise StoreError(f'{path.name} must not be a symlink', code='unsafe_path')
    if not kind(info.st_mode):
        raise StoreError(f'{path.name} has an unexpected file type', code='unsafe_path')
    if info.st_uid != os.geteuid():
        raise StoreError(f'{path.name} must be owned by the Forge service account', code='unsafe_path')
    if info.st_mode & 0o077:
        raise StoreError(f'{path.name} must not be readable by group or others', code='unsafe_path')


class Store:
    def __init__(self, root, *, create=False, clock=time.time, busy_timeout=BUSY_TIMEOUT):
        if sqlite3.sqlite_version_info < MIN_SQLITE:
            raise StoreError(f'SQLite {sqlite3.sqlite_version} is too old', code='sqlite_unsupported')
        self.root = Path(root)
        self.directory = self.root / 'state'
        self.path = self.directory / DATABASE
        self.anchor = self.directory / ANCHOR
        self.clock = clock
        self.busy_timeout = busy_timeout
        self._depth = 0
        self._tx = None
        self.db = None
        fresh = self._check_paths(create)
        if fresh:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.fchmod(fd, 0o600)
            os.close(fd)
        self.db = self._connect()
        try:
            self._migrate()
            self._check_anchor(create)
        except BaseException:
            self.close()
            raise

    # --- Opening -------------------------------------------------------------

    def _check_paths(self, create):
        """Refuse symlinked, foreign or group-readable state; report whether to initialize."""
        try:
            info = os.lstat(self.directory)
        except FileNotFoundError:
            if not create:
                raise StoreError('The Forge store is not initialized', code='store_missing') from None
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.mkdir(self.directory, 0o700)
            info = os.lstat(self.directory)
        _require_private(self.directory, info, stat.S_ISDIR)
        for path in (self.path, self.anchor, Path(f'{self.path}-wal'), Path(f'{self.path}-shm')):
            try:
                _require_private(path, os.lstat(path), stat.S_ISREG)
            except FileNotFoundError:
                pass
        database, anchor = self.path.exists(), self.anchor.exists()
        if database:
            return False
        if anchor:
            # Never silently start over after the initialized database disappeared.
            raise StoreError('The initialized Forge database is missing; recover it before continuing',
                             code='store_lost')
        if not create:
            raise StoreError('The Forge store is not initialized', code='store_missing')
        return True

    def _connect(self):
        uri = f'file:{quote(str(self.path.resolve()))}?mode=rw&nofollow=1'
        db = sqlite3.connect(uri, uri=True, timeout=self.busy_timeout, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute(f'PRAGMA busy_timeout = {int(self.busy_timeout * 1000)}')
            if db.execute('PRAGMA journal_mode = WAL').fetchone()[0] != 'wal':
                raise StoreError('WAL mode could not be enabled', code='store_settings')
            db.execute('PRAGMA synchronous = FULL')
            db.execute('PRAGMA foreign_keys = ON')
            db.execute('PRAGMA trusted_schema = OFF')
            if (db.execute('PRAGMA synchronous').fetchone()[0], db.execute('PRAGMA foreign_keys').fetchone()[0]) != (2, 1):
                raise StoreError('Required SQLite settings could not be established', code='store_settings')
        except BaseException:
            db.close()
            raise
        return db

    def _migrate(self):
        known = {version: (name, sql) for version, name, sql in schema.MIGRATIONS}
        with self.write() as db:
            applied = {}
            if db.execute("SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'schema_migrations'").fetchone():
                applied = {row['version']: row['checksum'] for row in db.execute('SELECT version, checksum FROM schema_migrations')}
            if set(applied) - set(known):
                raise StoreError('The Forge database was written by a newer release', code='schema_newer')
            for version, checksum in applied.items():
                if schema.checksum(known[version][1]) != checksum:
                    raise StoreError(f'Schema migration {version} does not match this release', code='schema_mismatch')
            for version in sorted(set(known) - set(applied)):
                name, sql = known[version]
                for statement in schema.statements(sql):
                    db.execute(statement)
                db.execute('INSERT INTO schema_migrations VALUES (?, ?, ?, ?)',
                           (version, name, schema.checksum(sql), self.clock()))
            db.execute(f'PRAGMA user_version = {max(known)}')
            if not db.execute("SELECT 1 FROM store_metadata WHERE key = 'instance_id'").fetchone():
                db.executemany('INSERT INTO store_metadata VALUES (?, ?)',
                               [('instance_id', uuid.uuid4().hex), ('format', FORMAT)])

    def _check_anchor(self, create):
        """The anchor outside the database tells a lost store apart from a first install."""
        with self.read() as db:
            instance = db.execute("SELECT value FROM store_metadata WHERE key = 'instance_id'").fetchone()[0]
            started = db.execute('SELECT 1 FROM events LIMIT 1').fetchone()
        expected = {'format': FORMAT, 'instance_id': instance}
        if not self.anchor.exists():
            if not create or started:
                raise StoreError('The Forge store anchor is missing; recover it before continuing', code='anchor_missing')
            atomic_json(self.anchor, expected)  # completes an interrupted first initialization
            os.chmod(self.anchor, 0o600)
        try:
            found = json.loads(self.anchor.read_text())
        except (OSError, ValueError):
            found = None
        if found != expected:
            raise StoreError('This database does not belong to this Forge installation', code='anchor_mismatch')

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # --- Transactions --------------------------------------------------------

    def write(self):
        return _Write(self)

    def read(self):
        return _Read(self)

    def _event(self, kind, *, unit_id=None, issue_id=None, job_id=None, candidate_id=None,
               actor=None, request_id=None, **payload):
        if kind not in codes.EVENT_KINDS:
            raise StoreError(f'Unknown event kind {kind!r}', code='unknown_code')
        self.db.execute(
            'INSERT INTO events (transaction_id, unit_id, issue_id, job_id, candidate_id, kind, at, actor,'
            ' request_id, payload_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (self._tx, unit_id, issue_id, job_id, candidate_id, kind, self.clock(), actor, request_id, _dumps(payload)))

    def _decide(self, request_id, actor, command, unit_id, expected_version, params, apply):
        """Apply a command once per request id. A replay returns the recorded result or rejection."""
        if request_id is None:
            with self.write():
                return apply()
        rejection = None
        with self.write() as db:
            prior = db.execute('SELECT * FROM decisions WHERE request_id = ?', (request_id,)).fetchone()
            if prior is not None:
                if prior['command'] != command or prior['unit_id'] != unit_id:
                    raise StoreError('This request id was already used for a different command', code='request_reused')
                if prior['status'] == 'applied':
                    return json.loads(prior['result_json'])
                rejection = REJECTIONS.get(prior['error_code'], StoreError)(
                    json.loads(prior['result_json'] or '{}').get('message', 'Rejected'), code=prior['error_code'])
            else:
                db.execute('SAVEPOINT decision')
                try:
                    result = apply()
                except (StoreError, sqlite3.IntegrityError) as error:
                    db.execute('ROLLBACK TO decision')
                    db.execute('RELEASE decision')
                    if isinstance(error, sqlite3.IntegrityError):
                        error = InvariantViolation(str(error))
                    if error.retryable:
                        raise error
                    rejection = error
                    db.execute('INSERT INTO decisions (id, request_id, unit_id, actor, command, params_json,'
                               ' expected_version, status, result_json, error_code, created_at)'
                               " VALUES (?, ?, ?, ?, ?, ?, ?, 'rejected', ?, ?, ?)",
                               (uuid.uuid4().hex, request_id, unit_id, actor, command, _dumps(params),
                                expected_version, _dumps({'message': str(error)}), error.code, self.clock()))
                    self._event('decision_rejected', unit_id=unit_id, actor=actor, request_id=request_id,
                                command=command, error=error.code)
                else:
                    db.execute('RELEASE decision')
                    db.execute('INSERT INTO decisions (id, request_id, unit_id, actor, command, params_json,'
                               ' expected_version, status, result_json, created_at)'
                               " VALUES (?, ?, ?, ?, ?, ?, ?, 'applied', ?, ?)",
                               (uuid.uuid4().hex, request_id, unit_id, actor, command, _dumps(params),
                                expected_version, _dumps(result), self.clock()))
                    self._event('decision_applied', unit_id=unit_id, actor=actor, request_id=request_id, command=command)
                    return result
        raise rejection

    # --- Repositories and issues --------------------------------------------

    def ensure_repository(self, full_name, repository_id=None):
        with self.write() as db:
            row = db.execute('SELECT id FROM repositories WHERE full_name = ?', (full_name,)).fetchone()
            if row:
                return row['id']
            return db.execute('INSERT INTO repositories (id, full_name) VALUES (?, ?)', (repository_id, full_name)).lastrowid

    def observe_issue(self, repository_id, number, *, state, title, body, requirements,
                      remote_updated_at, generation, state_reason=None, labels=(), assignees=()):
        """Record a GitHub issue observation. An older observation never replaces a newer one."""
        issue_id = f'{repository_id}:{number}'
        content = hashlib.sha256(_dumps([title, body]).encode()).hexdigest()
        requirements_json = _dumps(requirements)
        wanted = hashlib.sha256(requirements_json.encode()).hexdigest()
        with self.write() as db:
            now = self.clock()
            current = db.execute('SELECT * FROM issues WHERE id = ?', (issue_id,)).fetchone()
            if current and (remote_updated_at, generation) < (current['remote_updated_at'], current['observation_generation']):
                return {'issue_id': issue_id, 'stale': True, 'revision_id': current['current_revision_id']}
            revision = db.execute('SELECT id FROM issue_revisions WHERE issue_id = ? AND content_hash = ? AND requirements_hash = ?',
                                  (issue_id, content, wanted)).fetchone()
            revision_id = revision['id'] if revision else uuid.uuid4().hex
            if not revision:
                db.execute('INSERT INTO issue_revisions VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                           (revision_id, issue_id, title, body, content, wanted, requirements_json, now))
            values = (state, state_reason, _dumps(sorted(labels)), _dumps(sorted(assignees)), remote_updated_at,
                      generation, revision_id, now)
            if current:
                db.execute('UPDATE issues SET github_state = ?, state_reason = ?, labels_json = ?, assignees_json = ?,'
                           ' remote_updated_at = ?, observation_generation = ?, current_revision_id = ?, observed_at = ?,'
                           ' version = version + 1 WHERE id = ?', (*values, issue_id))
            else:
                db.execute('INSERT INTO issues (github_state, state_reason, labels_json, assignees_json, remote_updated_at,'
                           ' observation_generation, current_revision_id, observed_at, id, repository_id, github_number)'
                           ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (*values, issue_id, repository_id, number))
            return {'issue_id': issue_id, 'stale': False, 'revision_id': revision_id}

    # --- Units: admission and typed transitions ------------------------------

    def admit(self, issue_id, *, base_branch='main', actor, request_id=None):
        """Create a queued unit of one for an open issue that has no live membership."""
        def apply():
            unit_id = self._admit(issue_id, base_branch, actor, request_id)
            return {'unit_id': unit_id, 'state': 'queued', 'version': 1}
        return self._decide(request_id, actor, 'admit', None, None, {'issue_id': issue_id}, apply)

    def _admit(self, issue_id, base_branch, actor, request_id):
        issue = self.db.execute('SELECT * FROM issues WHERE id = ?', (issue_id,)).fetchone()
        if issue is None:
            raise NotFound(f'Issue {issue_id} is not known')
        if issue['github_state'] != 'open':
            raise IllegalTransition(f'Issue {issue_id} is closed on GitHub')
        if self.db.execute('SELECT 1 FROM unit_issues WHERE issue_id = ? AND left_at IS NULL', (issue_id,)).fetchone():
            raise InvariantViolation(f'Issue {issue_id} already belongs to an open unit')
        now, unit_id = self.clock(), uuid.uuid4().hex
        self.db.execute("INSERT INTO units (id, repository_id, base_branch, state, created_at, updated_at)"
                        " VALUES (?, ?, ?, 'queued', ?, ?)", (unit_id, issue['repository_id'], base_branch, now, now))
        self.db.execute("INSERT INTO unit_issues (id, unit_id, issue_id, repository_id, base_branch, requirements_revision_id,"
                        " state, joined_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
                        (uuid.uuid4().hex, unit_id, issue_id, issue['repository_id'], base_branch,
                         issue['current_revision_id'], now))
        self._event('unit_created', unit_id=unit_id, issue_id=issue_id, actor=actor, request_id=request_id, state='queued')
        self._event('admitted', unit_id=unit_id, issue_id=issue_id, actor=actor, request_id=request_id)
        return unit_id

    def _unit(self, unit_id):
        unit = self.db.execute('SELECT * FROM units WHERE id = ?', (unit_id,)).fetchone()
        if unit is None:
            raise NotFound(f'Unit {unit_id} is not known')
        return unit

    def _live_members(self, unit_id):
        return self.db.execute('SELECT * FROM unit_issues WHERE unit_id = ? AND left_at IS NULL ORDER BY joined_at, id',
                               (unit_id,)).fetchall()

    def _bump(self, unit, expected_version, **columns):
        if unit['version'] != expected_version:
            raise StaleVersion(f'Unit {unit["id"]} is at version {unit["version"]}, not {expected_version}')
        columns.update(version=unit['version'] + 1, updated_at=self.clock())
        names = ', '.join(f'{name} = ?' for name in columns)
        if self.db.execute(f'UPDATE units SET {names} WHERE id = ? AND version = ?',
                           (*columns.values(), unit['id'], unit['version'])).rowcount != 1:
            raise StaleVersion(f'Unit {unit["id"]} changed concurrently')
        return columns['version']

    def _allowed(self, unit_id, to_state, expected_version):
        unit = self._unit(unit_id)
        if unit['version'] != expected_version:
            raise StaleVersion(f'Unit {unit_id} is at version {unit["version"]}, not {expected_version}')
        if to_state not in codes.TRANSITIONS[unit['state']]:
            raise IllegalTransition(f'A {unit["state"]} unit cannot move to {to_state}')
        return unit

    def _transition(self, unit_id, to_state, expected_version, actor, request_id, *, check=None, **columns):
        unit = self._allowed(unit_id, to_state, expected_version)
        if check:
            check(unit)
        if unit['state'] in codes.CANDIDATE_STATES and to_state in codes.REWORK_STATES:
            # Rework invalidates the candidate's gates; evidence stays with that candidate.
            self._resolve_candidate_gates(unit_id, 'superseded')
        if to_state in codes.TERMINAL_STATES:
            self._settle(unit, to_state, actor, request_id)
        version = self._bump(unit, expected_version, state=to_state, **columns)
        self._event('phase_changed', unit_id=unit_id, actor=actor, request_id=request_id,
                    before=unit['state'], after=to_state, version=version, **columns)
        if to_state in ('merged', 'closed', 'dissolved'):
            self._event(to_state, unit_id=unit_id, actor=actor, request_id=request_id, **columns)
        return {'unit_id': unit_id, 'state': to_state, 'version': version}

    def _settle(self, unit, to_state, actor, request_id):
        """Before a unit ends: fence its live jobs, resolve its gates and release its members."""
        now = self.clock()
        for job in self.db.execute(f'SELECT id FROM jobs WHERE unit_id = ? AND fenced = 0 AND phase IN {LIVE}',
                                   (unit['id'],)).fetchall():
            self.db.execute('UPDATE jobs SET fenced = 1 WHERE id = ?', (job['id'],))
            self._event('attempt_fenced', unit_id=unit['id'], job_id=job['id'], actor=actor, request_id=request_id)
        for gate in self.db.execute('SELECT id, code FROM unit_gates WHERE unit_id = ? AND resolved_at IS NULL',
                                    (unit['id'],)).fetchall():
            self.db.execute("UPDATE unit_gates SET resolved_at = ?, resolution = 'superseded' WHERE id = ?", (now, gate['id']))
            self._event('gate_resolved', unit_id=unit['id'], actor=actor, request_id=request_id,
                        gate=gate['id'], code=gate['code'], resolution='superseded')
        reason = {'merged': 'merged', 'closed': 'closed', 'dissolved': 'dissolved'}[to_state]
        for member in self._live_members(unit['id']):
            part = 'done' if to_state == 'merged' else member['state']
            self.db.execute('UPDATE unit_issues SET state = ?, left_at = ?, exit_reason = ? WHERE id = ?',
                            (part, now, reason, member['id']))
            self._event('membership_changed', unit_id=unit['id'], issue_id=member['issue_id'], actor=actor,
                        request_id=request_id, member=member['id'], exit_reason=reason)

    def _resolve_candidate_gates(self, unit_id, resolution):
        for gate in self.db.execute('SELECT g.id, g.code FROM unit_gates g JOIN gate_codes c ON c.code = g.code'
                                    " WHERE g.unit_id = ? AND g.resolved_at IS NULL AND c.kind = 'candidate'",
                                    (unit_id,)).fetchall():
            self.db.execute('UPDATE unit_gates SET resolved_at = ?, resolution = ? WHERE id = ?',
                            (self.clock(), resolution, gate['id']))
            self._event('gate_resolved', unit_id=unit_id, gate=gate['id'], code=gate['code'], resolution=resolution)

    def _command(self, command, unit_id, to_state, expected_version, actor, request_id, params=None, **kwargs):
        return self._decide(request_id, actor, command, unit_id, expected_version, params or {},
                            lambda: self._transition(unit_id, to_state, expected_version, actor, request_id, **kwargs))

    def build(self, unit_id, *, expected_version, actor, request_id=None):
        """Start building (queued), or rework after a rejection or a base conflict."""
        return self._command('build', unit_id, 'building', expected_version, actor, request_id)

    def integrate(self, unit_id, *, expected_version, actor, request_id=None):
        """All parts are built: combine them into one candidate. Batches only."""
        def check(unit):
            if len(self._live_members(unit_id)) < 2:
                raise IllegalTransition('A unit of one publishes its candidate directly and reworks by building')
        return self._command('integrate', unit_id, 'integrating', expected_version, actor, request_id, check=check)

    def review(self, unit_id, *, candidate, expected_version, actor, request_id=None):
        """Publish a valid candidate and review it. The candidate becomes current."""
        fields = ('pull_number', 'branch', 'head_oid', 'base_oid', 'policy_hash', 'requirements_hash', 'input_manifest_hash')
        missing = [name for name in fields if not candidate.get(name)]
        if missing:
            raise StoreError(f'Candidate is missing {", ".join(missing)}', code='invalid_candidate')

        def apply():
            unit = self._allowed(unit_id, 'reviewing', expected_version)
            candidate_id = uuid.uuid4().hex
            self.db.execute('INSERT INTO candidates (id, unit_id, pull_number, branch, head_oid, base_oid, policy_hash,'
                            ' requirements_hash, membership_generation, input_manifest_hash, accepted_at)'
                            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                            (candidate_id, unit_id, *(candidate[name] for name in fields[:-1]),
                             unit['membership_generation'], candidate['input_manifest_hash'], self.clock()))
            if unit['current_candidate_id']:
                self._resolve_candidate_gates(unit_id, 'superseded')
                self._event('candidate_superseded', unit_id=unit_id, candidate_id=unit['current_candidate_id'],
                            actor=actor, request_id=request_id)
            self._event('candidate_selected', unit_id=unit_id, candidate_id=candidate_id, actor=actor,
                        request_id=request_id, pull=candidate['pull_number'], head=candidate['head_oid'])
            result = self._transition(unit_id, 'reviewing', expected_version, actor, request_id,
                                      current_candidate_id=candidate_id)
            return {**result, 'candidate_id': candidate_id}
        return self._decide(request_id, actor, 'review', unit_id, expected_version,
                            {name: candidate[name] for name in fields}, apply)

    def _needs_candidate(self, unit):
        if not unit['current_candidate_id']:
            raise IllegalTransition('This step needs a current candidate')

    def audit(self, unit_id, *, expected_version, actor, request_id=None):
        """The review passed for the current candidate."""
        return self._command('audit', unit_id, 'auditing', expected_version, actor, request_id, check=self._needs_candidate)

    def merge(self, unit_id, *, expected_version, actor, request_id=None):
        """The audit passed: the current candidate waits for its merge gates."""
        return self._command('merge', unit_id, 'merging', expected_version, actor, request_id, check=self._needs_candidate)

    def merged(self, unit_id, *, merge_sha, expected_version, actor, request_id=None):
        """GitHub independently confirmed that the current candidate merged."""
        if not merge_sha:
            raise StoreError('A confirmed merge needs its merge commit', code='invalid_merge')
        return self._command('merged', unit_id, 'merged', expected_version, actor, request_id,
                             {'merge_sha': merge_sha}, check=self._needs_candidate, merge_sha=merge_sha)

    def close_unit(self, unit_id, *, close_code, expected_version, actor, request_id=None):
        """End delivery without a merge, for a coded reason."""
        if close_code not in codes.CLOSE_CODES:
            raise StoreError(f'Unknown close code {close_code!r}', code='unknown_code')
        return self._command('close', unit_id, 'closed', expected_version, actor, request_id,
                             {'close_code': close_code}, close_code=close_code)

    def dissolve(self, unit_id, *, expected_version, actor, request_id=None):
        """Release a batch's members to new queued units of one, before any merge."""
        def apply():
            members = self._live_members(unit_id)
            if len(members) < 2:
                raise IllegalTransition('Only a unit with several issues can be dissolved')
            unit = self._unit(unit_id)
            result = self._transition(unit_id, 'dissolved', expected_version, actor, request_id)
            released = [self._admit(member['issue_id'], unit['base_branch'], actor, request_id) for member in members]
            return {**result, 'released': released}
        return self._decide(request_id, actor, 'dissolve', unit_id, expected_version, {}, apply)

    # --- Gates ---------------------------------------------------------------

    def open_gate(self, unit_id, code, *, member_id=None, candidate_id=None, details=None, actor=None):
        """Open a coded gate. An unresolved gate of the same code and subject is reused."""
        gate = codes.GATES.get(code)
        if gate is None:
            raise StoreError(f'Unknown gate code {code!r}', code='unknown_code')
        if member_id and 'member' not in gate.scopes:
            raise InvariantViolation(f'{code} applies to the whole unit')
        subject = f'member:{member_id}' if member_id else 'unit'
        with self.write() as db:
            unit = self._unit(unit_id)
            if unit['state'] in codes.TERMINAL_STATES:
                raise IllegalTransition('A finished unit cannot gain gates')
            if gate.candidate:
                candidate_id = candidate_id or unit['current_candidate_id']
                if not candidate_id:
                    raise IllegalTransition(f'{code} needs a current candidate')
            existing = db.execute('SELECT id FROM unit_gates WHERE unit_id = ? AND code = ? AND subject_key = ?'
                                  ' AND resolved_at IS NULL', (unit_id, code, subject)).fetchone()
            if existing:
                return existing['id']
            gate_id = uuid.uuid4().hex
            db.execute('INSERT INTO unit_gates (id, unit_id, candidate_id, member_id, subject_key, code, details_json,'
                       ' created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (gate_id, unit_id, candidate_id, member_id, subject, code, _dumps(details or {}), self.clock()))
            self._bump(unit, unit['version'])
            self._event('gate_opened', unit_id=unit_id, candidate_id=candidate_id, actor=actor, gate=gate_id,
                        code=code, subject=subject)
            return gate_id

    def resolve_gate(self, gate_id, *, release, actor=None):
        """Resolve one gate by its own release rule; a different rule is refused."""
        with self.write() as db:
            gate = db.execute('SELECT * FROM unit_gates WHERE id = ?', (gate_id,)).fetchone()
            if gate is None:
                raise NotFound(f'Gate {gate_id} is not known')
            if gate['resolved_at'] is not None:
                return False
            if release != codes.GATES[gate['code']].release:
                raise StoreError(f'{gate["code"]} is released by {codes.GATES[gate["code"]].release}, not {release}',
                                 code='release_refused')
            db.execute('UPDATE unit_gates SET resolved_at = ?, resolution = ? WHERE id = ?', (self.clock(), release, gate_id))
            unit = self._unit(gate['unit_id'])
            self._bump(unit, unit['version'])
            self._event('gate_resolved', unit_id=gate['unit_id'], actor=actor, gate=gate_id, code=gate['code'],
                        resolution=release)
            return True

    def _blocker(self, unit_id):
        rows = self.db.execute('SELECT g.*, c.priority FROM unit_gates g JOIN gate_codes c ON c.code = g.code'
                               ' WHERE g.unit_id = ? AND g.resolved_at IS NULL'
                               ' ORDER BY c.priority, g.member_id IS NOT NULL, g.created_at, g.id LIMIT 1',
                               (unit_id,)).fetchall()
        if not rows:
            return None
        gate = rows[0]
        details = json.loads(gate['details_json'])
        return {'id': gate['id'], 'code': gate['code'], 'subject': gate['subject_key'], 'details': details,
                'plain': status.describe(gate['code'], **details)}

    # --- Jobs, leases and artifacts ------------------------------------------

    def reserve_job(self, unit_id, *, role, slot, member_id=None, candidate_id=None, input_manifest=None, actor=None):
        """Reserve an attempt, its job and its slot lease before any launch."""
        if role not in codes.JOB_ROLES:
            raise StoreError(f'Unknown job role {role!r}', code='unknown_code')
        with self.write() as db:
            unit = self._unit(unit_id)
            if unit['state'] in codes.TERMINAL_STATES:
                raise IllegalTransition('A finished unit cannot start jobs')
            if role in ('review', 'audit', 'merge'):
                candidate_id = candidate_id or unit['current_candidate_id']
                if candidate_id != unit['current_candidate_id'] or not candidate_id:
                    raise IllegalTransition(f'A {role} job must use the current candidate')
            attempt_id, job_id, fence, now = uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex, self.clock()
            db.execute('INSERT INTO attempts VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                       (attempt_id, unit_id, member_id, unit['membership_generation'], unit['fencing_generation'],
                        _dumps(input_manifest or {}), unit['budget_generation'], now))
            db.execute("INSERT INTO jobs (id, unit_id, attempt_id, member_id, candidate_id, role, phase, created_at)"
                       " VALUES (?, ?, ?, ?, ?, ?, 'reserved', ?)", (job_id, unit_id, attempt_id, member_id, candidate_id, role, now))
            db.execute('INSERT INTO job_leases VALUES (?, ?, ?, ?)', (slot, job_id, fence, now))
            self._event('attempt_reserved', unit_id=unit_id, job_id=job_id, candidate_id=candidate_id, actor=actor,
                        role=role, slot=slot, member=member_id)
            return {'job_id': job_id, 'attempt_id': attempt_id, 'execution_fence': fence}

    def finish_job(self, job_id, outcome, *, actor=None):
        with self.write() as db:
            job = self._job(job_id)
            if job['phase'] not in LIVE:
                return False
            db.execute("UPDATE jobs SET phase = 'finished', outcome = ?, finished_at = ? WHERE id = ?", (outcome, self.clock(), job_id))
            self._event('attempt_finished', unit_id=job['unit_id'], job_id=job_id, actor=actor, outcome=outcome)
            return True

    def fence_job(self, job_id, *, actor=None):
        with self.write() as db:
            job = self._job(job_id)
            if job['fenced']:
                return False
            db.execute('UPDATE jobs SET fenced = 1 WHERE id = ?', (job_id,))
            self._event('attempt_fenced', unit_id=job['unit_id'], job_id=job_id, actor=actor)
            return True

    def release_lease(self, job_id, *, actor=None):
        """Record verified cleanup and free the slot."""
        with self.write() as db:
            job = self._job(job_id)
            if job['cleanup']:
                return False
            db.execute("UPDATE jobs SET fenced = 1, cleanup = 1, phase = 'cleaned', finished_at = coalesce(finished_at, ?)"
                       ' WHERE id = ?', (self.clock(), job_id))
            db.execute('DELETE FROM job_leases WHERE job_id = ?', (job_id,))
            self._event('lease_released', unit_id=job['unit_id'], job_id=job_id, actor=actor)
            return True

    def _job(self, job_id):
        job = self.db.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        if job is None:
            raise NotFound(f'Job {job_id} is not known')
        return job

    def put_artifact(self, job_id, kind, data: bytes, *, media_type='text/plain', sequence=None):
        if codes.ARTIFACT_KINDS.get(kind) is None:
            raise StoreError(f'Unknown artifact kind {kind!r}', code='unknown_code')
        if len(data) > MAX_ARTIFACT_BYTES:
            raise StoreError('Artifact exceeds the size limit', code='artifact_too_large')
        packed = zlib.compress(data, 6)
        codec, content = ('zlib', packed) if len(packed) < len(data) else ('identity', bytes(data))
        with self.write() as db:
            if sequence is None:
                sequence = db.execute('SELECT coalesce(max(sequence) + 1, 0) FROM job_artifacts WHERE job_id = ? AND kind = ?',
                                      (job_id, kind)).fetchone()[0]
            db.execute('INSERT INTO job_artifacts (job_id, kind, sequence, media_type, codec, raw_size, digest, content,'
                       ' size, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                       (job_id, kind, sequence, media_type, codec, len(data), hashlib.sha256(data).hexdigest(),
                        content, len(content), self.clock()))
        return sequence

    def read_artifact(self, job_id, kind, sequence=0, *, limit=MAX_ARTIFACT_BYTES):
        """Return {'status', 'content'}: available, expired, missing, too_large or corrupt."""
        with self.read() as db:
            row = db.execute('SELECT * FROM job_artifacts WHERE job_id = ? AND kind = ? AND sequence = ?',
                             (job_id, kind, sequence)).fetchone()
        if row is None:
            return {'status': 'missing', 'content': None}
        if row['expired_at'] is not None:
            return {'status': 'expired', 'content': None, 'expired_at': row['expired_at']}
        if row['raw_size'] > limit:
            return {'status': 'too_large', 'content': None}
        content = row['content']
        if row['codec'] == 'zlib':
            inflater = zlib.decompressobj()
            try:
                content = inflater.decompress(content, row['raw_size'] + 1)
            except zlib.error:
                return {'status': 'corrupt', 'content': None}
        if len(content) != row['raw_size'] or hashlib.sha256(content).hexdigest() != row['digest']:
            return {'status': 'corrupt', 'content': None}
        return {'status': 'available', 'content': content}

    def prune_raw_logs(self, *, now=None, days=codes.RAW_LOG_RETENTION_DAYS, batch=500):
        """Expire raw logs older than `days` that no open work still needs. Not scheduled yet.

        Kept: durable kinds, jobs not yet cleaned up, logs of a current candidate of an
        open unit, and logs a pending recovery or cleanup may still read.
        """
        now = self.clock() if now is None else now
        cutoff = now - days * 86400
        raw = tuple(kind for kind, retention in codes.ARTIFACT_KINDS.items() if retention == 'raw')
        terminal = tuple(sorted(codes.TERMINAL_STATES))
        marks = lambda values: ', '.join('?' for _ in values)
        query = (
            f'SELECT a.rowid FROM job_artifacts a JOIN jobs j ON j.id = a.job_id JOIN units u ON u.id = j.unit_id'
            f' WHERE a.expired_at IS NULL AND a.kind IN ({marks(raw)}) AND a.created_at < ? AND j.cleanup = 1'
            f' AND (u.state IN ({marks(terminal)}) OR j.candidate_id IS NULL OR j.candidate_id IS NOT u.current_candidate_id)'
            f" AND NOT EXISTS (SELECT 1 FROM unit_gates g WHERE g.unit_id = u.id AND g.resolved_at IS NULL"
            f"  AND g.code IN ('recovery_pending', 'cleanup_pending') AND (g.member_id IS NULL OR g.member_id IS j.member_id))"
            f' LIMIT ?')
        total = 0
        while True:
            with self.write() as db:
                rows = [row[0] for row in db.execute(query, (*raw, cutoff, *terminal, batch)).fetchall()]
                if rows:
                    db.execute(f'UPDATE job_artifacts SET content = NULL, expired_at = ? WHERE rowid IN ({marks(rows)})',
                               (now, *rows))
            total += len(rows)
            if len(rows) < batch:
                return total

    # --- Snapshot reads ------------------------------------------------------

    def unit(self, unit_id):
        """One unit with its live members, current blocker and plain wording, from one snapshot."""
        with self.read() as db:
            unit = dict(self._unit(unit_id))
            members = [dict(row) for row in self._live_members(unit_id)]
            blocker = None if unit['state'] in codes.TERMINAL_STATES else self._blocker(unit_id)
            candidate = db.execute('SELECT pull_number FROM candidates WHERE id = ?', (unit['current_candidate_id'],)).fetchone()
        params = {'pull': candidate['pull_number']} if candidate else {}
        plain = status.describe(unit['close_code'], **params) if unit['close_code'] else status.describe(unit['state'], **params)
        return {**unit, 'members': members, 'blocker': blocker, 'plain': plain}

    def events_after(self, sequence=0, limit=100):
        with self.read() as db:
            rows = db.execute('SELECT * FROM events WHERE sequence > ? ORDER BY sequence LIMIT ?',
                              (sequence, max(1, min(int(limit), 1000)))).fetchall()
        return [{**dict(row), 'payload': json.loads(row['payload_json'])} for row in rows]


class _Write:
    """`BEGIN IMMEDIATE` ... `COMMIT`; nested writes join the outer transaction."""

    def __init__(self, store):
        self.store = store

    def __enter__(self):
        store = self.store
        if store._depth:
            store._depth += 1
            return store.db
        try:
            store.db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError as error:
            if 'locked' in str(error) or 'busy' in str(error):
                raise StoreBusy('The Forge store is busy; retry the request') from error
            raise
        store._depth, store._tx = 1, uuid.uuid4().hex
        return store.db

    def __exit__(self, kind, error, trace):
        store = self.store
        store._depth -= 1
        if store._depth:
            return False
        store._tx = None
        if kind is None:
            try:
                store.db.execute('COMMIT')
                return False
            except sqlite3.Error as failure:
                error = failure  # e.g. a deferred foreign key: roll back and report it
        if store.db.in_transaction:
            store.db.execute('ROLLBACK')
        if isinstance(error, sqlite3.IntegrityError):
            raise InvariantViolation(str(error)) from error
        if isinstance(error, sqlite3.OperationalError) and ('locked' in str(error) or 'busy' in str(error)):
            raise StoreBusy('The Forge store is busy; retry the request') from error
        if kind is None:
            raise error
        return False


class _Read:
    """A read-only snapshot: every query in the block sees the same committed state."""

    def __init__(self, store):
        self.store = store
        self.nested = False

    def __enter__(self):
        store = self.store
        if store._depth:
            self.nested = True  # inside a write: see the transaction's own changes
            return store.db
        store.db.execute('PRAGMA query_only = ON')
        try:
            store.db.execute('BEGIN')
            store.db.execute('SELECT count(*) FROM store_metadata').fetchone()  # pin the snapshot now
        except BaseException:
            store.db.execute('PRAGMA query_only = OFF')
            raise
        return store.db

    def __exit__(self, *exc):
        if self.nested:
            return False
        store = self.store
        try:
            if store.db.in_transaction:
                store.db.execute('ROLLBACK')
        finally:
            store.db.execute('PRAGMA query_only = OFF')
        return False
