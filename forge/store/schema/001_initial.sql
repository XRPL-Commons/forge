-- Inactive SQLite store foundation. Connection setup enables foreign_keys.
-- JSON bounds are byte limits; no field holds a serialized controller ledger.
CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    checksum TEXT NOT NULL,
    applied_at REAL NOT NULL
) STRICT;
CREATE TABLE store_metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL CHECK (length(CAST(value AS BLOB)) <= 65536)
) STRICT;

CREATE TABLE repositories (
    id INTEGER PRIMARY KEY CHECK (id > 0),
    full_name TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
) STRICT;
CREATE TABLE issues (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    github_number INTEGER NOT NULL CHECK (github_number > 0),
    github_state TEXT NOT NULL CHECK (github_state IN ('open', 'closed')),
    current_revision_id TEXT NOT NULL,
    state_reason TEXT CHECK (state_reason IN ('completed', 'not_planned', 'duplicate', 'reopened')),
    remote_updated_at TEXT,
    collection_generation INTEGER NOT NULL DEFAULT 0 CHECK (collection_generation >= 0),
    labels_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(labels_json) AND json_type(labels_json) = 'array' AND length(CAST(labels_json AS BLOB)) <= 65536),
    assignees_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(assignees_json) AND json_type(assignees_json) = 'array' AND length(CAST(assignees_json AS BLOB)) <= 65536),
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
    requirements_json TEXT NOT NULL CHECK (json_valid(requirements_json) AND length(CAST(requirements_json AS BLOB)) <= 1048576),
    observed_at REAL NOT NULL,
    UNIQUE (issue_id, id),
    UNIQUE (issue_id, content_hash, requirements_hash)
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
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND json_type(details_json) = 'object' AND length(CAST(details_json AS BLOB)) <= 65536),
    created_at REAL NOT NULL,
    resolved_at REAL,
    CHECK ((member_id IS NULL AND subject_key = 'unit') OR
           (member_id IS NOT NULL AND subject_key = 'member:' || member_id) OR
           (member_id IS NULL AND candidate_id IS NOT NULL AND subject_key = 'candidate:' || candidate_id)),
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
    input_manifest_json TEXT NOT NULL CHECK (json_valid(input_manifest_json) AND length(CAST(input_manifest_json AS BLOB)) <= 1048576),
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
    size INTEGER NOT NULL CHECK (size >= 0 AND size <= 1048576 AND size = length(content)),
    created_at REAL NOT NULL DEFAULT 0,
    expires_at REAL CHECK (expires_at IS NULL OR expires_at >= created_at),
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
    payload_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(payload_json) AND length(CAST(payload_json AS BLOB)) <= 65536),
    result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND length(CAST(result_json AS BLOB)) <= 65536)),
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
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(CAST(payload_json AS BLOB)) <= 65536)
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

CREATE INDEX open_units ON units(state, updated_at, id);
CREATE INDEX repository_units ON units(repository_id, state, updated_at, id);
CREATE INDEX repository_issues ON issues(repository_id, github_state, github_number);
CREATE INDEX attempt_budget ON attempts(unit_id, budget_generation);
CREATE INDEX unresolved_gates ON unit_gates(unit_id, resolved_at, code);
CREATE INDEX job_attempt ON jobs(attempt_id);
CREATE TRIGGER jobs_match_attempt BEFORE INSERT ON jobs
WHEN NOT EXISTS (SELECT 1 FROM attempts WHERE id = NEW.attempt_id
    AND unit_id = NEW.unit_id AND member_id IS NEW.member_id)
BEGIN SELECT RAISE(ABORT, 'job must match its attempt member'); END;
CREATE TRIGGER jobs_identity_immutable BEFORE UPDATE OF unit_id, attempt_id, member_id, candidate_id, role ON jobs
WHEN NEW.unit_id IS NOT OLD.unit_id OR NEW.attempt_id IS NOT OLD.attempt_id
    OR NEW.member_id IS NOT OLD.member_id OR NEW.candidate_id IS NOT OLD.candidate_id
    OR NEW.role IS NOT OLD.role
