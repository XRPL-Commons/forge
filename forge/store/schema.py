"""Ordered schema migrations. Applied migrations are immutable: add a new one.

The constraints, partial unique indexes and triggers here replace the
whole-ledger checks in `Ledger.validate()`:

- one job per slot: `job_leases.slot` is the key and each job holds one lease;
- one live job per part: `one_live_job_per_part`, `one_live_unit_job`;
- one open unit per issue: `one_live_membership_per_issue` plus the triggers
  that keep live members out of terminal units;
- one unresolved gate of each code per subject: `one_open_gate_per_subject`.

The reference tables repeat `codes.py`; tests check that the two agree.
"""
from __future__ import annotations

import hashlib
import sqlite3

INITIAL = r"""
CREATE TABLE store_metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL
) STRICT;
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL,
    checksum TEXT NOT NULL,
    applied_at REAL NOT NULL
) STRICT;

-- The closed code registry (forge/store/codes.py).
CREATE TABLE unit_states (
    code TEXT PRIMARY KEY NOT NULL,
    terminal INTEGER NOT NULL CHECK (terminal IN (0, 1))
) STRICT;
INSERT INTO unit_states VALUES
    ('queued', 0), ('building', 0), ('integrating', 0), ('reviewing', 0), ('auditing', 0),
    ('merging', 0), ('merged', 1), ('closed', 1), ('dissolved', 1);
CREATE TABLE unit_transitions (
    from_state TEXT NOT NULL REFERENCES unit_states(code),
    to_state TEXT NOT NULL REFERENCES unit_states(code),
    PRIMARY KEY (from_state, to_state)
) STRICT, WITHOUT ROWID;
INSERT INTO unit_transitions VALUES
    ('queued', 'building'), ('queued', 'closed'),
    ('building', 'integrating'), ('building', 'reviewing'), ('building', 'closed'), ('building', 'dissolved'),
    ('integrating', 'reviewing'), ('integrating', 'building'), ('integrating', 'closed'), ('integrating', 'dissolved'),
    ('reviewing', 'auditing'), ('reviewing', 'building'), ('reviewing', 'integrating'), ('reviewing', 'merged'),
    ('reviewing', 'closed'), ('reviewing', 'dissolved'),
    ('auditing', 'merging'), ('auditing', 'building'), ('auditing', 'integrating'), ('auditing', 'merged'),
    ('auditing', 'closed'), ('auditing', 'dissolved'),
    ('merging', 'merged'), ('merging', 'building'), ('merging', 'integrating'), ('merging', 'closed'),
    ('merging', 'dissolved');
CREATE TABLE close_codes (
    code TEXT PRIMARY KEY NOT NULL,
    issue_outcome TEXT CHECK (issue_outcome IN ('done', 'dropped'))
) STRICT;
INSERT INTO close_codes VALUES
    ('github_completed', 'done'), ('github_not_planned', 'dropped'), ('github_duplicate', 'dropped'),
    ('operator_closed', NULL), ('combined', NULL);
CREATE TABLE gate_codes (
    code TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('intent', 'decision', 'readiness', 'dependency', 'automatic', 'candidate')),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 2 AND 7),
    release TEXT NOT NULL CHECK (release IN ('operator', 'observation', 'recheck', 'time', 'evidence', 'transition')),
    member_scope INTEGER NOT NULL CHECK (member_scope IN (0, 1))
) STRICT;
INSERT INTO gate_codes VALUES
    ('operator_pause', 'intent', 2, 'operator', 0),
    ('operator_backlog', 'intent', 2, 'operator', 0),
    ('label_hold', 'intent', 2, 'observation', 1),
    ('membership_decision', 'intent', 2, 'operator', 1),
    ('operator_sync', 'intent', 2, 'observation', 0),
    ('external_change', 'intent', 2, 'operator', 1),
    ('ownership_conflict', 'intent', 2, 'observation', 1),
    ('budget_exhausted', 'decision', 3, 'operator', 0),
    ('approach_required', 'decision', 3, 'operator', 1),
    ('decision_required', 'decision', 3, 'operator', 1),
    ('repository_setup', 'readiness', 4, 'observation', 0),
    ('environment', 'readiness', 4, 'recheck', 1),
    ('observation_unavailable', 'readiness', 4, 'observation', 1),
    ('legacy_reconciliation', 'readiness', 4, 'operator', 1),
    ('dependency', 'dependency', 5, 'observation', 1),
    ('recovery_pending', 'automatic', 6, 'transition', 1),
    ('cleanup_pending', 'automatic', 6, 'transition', 1),
    ('provider_unavailable', 'automatic', 6, 'time', 0),
    ('capacity', 'automatic', 6, 'transition', 0),
    ('retry_delay', 'automatic', 6, 'time', 1),
    ('verification_human', 'candidate', 7, 'evidence', 0),
    ('verification_ci', 'candidate', 7, 'evidence', 0),
    ('approval_required', 'candidate', 7, 'evidence', 0),
    ('changes_requested', 'candidate', 7, 'transition', 0),
    ('merge_checks', 'candidate', 7, 'observation', 0),
    ('merge_conflict', 'candidate', 7, 'transition', 0);
CREATE TRIGGER unit_states_fixed BEFORE UPDATE ON unit_states BEGIN SELECT RAISE(ABORT, 'the code registry changes only by migration'); END;
CREATE TRIGGER unit_states_kept BEFORE DELETE ON unit_states BEGIN SELECT RAISE(ABORT, 'the code registry changes only by migration'); END;
CREATE TRIGGER unit_transitions_fixed BEFORE UPDATE ON unit_transitions BEGIN SELECT RAISE(ABORT, 'the code registry changes only by migration'); END;
CREATE TRIGGER unit_transitions_kept BEFORE DELETE ON unit_transitions BEGIN SELECT RAISE(ABORT, 'the code registry changes only by migration'); END;
CREATE TRIGGER close_codes_fixed BEFORE UPDATE ON close_codes BEGIN SELECT RAISE(ABORT, 'the code registry changes only by migration'); END;
CREATE TRIGGER close_codes_kept BEFORE DELETE ON close_codes BEGIN SELECT RAISE(ABORT, 'the code registry changes only by migration'); END;
CREATE TRIGGER gate_codes_fixed BEFORE UPDATE ON gate_codes BEGIN SELECT RAISE(ABORT, 'the code registry changes only by migration'); END;
CREATE TRIGGER gate_codes_kept BEFORE DELETE ON gate_codes BEGIN SELECT RAISE(ABORT, 'the code registry changes only by migration'); END;

-- Repositories and GitHub facts. The issue row is also the inventory cache.
CREATE TABLE repositories (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    full_name TEXT NOT NULL UNIQUE CHECK (length(full_name) BETWEEN 3 AND 200)
) STRICT;
CREATE TABLE repository_observations (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    kind TEXT NOT NULL CHECK (kind IN ('access', 'protection', 'inventory', 'deployment', 'refresh', 'setup')),
    generation INTEGER NOT NULL CHECK (generation > 0),
    remote_revision TEXT,
    fetched_at REAL NOT NULL,
    error_code TEXT,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(details_json) <= 65536),
    PRIMARY KEY (repository_id, kind)
) STRICT;
CREATE TRIGGER repository_observations_ordered BEFORE UPDATE ON repository_observations
WHEN NEW.generation < OLD.generation
BEGIN SELECT RAISE(ABORT, 'an older observation cannot replace a newer one'); END;
CREATE TABLE issues (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    github_number INTEGER NOT NULL CHECK (github_number > 0),
    github_state TEXT NOT NULL CHECK (github_state IN ('open', 'closed')),
    state_reason TEXT CHECK (state_reason IN ('completed', 'not_planned', 'duplicate', 'reopened')),
    labels_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(labels_json)),
    assignees_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(assignees_json)),
    remote_updated_at TEXT NOT NULL,
    observation_generation INTEGER NOT NULL CHECK (observation_generation > 0),
    current_revision_id TEXT NOT NULL,
    observed_at REAL NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    UNIQUE (repository_id, github_number),
    UNIQUE (id, repository_id),
    FOREIGN KEY (id, current_revision_id) REFERENCES issue_revisions(issue_id, id)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TRIGGER issues_identity_fixed BEFORE UPDATE ON issues
WHEN NEW.repository_id <> OLD.repository_id OR NEW.github_number <> OLD.github_number
BEGIN SELECT RAISE(ABORT, 'issue identity is fixed'); END;
CREATE TRIGGER issues_observed_in_order BEFORE UPDATE ON issues
WHEN NEW.remote_updated_at < OLD.remote_updated_at
  OR (NEW.remote_updated_at = OLD.remote_updated_at AND NEW.observation_generation < OLD.observation_generation)
BEGIN SELECT RAISE(ABORT, 'an older observation cannot replace a newer one'); END;
CREATE TABLE issue_revisions (
    id TEXT PRIMARY KEY NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issues(id) DEFERRABLE INITIALLY DEFERRED,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    requirements_hash TEXT NOT NULL,
    requirements_json TEXT NOT NULL CHECK (json_valid(requirements_json)),
    observed_at REAL NOT NULL,
    UNIQUE (issue_id, id),
    UNIQUE (issue_id, content_hash, requirements_hash)
) STRICT;
CREATE TRIGGER issue_revisions_no_update BEFORE UPDATE ON issue_revisions
BEGIN SELECT RAISE(ABORT, 'issue revisions are immutable'); END;
CREATE TRIGGER issue_revisions_no_delete BEFORE DELETE ON issue_revisions
BEGIN SELECT RAISE(ABORT, 'issue revisions are retained'); END;

-- Units of work and their members.
CREATE TABLE units (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    base_branch TEXT NOT NULL CHECK (length(base_branch) > 0),
    state TEXT NOT NULL REFERENCES unit_states(code),
    close_code TEXT REFERENCES close_codes(code),
    merge_sha TEXT,
    membership_generation INTEGER NOT NULL DEFAULT 1 CHECK (membership_generation > 0),
    fencing_generation INTEGER NOT NULL DEFAULT 1 CHECK (fencing_generation > 0),
    budget_generation INTEGER NOT NULL DEFAULT 1 CHECK (budget_generation > 0),
    current_candidate_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK ((state = 'closed') = (close_code IS NOT NULL)),
    CHECK ((state = 'merged') = (merge_sha IS NOT NULL)),
    UNIQUE (id, repository_id, base_branch),
    FOREIGN KEY (id, current_candidate_id) REFERENCES candidates(unit_id, id)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE INDEX units_by_state ON units(state, updated_at);
CREATE TRIGGER unit_transition_legal BEFORE UPDATE OF state ON units
WHEN NEW.state <> OLD.state AND NOT EXISTS (
    SELECT 1 FROM unit_transitions WHERE from_state = OLD.state AND to_state = NEW.state)
BEGIN SELECT RAISE(ABORT, 'illegal unit transition'); END;
CREATE TRIGGER unit_version_fence BEFORE UPDATE ON units
WHEN NEW.version <> OLD.version + 1
BEGIN SELECT RAISE(ABORT, 'a unit write must advance its version by one'); END;
CREATE TRIGGER unit_identity_fixed BEFORE UPDATE ON units
WHEN NEW.repository_id <> OLD.repository_id OR NEW.base_branch <> OLD.base_branch
BEGIN SELECT RAISE(ABORT, 'unit repository and base are fixed'); END;
CREATE TRIGGER unit_terminal_has_no_live_members BEFORE UPDATE OF state ON units
WHEN NEW.state IN (SELECT code FROM unit_states WHERE terminal = 1)
 AND EXISTS (SELECT 1 FROM unit_issues WHERE unit_id = NEW.id AND left_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'release live members before ending a unit'); END;
CREATE TRIGGER unit_terminal_has_no_live_jobs BEFORE UPDATE OF state ON units
WHEN NEW.state IN (SELECT code FROM unit_states WHERE terminal = 1)
 AND EXISTS (SELECT 1 FROM jobs WHERE unit_id = NEW.id AND fenced = 0
             AND phase IN ('reserved', 'starting', 'running', 'collecting'))
BEGIN SELECT RAISE(ABORT, 'fence live jobs before ending a unit'); END;
CREATE TRIGGER unit_terminal_resolves_gates BEFORE UPDATE OF state ON units
WHEN NEW.state IN (SELECT code FROM unit_states WHERE terminal = 1)
 AND EXISTS (SELECT 1 FROM unit_gates WHERE unit_id = NEW.id AND resolved_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'resolve open gates before ending a unit'); END;

CREATE TABLE unit_issues (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    repository_id INTEGER NOT NULL,
    base_branch TEXT NOT NULL,
    requirements_revision_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('pending', 'building', 'built', 'failed', 'removed', 'done')),
    joined_at REAL NOT NULL,
    left_at REAL,
    exit_reason TEXT CHECK (exit_reason IN ('dropped', 'dissolved', 'merged', 'closed', 'combined')),
    CHECK ((left_at IS NULL AND exit_reason IS NULL) OR (left_at IS NOT NULL AND exit_reason IS NOT NULL)),
    FOREIGN KEY (unit_id, repository_id, base_branch) REFERENCES units(id, repository_id, base_branch),
    FOREIGN KEY (issue_id, repository_id) REFERENCES issues(id, repository_id),
    FOREIGN KEY (issue_id, requirements_revision_id) REFERENCES issue_revisions(issue_id, id),
    UNIQUE (unit_id, id)
) STRICT;
CREATE UNIQUE INDEX one_live_membership_per_issue ON unit_issues(issue_id) WHERE left_at IS NULL;
CREATE INDEX unit_members ON unit_issues(unit_id, left_at);
CREATE TRIGGER unit_issues_join_open_unit BEFORE INSERT ON unit_issues
WHEN NEW.left_at IS NULL AND (SELECT terminal FROM unit_states JOIN units ON units.state = unit_states.code
                             WHERE units.id = NEW.unit_id) = 1
BEGIN SELECT RAISE(ABORT, 'a closed unit cannot gain members'); END;
CREATE TRIGGER unit_issues_history_fixed BEFORE UPDATE ON unit_issues
WHEN OLD.left_at IS NOT NULL OR NEW.unit_id <> OLD.unit_id OR NEW.issue_id <> OLD.issue_id
  OR NEW.joined_at <> OLD.joined_at
BEGIN SELECT RAISE(ABORT, 'membership history is immutable'); END;
CREATE TRIGGER unit_issues_kept BEFORE DELETE ON unit_issues
BEGIN SELECT RAISE(ABORT, 'membership history is retained'); END;

CREATE TABLE candidates (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    pull_number INTEGER NOT NULL CHECK (pull_number > 0),
    branch TEXT NOT NULL,
    head_oid TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    policy_hash TEXT NOT NULL,
    requirements_hash TEXT NOT NULL,
    membership_generation INTEGER NOT NULL CHECK (membership_generation > 0),
    input_manifest_hash TEXT NOT NULL,
    accepted_at REAL NOT NULL,
    UNIQUE (unit_id, id),
    UNIQUE (unit_id, pull_number, head_oid, base_oid, policy_hash, requirements_hash,
            membership_generation, input_manifest_hash)
) STRICT;
CREATE TRIGGER candidates_no_update BEFORE UPDATE ON candidates
BEGIN SELECT RAISE(ABORT, 'candidates are immutable'); END;
CREATE TRIGGER candidates_no_delete BEFORE DELETE ON candidates
BEGIN SELECT RAISE(ABORT, 'candidate history is retained'); END;

-- Coded gates (waits). A unit has at most one unresolved gate of a code per subject.
CREATE TABLE unit_gates (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    candidate_id TEXT,
    member_id TEXT,
    subject_key TEXT NOT NULL,
    code TEXT NOT NULL REFERENCES gate_codes(code),
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(details_json) <= 16384),
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolution TEXT CHECK (resolution IN ('operator', 'observation', 'recheck', 'time', 'evidence', 'transition', 'superseded')),
    CHECK ((member_id IS NULL AND subject_key = 'unit') OR
           (member_id IS NOT NULL AND subject_key = 'member:' || member_id)),
    CHECK ((resolved_at IS NULL) = (resolution IS NULL)),
    UNIQUE (unit_id, id),
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id)
) STRICT;
CREATE UNIQUE INDEX one_open_gate_per_subject ON unit_gates(unit_id, code, subject_key) WHERE resolved_at IS NULL;
CREATE TRIGGER unit_gates_scope BEFORE INSERT ON unit_gates
WHEN (NEW.member_id IS NOT NULL AND (SELECT member_scope FROM gate_codes WHERE code = NEW.code) = 0)
  OR ((SELECT kind FROM gate_codes WHERE code = NEW.code) = 'candidate' AND NEW.candidate_id IS NULL)
BEGIN SELECT RAISE(ABORT, 'gate scope does not fit its code'); END;
CREATE TRIGGER unit_gates_open_unit BEFORE INSERT ON unit_gates
WHEN (SELECT terminal FROM unit_states JOIN units ON units.state = unit_states.code
      WHERE units.id = NEW.unit_id) = 1
BEGIN SELECT RAISE(ABORT, 'a closed unit cannot gain gates'); END;
CREATE TRIGGER unit_gates_resolve_only BEFORE UPDATE ON unit_gates
WHEN OLD.resolved_at IS NOT NULL OR NEW.id <> OLD.id OR NEW.unit_id <> OLD.unit_id
  OR NEW.code <> OLD.code OR NEW.subject_key <> OLD.subject_key
  OR NEW.candidate_id IS NOT OLD.candidate_id OR NEW.member_id IS NOT OLD.member_id
  OR NEW.details_json <> OLD.details_json OR NEW.created_at <> OLD.created_at
BEGIN SELECT RAISE(ABORT, 'a gate can only be resolved, once'); END;
CREATE TRIGGER unit_gates_kept BEFORE DELETE ON unit_gates
BEGIN SELECT RAISE(ABORT, 'gate history is retained'); END;

-- Attempts, the shared failure budget, jobs and slot leases.
CREATE TABLE attempts (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    member_id TEXT,
    membership_generation INTEGER NOT NULL CHECK (membership_generation > 0),
    assignment_generation INTEGER NOT NULL CHECK (assignment_generation > 0),
    input_manifest_json TEXT NOT NULL CHECK (json_valid(input_manifest_json)),
    budget_generation INTEGER NOT NULL CHECK (budget_generation > 0),
    created_at REAL NOT NULL,
    UNIQUE (unit_id, id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id)
) STRICT;
CREATE TABLE budget_entries (
    id TEXT PRIMARY KEY NOT NULL,
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    kind TEXT NOT NULL CHECK (kind IN ('charge', 'correction')),
    corrects_id TEXT UNIQUE REFERENCES budget_entries(id),
    reason_code TEXT NOT NULL,
    created_at REAL NOT NULL,
    CHECK ((kind = 'charge' AND corrects_id IS NULL) OR (kind = 'correction' AND corrects_id IS NOT NULL)),
    UNIQUE (attempt_id, kind)
) STRICT;
CREATE TRIGGER budget_no_update BEFORE UPDATE ON budget_entries
BEGIN SELECT RAISE(ABORT, 'budget entries are append-only'); END;
CREATE TRIGGER budget_no_delete BEFORE DELETE ON budget_entries
BEGIN SELECT RAISE(ABORT, 'budget entries are append-only'); END;
CREATE TRIGGER correction_valid BEFORE INSERT ON budget_entries
WHEN NEW.kind = 'correction' AND NOT EXISTS (
    SELECT 1 FROM budget_entries WHERE id = NEW.corrects_id AND kind = 'charge' AND attempt_id = NEW.attempt_id)
BEGIN SELECT RAISE(ABORT, 'correction must reference this attempt charge'); END;

CREATE TABLE jobs (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    attempt_id TEXT NOT NULL,
    member_id TEXT,
    candidate_id TEXT,
    role TEXT NOT NULL CHECK (role IN ('build', 'integration', 'review', 'audit', 'recovery', 'merge')),
    phase TEXT NOT NULL CHECK (phase IN ('reserved', 'starting', 'running', 'collecting', 'finished', 'cleaned')),
    outcome TEXT,
    fenced INTEGER NOT NULL DEFAULT 0 CHECK (fenced IN (0, 1)),
    cleanup INTEGER NOT NULL DEFAULT 0 CHECK (cleanup IN (0, 1)),
    created_at REAL NOT NULL,
    finished_at REAL,
    CHECK (cleanup = 0 OR (fenced = 1 AND phase = 'cleaned')),
    CHECK ((role = 'build') <= (member_id IS NOT NULL)),
    FOREIGN KEY (unit_id, attempt_id) REFERENCES attempts(unit_id, id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id),
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id)
) STRICT;
CREATE INDEX unit_jobs ON jobs(unit_id, cleanup, created_at);
CREATE UNIQUE INDEX one_live_job_per_part ON jobs(member_id)
    WHERE member_id IS NOT NULL AND fenced = 0 AND phase IN ('reserved', 'starting', 'running', 'collecting');
CREATE UNIQUE INDEX one_live_unit_job ON jobs(unit_id)
    WHERE member_id IS NULL AND fenced = 0 AND phase IN ('reserved', 'starting', 'running', 'collecting');
CREATE TRIGGER jobs_for_open_units BEFORE INSERT ON jobs
WHEN (SELECT terminal FROM unit_states JOIN units ON units.state = unit_states.code
      WHERE units.id = NEW.unit_id) = 1
BEGIN SELECT RAISE(ABORT, 'a closed unit cannot start jobs'); END;
CREATE TRIGGER jobs_forward_only BEFORE UPDATE ON jobs
WHEN NEW.unit_id <> OLD.unit_id OR NEW.attempt_id <> OLD.attempt_id OR NEW.role <> OLD.role
  OR NEW.member_id IS NOT OLD.member_id OR NEW.candidate_id IS NOT OLD.candidate_id
  OR NEW.fenced < OLD.fenced OR NEW.cleanup < OLD.cleanup
  OR (OLD.phase IN ('finished', 'cleaned') AND NEW.phase IN ('reserved', 'starting', 'running', 'collecting'))
  OR (OLD.phase = 'cleaned' AND NEW.phase <> 'cleaned')
BEGIN SELECT RAISE(ABORT, 'jobs only move forward'); END;
CREATE TRIGGER jobs_kept BEFORE DELETE ON jobs
BEGIN SELECT RAISE(ABORT, 'job history is retained'); END;
CREATE TABLE job_leases (
    slot TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    execution_fence TEXT NOT NULL UNIQUE,
    acquired_at REAL NOT NULL
) STRICT;
CREATE TRIGGER release_verified BEFORE DELETE ON job_leases
WHEN NOT EXISTS (SELECT 1 FROM jobs WHERE id = OLD.job_id AND fenced = 1 AND cleanup = 1)
BEGIN SELECT RAISE(ABORT, 'cleanup must be verified before release'); END;
CREATE TRIGGER lease_no_reassign BEFORE UPDATE ON job_leases
BEGIN SELECT RAISE(ABORT, 'leases cannot be reassigned'); END;
CREATE TRIGGER lease_requires_live_job BEFORE INSERT ON job_leases
WHEN NOT EXISTS (SELECT 1 FROM jobs WHERE id = NEW.job_id AND cleanup = 0 AND fenced = 0)
BEGIN SELECT RAISE(ABORT, 'lease requires an unfenced job awaiting cleanup'); END;

-- Job output, kept apart from job rows. Raw logs may expire; nothing else changes.
CREATE TABLE job_artifacts (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK (kind IN ('output', 'live_tail', 'ci_transcript', 'launch_input',
                                       'input_manifest', 'result', 'summary')),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    media_type TEXT NOT NULL,
    codec TEXT NOT NULL CHECK (codec IN ('zlib', 'identity')),
    raw_size INTEGER NOT NULL CHECK (raw_size >= 0),
    digest TEXT NOT NULL,
    content BLOB,
    size INTEGER NOT NULL CHECK (size >= 0),
    created_at REAL NOT NULL,
    expired_at REAL,
    CHECK ((content IS NULL) = (expired_at IS NOT NULL)),
    CHECK (content IS NULL OR size = length(content)),
    PRIMARY KEY (job_id, kind, sequence)
) STRICT;
CREATE TRIGGER artifacts_expire_only BEFORE UPDATE ON job_artifacts
WHEN NOT (OLD.expired_at IS NULL AND NEW.expired_at IS NOT NULL AND NEW.content IS NULL
          AND OLD.kind IN ('output', 'live_tail', 'ci_transcript', 'launch_input')
          AND NEW.job_id = OLD.job_id AND NEW.kind = OLD.kind AND NEW.sequence = OLD.sequence
          AND NEW.media_type = OLD.media_type AND NEW.codec = OLD.codec AND NEW.raw_size = OLD.raw_size
          AND NEW.digest = OLD.digest AND NEW.size = OLD.size AND NEW.created_at = OLD.created_at)
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable; only raw logs may expire'); END;
CREATE TRIGGER artifacts_kept BEFORE DELETE ON job_artifacts
BEGIN SELECT RAISE(ABORT, 'artifact records are retained'); END;

-- Append-only events. The sequence is the change-feed cursor.
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL,
    unit_id TEXT REFERENCES units(id),
    issue_id TEXT REFERENCES issues(id),
    job_id TEXT REFERENCES jobs(id),
    candidate_id TEXT REFERENCES candidates(id),
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    actor TEXT,
    request_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(payload_json) <= 16384)
) STRICT;
CREATE INDEX unit_events ON events(unit_id, sequence);
CREATE INDEX issue_events ON events(issue_id, sequence);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events are append-only'); END;

-- Operator decisions, idempotent by request id.
CREATE TABLE decisions (
    id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL UNIQUE CHECK (length(request_id) BETWEEN 1 AND 256),
    unit_id TEXT REFERENCES units(id),
    actor TEXT NOT NULL,
    command TEXT NOT NULL,
    params_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(params_json)),
    expected_version INTEGER CHECK (expected_version > 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
    result_json TEXT CHECK (result_json IS NULL OR json_valid(result_json)),
    error_code TEXT,
    created_at REAL NOT NULL,
    CHECK ((status = 'rejected') = (error_code IS NOT NULL))
) STRICT;
CREATE TRIGGER decisions_settled_once BEFORE UPDATE ON decisions
WHEN OLD.status <> 'pending' OR NEW.request_id <> OLD.request_id OR NEW.command <> OLD.command
BEGIN SELECT RAISE(ABORT, 'a settled decision cannot change'); END;
CREATE TRIGGER decisions_kept BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are retained'); END;

-- Discussions and their ordered messages.
CREATE TABLE discussions (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    issue_id TEXT REFERENCES issues(id),
    unit_id TEXT REFERENCES units(id),
    origin TEXT,
    status TEXT NOT NULL CHECK (status IN ('idle', 'queued', 'running', 'cancelling', 'cancelled', 'error')),
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
) STRICT;
CREATE INDEX discussions_by_issue ON discussions(issue_id);
CREATE TABLE messages (
    discussion_id TEXT NOT NULL REFERENCES discussions(id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    request_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    body TEXT NOT NULL CHECK (length(body) <= 200000),
    at REAL NOT NULL,
    PRIMARY KEY (discussion_id, sequence),
    UNIQUE (discussion_id, request_id)
) STRICT;
CREATE TRIGGER messages_in_order BEFORE INSERT ON messages
WHEN NEW.sequence <> 1 + coalesce((SELECT max(sequence) FROM messages WHERE discussion_id = NEW.discussion_id), 0)
BEGIN SELECT RAISE(ABORT, 'messages are appended in order'); END;
CREATE TRIGGER messages_no_update BEFORE UPDATE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;
CREATE TRIGGER messages_no_delete BEFORE DELETE ON messages
BEGIN SELECT RAISE(ABORT, 'messages are append-only'); END;

-- Operator "check now" requests: the newest request per issue.
CREATE TABLE check_requests (
    issue_id TEXT PRIMARY KEY NOT NULL REFERENCES issues(id),
    request_id TEXT NOT NULL,
    requested_at REAL NOT NULL
) STRICT;

CREATE TABLE provider_cooldowns (
    provider TEXT PRIMARY KEY NOT NULL,
    reason TEXT NOT NULL CHECK (reason IN ('blocked_auth', 'blocked_quota')),
    since REAL NOT NULL,
    until REAL NOT NULL,
    attempt_id TEXT,
    updated_at REAL NOT NULL,
    CHECK (until >= since)
) STRICT;

-- Durable intents for external effects (GitHub comments, labels, notifications).
CREATE TABLE outbox (
    id TEXT PRIMARY KEY NOT NULL,
    effect_key TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    unit_id TEXT REFERENCES units(id),
    issue_id TEXT REFERENCES issues(id),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(payload_json) <= 65536),
    status TEXT NOT NULL CHECK (status IN ('pending', 'sent', 'confirmed', 'failed', 'abandoned')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at REAL,
    detail_json TEXT CHECK (detail_json IS NULL OR json_valid(detail_json)),
    created_at REAL NOT NULL,
    completed_at REAL,
    CHECK ((status IN ('confirmed', 'abandoned')) = (completed_at IS NOT NULL))
) STRICT;
CREATE INDEX outbox_pending ON outbox(status, next_attempt_at);
CREATE TRIGGER outbox_identity_fixed BEFORE UPDATE ON outbox
WHEN NEW.effect_key <> OLD.effect_key OR NEW.kind <> OLD.kind OR NEW.payload_json <> OLD.payload_json
  OR OLD.status IN ('confirmed', 'abandoned')
BEGIN SELECT RAISE(ABORT, 'an outbox effect keeps its identity and settles once'); END;
"""

MIGRATIONS = (
    (1, 'initial', INITIAL),
)


def checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode()).hexdigest()


def statements(sql: str):
    """Split a script into complete statements, keeping trigger bodies whole."""
    pending = ''
    for piece in sql.split(';'):
        pending += piece + ';'
        if sqlite3.complete_statement(pending):
            text = pending.strip()
            if text != ';':
                yield text
            pending = ''
    if pending.strip(' \n;'):
        raise ValueError('Incomplete SQL statement in migration')

