-- Proposed core schema for design validation, not a deployed migration.
PRAGMA foreign_keys = ON;

CREATE TABLE repositories (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    full_name TEXT NOT NULL UNIQUE
) STRICT;
CREATE TABLE issues (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    github_number INTEGER NOT NULL CHECK (github_number > 0),
    github_state TEXT NOT NULL CHECK (github_state IN ('open', 'closed')),
    current_revision_id TEXT NOT NULL,
    observed_at REAL NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    UNIQUE (repository_id, github_number),
    UNIQUE (id, repository_id),
    FOREIGN KEY (id, current_revision_id) REFERENCES issue_revisions(issue_id, id)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;
CREATE TABLE issue_revisions (
    id TEXT PRIMARY KEY NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issues(id),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    requirements_hash TEXT NOT NULL,
    requirements_json TEXT NOT NULL,
    observed_at REAL NOT NULL,
    UNIQUE (issue_id, id),
    UNIQUE (issue_id, requirements_hash)
) STRICT;
CREATE TABLE units (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    base_branch TEXT NOT NULL,
    membership_generation INTEGER NOT NULL DEFAULT 1 CHECK (membership_generation > 0),
    fencing_generation INTEGER NOT NULL DEFAULT 1 CHECK (fencing_generation > 0),
    state TEXT NOT NULL CHECK (state IN (
        'queued', 'building', 'integrating', 'reviewing', 'auditing',
        'merging', 'merged', 'closed', 'dissolved')),
    current_candidate_id TEXT,
    primary_gate_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    budget_generation INTEGER NOT NULL DEFAULT 1 CHECK (budget_generation > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (id, repository_id, base_branch),
    FOREIGN KEY (id, current_candidate_id) REFERENCES candidates(unit_id, id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (id, primary_gate_id) REFERENCES unit_gates(unit_id, id)
        DEFERRABLE INITIALLY DEFERRED
) STRICT;
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
    CHECK ((left_at IS NULL AND exit_reason IS NULL) OR
           (left_at IS NOT NULL AND exit_reason IS NOT NULL)),
    FOREIGN KEY (unit_id, repository_id, base_branch)
        REFERENCES units(id, repository_id, base_branch),
    FOREIGN KEY (issue_id, repository_id) REFERENCES issues(id, repository_id),
    FOREIGN KEY (issue_id, requirements_revision_id) REFERENCES issue_revisions(issue_id, id),
    UNIQUE (unit_id, id)
) STRICT;
CREATE UNIQUE INDEX one_active_unit_per_issue ON unit_issues(issue_id) WHERE left_at IS NULL;
CREATE INDEX unit_members ON unit_issues(unit_id, left_at);
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
    UNIQUE (unit_id, pull_number, head_oid, base_oid, policy_hash, requirements_hash, membership_generation, input_manifest_hash)
) STRICT;
CREATE TABLE unit_gates (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    candidate_id TEXT,
    member_id TEXT,
    subject_key TEXT NOT NULL,
    code TEXT NOT NULL CHECK (code IN (
        'operator_pause', 'operator_backlog', 'label_hold', 'membership_decision', 'operator_sync',
        'external_change', 'ownership_conflict', 'budget_exhausted',
        'approach_required', 'decision_required', 'repository_setup', 'environment',
        'observation_unavailable', 'dependency', 'recovery_pending', 'cleanup_pending',
        'provider_unavailable', 'capacity', 'retry_delay', 'verification_human',
        'verification_ci', 'approval_required', 'changes_requested', 'merge_checks',
        'merge_conflict', 'legacy_reconciliation')),
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    resolved_at REAL,
    CHECK ((member_id IS NULL AND subject_key = 'unit') OR
           (member_id IS NOT NULL AND subject_key = 'member:' || member_id)),
    UNIQUE (unit_id, id),
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id)
) STRICT;
CREATE UNIQUE INDEX one_current_gate_code ON unit_gates(unit_id, code, subject_key) WHERE resolved_at IS NULL;
CREATE TABLE attempts (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    member_id TEXT,
    membership_generation INTEGER NOT NULL CHECK (membership_generation > 0),
    assignment_generation INTEGER NOT NULL CHECK (assignment_generation > 0),
    input_manifest_json TEXT NOT NULL,
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
    CHECK ((kind = 'charge' AND corrects_id IS NULL) OR
           (kind = 'correction' AND corrects_id IS NOT NULL)),
    UNIQUE (attempt_id, kind)
) STRICT;
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
    CHECK (cleanup = 0 OR (fenced = 1 AND phase = 'cleaned')),
    FOREIGN KEY (unit_id, attempt_id) REFERENCES attempts(unit_id, id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id),
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id)
) STRICT;
CREATE INDEX unit_jobs ON jobs(unit_id, cleanup, created_at);
CREATE TABLE job_leases (
    slot TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    execution_fence TEXT NOT NULL UNIQUE,
    acquired_at REAL NOT NULL
) STRICT;
CREATE TABLE job_artifacts (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    media_type TEXT NOT NULL,
    digest TEXT NOT NULL,
    content BLOB NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0 AND size = length(content)),
    PRIMARY KEY (job_id, kind, sequence)
) STRICT;
CREATE TABLE decisions (
    id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    unit_id TEXT REFERENCES units(id),
    actor_id TEXT NOT NULL,
    expected_version INTEGER CHECK (expected_version > 0),
    command TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'rejected')),
    result_json TEXT,
    created_at REAL NOT NULL
) STRICT;
CREATE TABLE events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    transaction_id TEXT NOT NULL,
    unit_id TEXT REFERENCES units(id),
    issue_id TEXT REFERENCES issues(id),
    job_id TEXT REFERENCES jobs(id),
    kind TEXT NOT NULL,
    at REAL NOT NULL,
    actor_id TEXT,
    payload_json TEXT NOT NULL
) STRICT;
CREATE INDEX unit_events ON events(unit_id, sequence);
CREATE INDEX issue_events ON events(issue_id, sequence);
CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN
    SELECT RAISE(ABORT, 'events are append-only');