BEGIN SELECT RAISE(ABORT, 'job assignment identity is immutable'); END;
CREATE TRIGGER candidates_no_delete BEFORE DELETE ON candidates
BEGIN SELECT RAISE(ABORT, 'candidates are retained'); END;
CREATE TRIGGER attempts_no_update BEFORE UPDATE ON attempts
BEGIN SELECT RAISE(ABORT, 'attempts are immutable'); END;
CREATE TRIGGER attempts_no_delete BEFORE DELETE ON attempts
BEGIN SELECT RAISE(ABORT, 'attempts are retained'); END;
CREATE TRIGGER artifacts_no_delete BEFORE DELETE ON job_artifacts
BEGIN SELECT RAISE(ABORT, 'artifact retention is not enabled'); END;

-- Observations retain ordering/freshness independently from lifecycle transitions.
CREATE TABLE repository_observations (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    kind TEXT NOT NULL CHECK (kind IN ('access', 'protection', 'inventory', 'deployment', 'discovery')),
    target_branch TEXT,
    controller_identity TEXT,
    policy_hash TEXT,
    remote_revision TEXT,
    collection_generation INTEGER NOT NULL CHECK (collection_generation > 0),
    fetched_at REAL NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    cursor TEXT,
    error_code TEXT,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
    UNIQUE (repository_id, kind, collection_generation)
) STRICT;
CREATE INDEX latest_repository_observation ON repository_observations(repository_id, kind, collection_generation DESC);
CREATE TABLE issue_dependencies (
    id TEXT PRIMARY KEY NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issues(id),
    requirements_revision_id TEXT NOT NULL,
    prerequisite_issue_id TEXT NOT NULL REFERENCES issues(id),
    source TEXT NOT NULL CHECK (source IN ('requirements', 'worker', 'operator')),
    evidence TEXT NOT NULL CHECK (length(CAST(evidence AS BLOB)) <= 65536),
    created_at REAL NOT NULL,
    CHECK (issue_id != prerequisite_issue_id),
    FOREIGN KEY (issue_id, requirements_revision_id) REFERENCES issue_revisions(issue_id, id),
    UNIQUE (requirements_revision_id, prerequisite_issue_id)
) STRICT;
CREATE INDEX prerequisite_dependents ON issue_dependencies(prerequisite_issue_id, issue_id);
CREATE TABLE member_dependencies (
    unit_id TEXT NOT NULL REFERENCES units(id),
    member_id TEXT NOT NULL,
    prerequisite_member_id TEXT NOT NULL,
    membership_generation INTEGER NOT NULL CHECK (membership_generation > 0),
    PRIMARY KEY (unit_id, member_id, prerequisite_member_id, membership_generation),
    CHECK (member_id != prerequisite_member_id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id),
    FOREIGN KEY (unit_id, prerequisite_member_id) REFERENCES unit_issues(unit_id, id)
) STRICT;
CREATE TABLE part_outputs (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    member_id TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
    membership_generation INTEGER NOT NULL CHECK (membership_generation > 0),
    commit_oid TEXT NOT NULL,
    base_oid TEXT NOT NULL,
    input_manifest_hash TEXT NOT NULL,
    accepted_at REAL NOT NULL,
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id),
    FOREIGN KEY (unit_id, attempt_id) REFERENCES attempts(unit_id, id),
    UNIQUE (attempt_id)
) STRICT;
CREATE TABLE candidate_evidence (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    candidate_id TEXT NOT NULL,
    attempt_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('review', 'audit', 'verification_human', 'verification_ci', 'publication', 'merge')),
    verdict TEXT NOT NULL CHECK (verdict IN ('pass', 'fail', 'inconclusive')),
    actor_id TEXT NOT NULL,
    observed_at REAL NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id),
    FOREIGN KEY (unit_id, attempt_id) REFERENCES attempts(unit_id, id)
) STRICT;
CREATE INDEX candidate_verdicts ON candidate_evidence(candidate_id, kind, observed_at DESC);
CREATE TABLE pull_requests (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    github_number INTEGER NOT NULL CHECK (github_number > 0),
    author_login TEXT NOT NULL,
    head_branch TEXT NOT NULL,
    base_branch TEXT NOT NULL,
    head_oid TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('open', 'closed', 'merged')),
    merge_oid TEXT,
    remote_updated_at TEXT NOT NULL,
    collection_generation INTEGER NOT NULL CHECK (collection_generation > 0),
    fetched_at REAL NOT NULL,
    UNIQUE (repository_id, github_number)
) STRICT;
CREATE TABLE candidate_observations (
    id TEXT PRIMARY KEY NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    kind TEXT NOT NULL CHECK (kind IN ('pull_request', 'checks', 'approval', 'deployment', 'merge')),
    collection_generation INTEGER NOT NULL CHECK (collection_generation > 0),
    remote_revision TEXT,
    fetched_at REAL NOT NULL,
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    error_code TEXT,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
    UNIQUE (candidate_id, kind, collection_generation)
) STRICT;
CREATE TABLE check_runs (
    id TEXT PRIMARY KEY NOT NULL,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    github_id INTEGER NOT NULL CHECK (github_id > 0),
    app_id INTEGER NOT NULL CHECK (app_id > 0),
    name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'in_progress', 'completed')),
    conclusion TEXT,
    fetched_at REAL NOT NULL,
    collection_generation INTEGER NOT NULL CHECK (collection_generation > 0),
    UNIQUE (candidate_id, github_id)
) STRICT;
CREATE TABLE external_effects (
    id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    decision_id TEXT REFERENCES decisions(id),
    unit_id TEXT REFERENCES units(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'uncertain', 'confirmed', 'failed', 'superseded')),
    expected_version INTEGER CHECK (expected_version > 0),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(CAST(payload_json AS BLOB)) <= 65536),
    receipt_json TEXT CHECK (receipt_json IS NULL OR (json_valid(receipt_json) AND length(CAST(receipt_json AS BLOB)) <= 65536)),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    retry_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
) STRICT;
CREATE INDEX pending_effects ON external_effects(status, retry_at, created_at);
CREATE TABLE merge_requests (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    candidate_id TEXT NOT NULL,
    effect_id TEXT NOT NULL UNIQUE REFERENCES external_effects(id),
    fencing_generation INTEGER NOT NULL CHECK (fencing_generation > 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'uncertain', 'merged', 'refused', 'superseded')),
    merge_oid TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id)
) STRICT;
CREATE UNIQUE INDEX one_pending_merge ON merge_requests(unit_id) WHERE status IN ('pending', 'uncertain');
CREATE TABLE check_requests (
    id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    effect_id TEXT REFERENCES external_effects(id),
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed', 'superseded')),
    created_at REAL NOT NULL
) STRICT;

-- Discussions use the same capacity namespace as pipeline work.
CREATE TABLE discussions (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER REFERENCES repositories(id),
    unit_id TEXT REFERENCES units(id),
    issue_id TEXT REFERENCES issues(id),
    title TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
) STRICT;
CREATE TABLE messages (
    id TEXT PRIMARY KEY NOT NULL,
    discussion_id TEXT NOT NULL REFERENCES discussions(id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    author_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'assistant', 'system')),
    content TEXT NOT NULL CHECK (length(CAST(content AS BLOB)) <= 1048576),
    created_at REAL NOT NULL,
    UNIQUE (discussion_id, sequence)
) STRICT;
CREATE TABLE discussion_executions (
    id TEXT PRIMARY KEY NOT NULL,
    discussion_id TEXT NOT NULL REFERENCES discussions(id),
    phase TEXT NOT NULL CHECK (phase IN ('reserved', 'starting', 'running', 'collecting', 'finished', 'cleaned')),
    fenced INTEGER NOT NULL DEFAULT 0 CHECK (fenced IN (0, 1)),
    cleanup INTEGER NOT NULL DEFAULT 0 CHECK (cleanup IN (0, 1)),
    created_at REAL NOT NULL,
    CHECK (cleanup = 0 OR (fenced = 1 AND phase = 'cleaned'))
) STRICT;
CREATE TABLE discussion_leases (
    slot TEXT PRIMARY KEY NOT NULL,
    execution_id TEXT NOT NULL UNIQUE REFERENCES discussion_executions(id),
    execution_fence TEXT NOT NULL UNIQUE,
    acquired_at REAL NOT NULL
) STRICT;
CREATE TABLE discussion_artifacts (
    execution_id TEXT NOT NULL REFERENCES discussion_executions(id),
    kind TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    media_type TEXT NOT NULL,
    digest TEXT NOT NULL,
    content BLOB NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0 AND size <= 1048576 AND size = length(content)),
    created_at REAL NOT NULL DEFAULT 0,
    expires_at REAL CHECK (expires_at IS NULL OR expires_at >= created_at),
    PRIMARY KEY (execution_id, kind, sequence)
) STRICT;
CREATE TRIGGER pipeline_capacity BEFORE INSERT ON job_leases
WHEN EXISTS (SELECT 1 FROM discussion_leases WHERE slot = NEW.slot OR execution_fence = NEW.execution_fence)
BEGIN SELECT RAISE(ABORT, 'capacity is leased by a discussion'); END;
CREATE TRIGGER discussion_capacity BEFORE INSERT ON discussion_leases
WHEN EXISTS (SELECT 1 FROM job_leases WHERE slot = NEW.slot OR execution_fence = NEW.execution_fence)
BEGIN SELECT RAISE(ABORT, 'capacity is leased by a pipeline job'); END;
CREATE TRIGGER discussion_release_verified BEFORE DELETE ON discussion_leases
WHEN NOT EXISTS (SELECT 1 FROM discussion_executions WHERE id = OLD.execution_id AND fenced = 1 AND cleanup = 1)
BEGIN SELECT RAISE(ABORT, 'cleanup must be verified before release'); END;
CREATE TRIGGER discussion_lease_no_reassign BEFORE UPDATE ON discussion_leases
BEGIN SELECT RAISE(ABORT, 'leases cannot be reassigned'); END;
CREATE TRIGGER discussion_lease_requires_live_execution BEFORE INSERT ON discussion_leases
WHEN NOT EXISTS (SELECT 1 FROM discussion_executions WHERE id = NEW.execution_id AND cleanup = 0 AND fenced = 0)
BEGIN SELECT RAISE(ABORT, 'lease requires an unfenced execution awaiting cleanup'); END;