END;
CREATE TRIGGER budget_no_update BEFORE UPDATE ON budget_entries BEGIN
    SELECT RAISE(ABORT, 'budget entries are append-only');
END;
CREATE TRIGGER budget_no_delete BEFORE DELETE ON budget_entries BEGIN
    SELECT RAISE(ABORT, 'budget entries are append-only');
END;
CREATE TRIGGER correction_valid BEFORE INSERT ON budget_entries
WHEN NEW.kind = 'correction' AND NOT EXISTS (
    SELECT 1 FROM budget_entries
    WHERE id = NEW.corrects_id AND kind = 'charge' AND attempt_id = NEW.attempt_id
) BEGIN SELECT RAISE(ABORT, 'correction must reference this attempt charge'); END;
CREATE TRIGGER release_verified BEFORE DELETE ON job_leases
WHEN NOT EXISTS (SELECT 1 FROM jobs WHERE id = OLD.job_id AND fenced = 1 AND cleanup = 1)
BEGIN SELECT RAISE(ABORT, 'cleanup must be verified before release'); END;
CREATE TRIGGER lease_no_reassign BEFORE UPDATE ON job_leases
BEGIN SELECT RAISE(ABORT, 'leases cannot be reassigned'); END;
CREATE TRIGGER lease_requires_live_job BEFORE INSERT ON job_leases
WHEN NOT EXISTS (SELECT 1 FROM jobs WHERE id = NEW.job_id AND cleanup = 0 AND fenced = 0)
BEGIN SELECT RAISE(ABORT, 'lease requires an unfenced job awaiting cleanup'); END;
CREATE TRIGGER candidates_no_update BEFORE UPDATE ON candidates BEGIN
    SELECT RAISE(ABORT, 'candidates are immutable');
END;
CREATE TRIGGER artifacts_no_update BEFORE UPDATE ON job_artifacts BEGIN
    SELECT RAISE(ABORT, 'artifacts are immutable');
END;
CREATE TRIGGER issue_revisions_no_update BEFORE UPDATE ON issue_revisions BEGIN
    SELECT RAISE(ABORT, 'issue revisions are immutable');
END;
CREATE TRIGGER issue_revisions_no_delete BEFORE DELETE ON issue_revisions BEGIN
    SELECT RAISE(ABORT, 'issue revisions are retained');
END;