CREATE TABLE provider_cooldowns (
    account_ref TEXT PRIMARY KEY NOT NULL,
    provider TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    retry_at REAL NOT NULL,
    observed_at REAL NOT NULL
) STRICT;
CREATE TABLE provider_observations (
    id TEXT PRIMARY KEY NOT NULL,
    provider TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    readiness TEXT NOT NULL CHECK (readiness IN ('ready', 'unavailable', 'unknown')),
    reason_code TEXT,
    observed_at REAL NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536)
) STRICT;
CREATE INDEX provider_readiness ON provider_observations(account_ref, observed_at DESC);
CREATE TABLE memory_profiles (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    role TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    memory_bytes INTEGER NOT NULL CHECK (memory_bytes > 0),
    source_job_id TEXT REFERENCES jobs(id),
    observed_at REAL NOT NULL,
    UNIQUE (repository_id, role, revision)
) STRICT;
CREATE TABLE unit_environment (
    unit_id TEXT NOT NULL REFERENCES units(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    choice TEXT NOT NULL,
    override TEXT,
    retry_tier INTEGER NOT NULL DEFAULT 0 CHECK (retry_tier >= 0),
    memory_profile_id TEXT REFERENCES memory_profiles(id),
    created_at REAL NOT NULL,
    PRIMARY KEY (unit_id, revision)
) STRICT;
CREATE TABLE deployment_observations (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    candidate_id TEXT REFERENCES candidates(id),
    environment TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'success', 'failure', 'unknown')),
    commit_oid TEXT NOT NULL,
    observed_at REAL NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536)
) STRICT;
CREATE TABLE environment_scans (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    source_revision TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'completed', 'failed')),
    result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND length(CAST(result_json AS BLOB)) <= 65536)),
    created_at REAL NOT NULL
) STRICT;
CREATE TABLE environment_proposals (
    id TEXT PRIMARY KEY NOT NULL,
    scan_id TEXT NOT NULL REFERENCES environment_scans(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'applied', 'superseded')),
    proposal_json TEXT NOT NULL CHECK (json_valid(proposal_json) AND length(CAST(proposal_json AS BLOB)) <= 65536),
    decision_id TEXT REFERENCES decisions(id),
    effect_id TEXT REFERENCES external_effects(id),
    created_at REAL NOT NULL,
    UNIQUE (scan_id, revision)
) STRICT;

CREATE TABLE configuration (
    domain TEXT NOT NULL,
    subject TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    active INTEGER NOT NULL CHECK (active IN (0, 1)),
    settings_json TEXT NOT NULL CHECK (json_valid(settings_json) AND length(CAST(settings_json AS BLOB)) <= 65536),
    credential_ref TEXT,
    created_at REAL NOT NULL,
    PRIMARY KEY (domain, subject, revision)
) STRICT;
CREATE UNIQUE INDEX one_active_configuration ON configuration(domain, subject) WHERE active = 1;
CREATE TABLE runtime_controls (
    name TEXT PRIMARY KEY NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
    enabled INTEGER CHECK (enabled IN (0, 1)),
    cursor TEXT,
    heartbeat_at REAL,
    updated_at REAL NOT NULL
) STRICT;
CREATE TABLE setup_operations (
    id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    stage TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'waiting', 'completed', 'failed')),
    expected_generation INTEGER NOT NULL CHECK (expected_generation > 0),
    error_code TEXT,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
) STRICT;
CREATE TABLE operator_identities (
    id TEXT PRIMARY KEY NOT NULL,
    github_id INTEGER NOT NULL UNIQUE CHECK (github_id > 0),
    login TEXT NOT NULL,
    authorization_epoch INTEGER NOT NULL DEFAULT 1 CHECK (authorization_epoch > 0),
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1))
) STRICT;
CREATE TABLE operator_sessions (
    id TEXT PRIMARY KEY NOT NULL,
    operator_id TEXT NOT NULL REFERENCES operator_identities(id),
    token_hash TEXT NOT NULL UNIQUE,
    authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch > 0),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    CHECK (expires_at > created_at)
) STRICT;
CREATE INDEX active_operator_sessions ON operator_sessions(operator_id, revoked_at, expires_at);
CREATE TABLE operator_flows (
    id TEXT PRIMARY KEY NOT NULL,
    state_hash TEXT NOT NULL UNIQUE,
    nonce_hash TEXT NOT NULL,
    return_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL,
    CHECK (expires_at > created_at)
) STRICT;
CREATE TABLE client_connections (
    id TEXT PRIMARY KEY NOT NULL,
    operator_id TEXT NOT NULL REFERENCES operator_identities(id),
    token_hash TEXT NOT NULL UNIQUE,
    client_name TEXT NOT NULL,
    authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch > 0),
    scopes_json TEXT NOT NULL CHECK (json_valid(scopes_json) AND json_type(scopes_json) = 'array' AND length(CAST(scopes_json AS BLOB)) <= 65536),
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    CHECK (expires_at > created_at)
) STRICT;
CREATE INDEX operator_connections ON client_connections(operator_id, revoked_at, expires_at);
CREATE TABLE client_operations (
    id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT NOT NULL UNIQUE,
    connection_id TEXT NOT NULL REFERENCES client_connections(id),
    repository_id INTEGER REFERENCES repositories(id),
    kind TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'running', 'completed', 'rejected', 'failed')),
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(CAST(payload_json AS BLOB)) <= 65536),
    result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND length(CAST(result_json AS BLOB)) <= 65536)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
) STRICT;
CREATE TABLE operator_audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    operator_id TEXT REFERENCES operator_identities(id),
    session_id TEXT REFERENCES operator_sessions(id),
    connection_id TEXT REFERENCES client_connections(id),
    request_id TEXT,
    action TEXT NOT NULL,
    outcome TEXT NOT NULL,
    at REAL NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536)
) STRICT;
CREATE TABLE notification_outbox (
    id TEXT PRIMARY KEY NOT NULL,
    enqueue_key TEXT NOT NULL UNIQUE,
    event_sequence INTEGER REFERENCES events(sequence),
    channel TEXT NOT NULL,
    recipient_ref TEXT NOT NULL,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(CAST(payload_json AS BLOB)) <= 65536),
    status TEXT NOT NULL CHECK (status IN ('pending', 'sending', 'delivered', 'failed')),
    retry_at REAL,
    created_at REAL NOT NULL
) STRICT;
CREATE INDEX pending_notifications ON notification_outbox(status, retry_at, created_at);
CREATE TABLE notification_deliveries (
    id TEXT PRIMARY KEY NOT NULL,
    notification_id TEXT NOT NULL REFERENCES notification_outbox(id),
    attempt_number INTEGER NOT NULL CHECK (attempt_number > 0),
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'uncertain', 'failed')),
    receipt_ref TEXT,
    error_code TEXT,
    attempted_at REAL NOT NULL,
    UNIQUE (notification_id, attempt_number)
) STRICT;
CREATE TABLE import_runs (
    id TEXT PRIMARY KEY NOT NULL,
    importer_version TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    storage_generation INTEGER NOT NULL CHECK (storage_generation > 0),
    status TEXT NOT NULL CHECK (status IN ('planned', 'captured', 'importing', 'validating', 'validated', 'activated', 'failed')),
    validation_json TEXT CHECK (validation_json IS NULL OR (json_valid(validation_json) AND length(CAST(validation_json AS BLOB)) <= 65536)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
) STRICT;
CREATE TABLE import_sources (
    run_id TEXT NOT NULL REFERENCES import_runs(id),
    source_name TEXT NOT NULL,
    domain TEXT NOT NULL,
    digest TEXT NOT NULL,
    size INTEGER NOT NULL CHECK (size >= 0),
    PRIMARY KEY (run_id, source_name)
) STRICT;
CREATE TABLE import_progress (
    run_id TEXT NOT NULL REFERENCES import_runs(id),
    table_name TEXT NOT NULL,
    cursor TEXT,
    imported_count INTEGER NOT NULL DEFAULT 0 CHECK (imported_count >= 0),
    validated_count INTEGER NOT NULL DEFAULT 0 CHECK (validated_count >= 0),
    complete INTEGER NOT NULL DEFAULT 0 CHECK (complete IN (0, 1)),
    updated_at REAL NOT NULL,
    PRIMARY KEY (run_id, table_name)
) STRICT;

CREATE TRIGGER candidate_evidence_no_update BEFORE UPDATE ON candidate_evidence
BEGIN SELECT RAISE(ABORT, 'candidate evidence is immutable'); END;
CREATE TRIGGER candidate_evidence_no_delete BEFORE DELETE ON candidate_evidence
BEGIN SELECT RAISE(ABORT, 'candidate evidence is retained'); END;
CREATE TRIGGER part_outputs_no_update BEFORE UPDATE ON part_outputs
BEGIN SELECT RAISE(ABORT, 'part outputs are immutable'); END;
CREATE TRIGGER part_outputs_no_delete BEFORE DELETE ON part_outputs
BEGIN SELECT RAISE(ABORT, 'part outputs are retained'); END;
CREATE TRIGGER outputs_match_member BEFORE INSERT ON part_outputs
WHEN NOT EXISTS (SELECT 1 FROM unit_issues m JOIN attempts a ON a.id = NEW.attempt_id
    WHERE m.id = NEW.member_id AND m.unit_id = NEW.unit_id
      AND m.requirements_revision_id = NEW.requirements_revision_id
      AND a.member_id = m.id AND a.unit_id = m.unit_id
      AND a.membership_generation = NEW.membership_generation)
BEGIN SELECT RAISE(ABORT, 'part output must match pinned member and attempt'); END;
CREATE TRIGGER operator_audit_no_update BEFORE UPDATE ON operator_audit
BEGIN SELECT RAISE(ABORT, 'operator audit is append-only'); END;
CREATE TRIGGER operator_audit_no_delete BEFORE DELETE ON operator_audit
BEGIN SELECT RAISE(ABORT, 'operator audit is append-only'); END;
CREATE TRIGGER discussion_artifacts_no_update BEFORE UPDATE ON discussion_artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
CREATE TRIGGER discussion_artifacts_no_delete BEFORE DELETE ON discussion_artifacts
BEGIN SELECT RAISE(ABORT, 'artifact retention is not enabled'); END;
CREATE TRIGGER unit_generations_monotonic BEFORE UPDATE ON units
WHEN NEW.membership_generation < OLD.membership_generation
    OR NEW.fencing_generation < OLD.fencing_generation
    OR NEW.budget_generation < OLD.budget_generation OR NEW.version < OLD.version
BEGIN SELECT RAISE(ABORT, 'unit generations cannot move backwards'); END;
CREATE TRIGGER job_cleanup_monotonic BEFORE UPDATE ON jobs
WHEN NEW.fenced < OLD.fenced OR NEW.cleanup < OLD.cleanup
BEGIN SELECT RAISE(ABORT, 'job fencing and cleanup cannot be reversed'); END;
CREATE TRIGGER discussion_cleanup_monotonic BEFORE UPDATE ON discussion_executions
WHEN NEW.fenced < OLD.fenced OR NEW.cleanup < OLD.cleanup
BEGIN SELECT RAISE(ABORT, 'execution fencing and cleanup cannot be reversed'); END;
