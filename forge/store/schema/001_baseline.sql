-- Forge store schema baseline (#289).
--
-- This file replaces migrations 001-041. It is the schema those migrations
-- built, without the tables and codes nothing reads any more. A store created
-- from those 41 migrations is rebased onto this file once, by
-- forge/store/baseline_rebase.py inside `forge migrate upgrade`.
--
-- Never edit this file: its checksum is recorded in every store. Schema changes
-- are new migrations, starting at 002.

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    checksum TEXT NOT NULL,
    applied_at REAL NOT NULL
) STRICT;
CREATE TABLE store_metadata (
    key TEXT PRIMARY KEY NOT NULL,
    value TEXT NOT NULL CHECK (length(CAST(value AS BLOB)) <= 65536)
) STRICT;
CREATE TABLE unit_states (code TEXT PRIMARY KEY NOT NULL, terminal INTEGER NOT NULL CHECK (terminal IN (0,1))) STRICT;
CREATE TABLE unit_transitions (from_state TEXT NOT NULL REFERENCES unit_states(code), to_state TEXT NOT NULL REFERENCES unit_states(code), PRIMARY KEY (from_state,to_state)) STRICT;
CREATE TABLE close_codes (code TEXT PRIMARY KEY NOT NULL, issue_outcome TEXT CHECK (issue_outcome IN ('done','dropped'))) STRICT;
CREATE TABLE gate_codes (
    code TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('intent','decision','readiness','dependency','automatic','candidate')),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 2 AND 7),
    release TEXT NOT NULL CHECK (release IN ('operator','observation','recheck','time','evidence','transition')),
    unit_scope INTEGER NOT NULL CHECK (unit_scope IN (0,1)),
    member_scope INTEGER NOT NULL CHECK (member_scope IN (0,1)),
    candidate_scope INTEGER NOT NULL CHECK (candidate_scope IN (0,1))
, job_scope INTEGER NOT NULL DEFAULT 0 CHECK(job_scope IN (0,1))) STRICT;
CREATE TABLE job_roles (code TEXT PRIMARY KEY NOT NULL) STRICT;
CREATE TABLE job_phases (code TEXT PRIMARY KEY NOT NULL) STRICT;
CREATE TABLE part_states (code TEXT PRIMARY KEY NOT NULL) STRICT;
CREATE TABLE exit_reasons (code TEXT PRIMARY KEY NOT NULL) STRICT;
CREATE TABLE event_kinds (code TEXT PRIMARY KEY NOT NULL) STRICT;
CREATE TABLE artifact_kinds (code TEXT PRIMARY KEY NOT NULL, retention TEXT NOT NULL CHECK (retention IN ('raw','durable'))) STRICT;
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
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0), assignee_types_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(assignee_types_json) AND length(CAST(assignee_types_json AS BLOB)) <= 65536), reopened_version INTEGER NOT NULL DEFAULT 0 CHECK (reopened_version >= 0),
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
    state TEXT NOT NULL REFERENCES unit_states(code),
    close_code TEXT REFERENCES close_codes(code),
    merge_sha TEXT CHECK (merge_sha IS NULL OR length(merge_sha) > 0),
    current_candidate_id TEXT,
    primary_gate_id TEXT,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    budget_generation INTEGER NOT NULL DEFAULT 1 CHECK (budget_generation > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    CHECK ((state = 'closed') = (close_code IS NOT NULL)),
    CHECK ((state = 'merged') = (merge_sha IS NOT NULL)),
    CHECK (state NOT IN ('merged','closed','dissolved') OR primary_gate_id IS NULL),
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
    state TEXT NOT NULL REFERENCES part_states(code),
    joined_at REAL NOT NULL,
    left_at REAL,
    exit_reason TEXT REFERENCES exit_reasons(code), completion_issue_version INTEGER CHECK (completion_issue_version > 0),
    CHECK ((left_at IS NULL AND exit_reason IS NULL) OR
           (left_at IS NOT NULL AND exit_reason IS NOT NULL)),
    FOREIGN KEY (unit_id, repository_id, base_branch)
        REFERENCES units(id, repository_id, base_branch),
    FOREIGN KEY (issue_id, repository_id) REFERENCES issues(id, repository_id),
    FOREIGN KEY (issue_id, requirements_revision_id) REFERENCES issue_revisions(issue_id, id),
    UNIQUE (unit_id, id)
) STRICT;
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
    role TEXT NOT NULL REFERENCES job_roles(code),
    phase TEXT NOT NULL REFERENCES job_phases(code),
    outcome TEXT,
    fenced INTEGER NOT NULL DEFAULT 0 CHECK (fenced IN (0, 1)),
    cleanup INTEGER NOT NULL DEFAULT 0 CHECK (cleanup IN (0, 1)),
    created_at REAL NOT NULL, execution_kind TEXT NOT NULL DEFAULT 'legacy' CHECK(execution_kind IN ('legacy','task')),
    CHECK (cleanup = 0 OR (fenced = 1 AND phase = 'cleaned')),
    FOREIGN KEY (unit_id, attempt_id) REFERENCES attempts(unit_id, id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id),
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id)
) STRICT;
CREATE TABLE job_leases (
    slot TEXT PRIMARY KEY NOT NULL,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    execution_fence TEXT NOT NULL UNIQUE,
    acquired_at REAL NOT NULL
) STRICT;
CREATE TABLE job_artifacts (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL REFERENCES artifact_kinds(code),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    media_type TEXT NOT NULL,
    digest TEXT NOT NULL,
    content BLOB,
    size INTEGER NOT NULL CHECK (size >= 0 AND size <= 1048576),
    codec TEXT NOT NULL DEFAULT 'identity' CHECK (codec IN ('identity','zlib')),
    raw_size INTEGER CHECK (raw_size IS NULL OR (raw_size >= 0 AND raw_size <= 1048576)),
    created_at REAL NOT NULL DEFAULT 0,
    expires_at REAL CHECK (expires_at IS NULL OR expires_at >= created_at),
    expired_at REAL,
    CHECK ((content IS NULL) = (expired_at IS NOT NULL)),
    CHECK (content IS NULL OR size = length(content)),
    CHECK (codec != 'zlib' OR raw_size IS NOT NULL),
    CHECK (codec != 'identity' OR raw_size IS NULL OR raw_size = size),
    CHECK (expired_at IS NULL OR (expired_at >= created_at + 2592000 AND (expires_at IS NULL OR expired_at >= expires_at))),
    PRIMARY KEY (job_id, kind, sequence)
) STRICT;
CREATE TABLE artifact_pins (
    job_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    purpose TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    PRIMARY KEY (job_id,kind,sequence,purpose,reference_id),
    FOREIGN KEY (job_id,kind,sequence) REFERENCES job_artifacts(job_id,kind,sequence)
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
    kind TEXT NOT NULL REFERENCES event_kinds(code),
    at REAL NOT NULL,
    actor_id TEXT,
    payload_json TEXT NOT NULL CHECK (json_valid(payload_json) AND length(CAST(payload_json AS BLOB)) <= 65536)
) STRICT;
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
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536), refreshing INTEGER NOT NULL DEFAULT 0 CHECK (refreshing IN (0,1)),
    UNIQUE (repository_id, kind, collection_generation)
) STRICT;
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
, uncertain_since REAL, conflict_since REAL, escalation_at REAL) STRICT;
CREATE TABLE merge_requests (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    candidate_id TEXT NOT NULL,
    effect_id TEXT NOT NULL UNIQUE REFERENCES external_effects(id),
    fencing_generation INTEGER NOT NULL CHECK (fencing_generation > 0),
    status TEXT NOT NULL CHECK (status IN ('pending', 'uncertain', 'merged', 'refused', 'superseded')),
    merge_oid TEXT,
    created_at REAL NOT NULL, send_count INTEGER NOT NULL DEFAULT 0 CHECK (send_count >= 0), last_sent_at REAL,
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id)
) STRICT;
CREATE TABLE discussions (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER REFERENCES repositories(id),
    unit_id TEXT REFERENCES units(id),
    issue_id TEXT REFERENCES issues(id),
    title TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
, status TEXT NOT NULL DEFAULT 'idle', source_revision TEXT, requested_at REAL, cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)), reason TEXT, partial TEXT, origin TEXT, presence_json TEXT CHECK (presence_json IS NULL OR (json_valid(presence_json) AND length(CAST(presence_json AS BLOB)) <= 65536)), current_execution_id TEXT REFERENCES discussion_executions(id), auto_retry TEXT, avoid_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(avoid_json) AND length(CAST(avoid_json AS BLOB))<=65536)) STRICT;
CREATE TABLE messages (
    id TEXT PRIMARY KEY NOT NULL,
    discussion_id TEXT NOT NULL REFERENCES discussions(id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    author_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('operator', 'assistant', 'system')),
    content TEXT NOT NULL CHECK (length(CAST(content AS BLOB)) <= 1048576),
    created_at REAL NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json) AND length(CAST(metadata_json AS BLOB)) <= 65536),
    UNIQUE (discussion_id, sequence)
) STRICT;
CREATE TABLE discussion_executions (
    id TEXT PRIMARY KEY NOT NULL,
    discussion_id TEXT NOT NULL REFERENCES discussions(id),
    phase TEXT NOT NULL REFERENCES job_phases(code),
    fenced INTEGER NOT NULL DEFAULT 0 CHECK (fenced IN (0, 1)),
    cleanup INTEGER NOT NULL DEFAULT 0 CHECK (cleanup IN (0, 1)),
    created_at REAL NOT NULL, completion_at REAL, source_created_at REAL,
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
CREATE TABLE provider_cooldowns (
    account_ref TEXT PRIMARY KEY NOT NULL,
    provider TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    retry_at REAL NOT NULL,
    observed_at REAL NOT NULL
, source_job_id TEXT REFERENCES jobs(id)) STRICT;
CREATE TABLE provider_observations (
    id TEXT PRIMARY KEY NOT NULL,
    provider TEXT NOT NULL,
    account_ref TEXT NOT NULL,
    readiness TEXT NOT NULL CHECK (readiness IN ('ready', 'unavailable', 'unknown')),
    reason_code TEXT,
    observed_at REAL NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536)
) STRICT;
CREATE TABLE memory_profiles (
    id TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    role TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    memory_bytes INTEGER NOT NULL CHECK (memory_bytes > 0),
    source_job_id TEXT REFERENCES jobs(id),
    observed_at REAL NOT NULL, allocation TEXT,
    UNIQUE (repository_id, role, revision)
) STRICT;
CREATE TABLE unit_environment (
    unit_id TEXT NOT NULL REFERENCES units(id),
    revision INTEGER NOT NULL CHECK (revision > 0),
    choice TEXT NOT NULL,
    override TEXT,
    retry_tier INTEGER NOT NULL DEFAULT 0 CHECK (retry_tier >= 0),
    memory_profile_id TEXT REFERENCES memory_profiles(id),
    created_at REAL NOT NULL, requirements_revision_id TEXT REFERENCES issue_revisions(id), config_hash TEXT, source_job_id TEXT REFERENCES jobs(id), member_id TEXT REFERENCES unit_issues(id),
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
, source_id TEXT, error TEXT, source_fields_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_fields_json))) STRICT;
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
CREATE TABLE runtime_controls (
    name TEXT PRIMARY KEY NOT NULL,
    generation INTEGER NOT NULL DEFAULT 1 CHECK (generation > 0),
    enabled INTEGER CHECK (enabled IN (0, 1)),
    cursor TEXT,
    heartbeat_at REAL,
    updated_at REAL NOT NULL
, source_generation TEXT, details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536)) STRICT;
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
    revoked_at REAL, csrf TEXT, operator_login TEXT,
    CHECK (expires_at > created_at)
) STRICT;
CREATE TABLE operator_flows (
    id TEXT PRIMARY KEY NOT NULL,
    state_hash TEXT NOT NULL UNIQUE,
    nonce_hash TEXT NOT NULL,
    return_path TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    consumed_at REAL, verifier TEXT, source_epoch TEXT,
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
    revoked_at REAL, source_epoch TEXT, instance_id TEXT, setup_generation TEXT, origin TEXT, scope_json TEXT CHECK (scope_json IS NULL OR (json_valid(scope_json) AND length(CAST(scope_json AS BLOB)) <= 65536)), access_hash TEXT, access_expires REAL, refresh_hash TEXT, idle_expires REAL, last_used_at REAL, revocation_reason TEXT, next_refresh_at REAL, operator_login TEXT,
    CHECK (expires_at > created_at)
) STRICT;
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
, finished_at REAL, error_code TEXT, expected_revision TEXT) STRICT;
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
CREATE TABLE inventory_pull_summaries (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    github_number INTEGER NOT NULL CHECK (github_number > 0),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    author_login TEXT NOT NULL,
    branch TEXT NOT NULL,
    forge_owned INTEGER NOT NULL CHECK (forge_owned IN (0,1)),
    observation_id TEXT NOT NULL REFERENCES repository_observations(id),
    PRIMARY KEY (repository_id, github_number)
) STRICT;
CREATE TABLE inventory_collaborators (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    login TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('collaborator','recent_author')),
    observation_id TEXT NOT NULL REFERENCES repository_observations(id),
    details_json TEXT NOT NULL CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
    PRIMARY KEY (repository_id, login, source)
) STRICT;
CREATE TABLE issue_action_requests (
    id TEXT PRIMARY KEY NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issues(id),
    action TEXT NOT NULL,
    expected_revision TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','applied','rejected')),
    reason TEXT NOT NULL,
    message TEXT,
    plan_json TEXT CHECK (plan_json IS NULL OR (json_valid(plan_json) AND length(CAST(plan_json AS BLOB)) <= 65536)),
    environment_json TEXT CHECK (environment_json IS NULL OR (json_valid(environment_json) AND length(CAST(environment_json AS BLOB)) <= 65536)),
    created_at REAL NOT NULL
) STRICT;
CREATE TABLE operator_requests (
    id TEXT PRIMARY KEY NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND length(CAST(result_json AS BLOB)) <= 65536))
) STRICT;
CREATE TABLE operator_auth_configuration (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    origin TEXT NOT NULL,
    client_id TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    credential_digest TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    source_epoch TEXT NOT NULL,
    authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch > 0)
) STRICT;
CREATE TABLE consumed_refresh_tokens (
    token_hash TEXT PRIMARY KEY NOT NULL,
    connection_id TEXT NOT NULL REFERENCES client_connections(id),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    UNIQUE (connection_id, sequence)
) STRICT;
CREATE TABLE client_pairings (
    device_hash TEXT PRIMARY KEY NOT NULL,
    user_code TEXT NOT NULL UNIQUE,
    instance_id TEXT NOT NULL,
    setup_generation TEXT NOT NULL,
    source_epoch TEXT NOT NULL,
    origin TEXT NOT NULL,
    client_name TEXT NOT NULL,
    capabilities_json TEXT NOT NULL CHECK (json_valid(capabilities_json) AND length(CAST(capabilities_json AS BLOB)) <= 65536),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json) AND length(CAST(scope_json AS BLOB)) <= 65536),
    expires_at REAL NOT NULL,
    poll_interval REAL NOT NULL CHECK (poll_interval > 0),
    next_poll_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','denied','consumed')),
    connection_id TEXT REFERENCES client_connections(id)
) STRICT;
CREATE TABLE auth_rate_limits (
    kind TEXT NOT NULL CHECK (kind IN ('device','lookup')),
    subject TEXT NOT NULL,
    window_start REAL NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    PRIMARY KEY (kind,subject)
) STRICT;
CREATE TABLE access_role_observations (
    scope TEXT NOT NULL CHECK (scope IN ('live','setup')),
    role TEXT NOT NULL CHECK (role IN ('build','review','audit','controller')),
    status TEXT NOT NULL,
    observed_at REAL NOT NULL,
    details_json TEXT NOT NULL CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
    PRIMARY KEY (scope,role)
) STRICT;
CREATE TABLE access_repository_observations (
    scope TEXT NOT NULL,
    role TEXT NOT NULL,
    repository_id INTEGER NOT NULL CHECK (repository_id > 0),
    full_name TEXT NOT NULL,
    archived INTEGER NOT NULL CHECK (archived IN (0,1)),
    disabled INTEGER NOT NULL CHECK (disabled IN (0,1)),
    position INTEGER NOT NULL CHECK (position >= 0),
    PRIMARY KEY (scope,role,repository_id),
    FOREIGN KEY (scope,role) REFERENCES access_role_observations(scope,role)
) STRICT;
CREATE TABLE repository_setup_observations (
    repository_id INTEGER PRIMARY KEY REFERENCES repositories(id),
    status TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    started_at REAL NOT NULL,
    previous_status TEXT,
    report_json TEXT CHECK (report_json IS NULL OR (json_valid(report_json) AND length(CAST(report_json AS BLOB)) <= 65536)),
    last_known_json TEXT CHECK (last_known_json IS NULL OR (json_valid(last_known_json) AND length(CAST(last_known_json AS BLOB)) <= 65536)),
    source_fields_json TEXT NOT NULL CHECK (json_valid(source_fields_json))
) STRICT;
CREATE TABLE setup_instances (
    scope TEXT PRIMARY KEY NOT NULL CHECK (scope IN ('live','setup')),
    generation TEXT NOT NULL,
    phase TEXT NOT NULL,
    owner TEXT NOT NULL,
    account_type TEXT NOT NULL,
    settings_json TEXT NOT NULL CHECK (json_valid(settings_json) AND length(CAST(settings_json AS BLOB)) <= 65536)
, revision INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0)) STRICT;
CREATE TABLE auxiliary_artifacts (
    execution_id TEXT NOT NULL REFERENCES auxiliary_executions(id),
    kind TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    digest TEXT NOT NULL,
    content BLOB NOT NULL CHECK (length(content) <= 1048576),
    PRIMARY KEY (execution_id,kind,sequence)
) STRICT;
CREATE TABLE auxiliary_leases (
    slot TEXT PRIMARY KEY NOT NULL,
    execution_id TEXT NOT NULL UNIQUE REFERENCES auxiliary_executions(id),
    execution_fence TEXT NOT NULL UNIQUE,
    acquired_at REAL NOT NULL
) STRICT;
CREATE TABLE deployment_profiles (
    repository_id INTEGER PRIMARY KEY REFERENCES repositories(id),
    status TEXT NOT NULL CHECK (status IN ('idle','running','proposed','confirmed','rejected','failed')),
    workflows_digest TEXT,
    head_oid TEXT,
    execution_id TEXT REFERENCES auxiliary_executions(id),
    proposal_json TEXT CHECK (proposal_json IS NULL OR (json_valid(proposal_json) AND length(CAST(proposal_json AS BLOB)) <= 65536)),
    summary TEXT,
    reason TEXT,
    error TEXT,
    observed_at REAL,
    checked_at REAL NOT NULL,
    reread_requested INTEGER NOT NULL CHECK (reread_requested IN (0,1)),
    observation_json TEXT CHECK (observation_json IS NULL OR (json_valid(observation_json) AND length(CAST(observation_json AS BLOB)) <= 65536)),
    observed_json TEXT CHECK (observed_json IS NULL OR (json_valid(observed_json) AND length(CAST(observed_json AS BLOB)) <= 65536)),
    exposure_issue INTEGER,
    source_fields_json TEXT NOT NULL CHECK (json_valid(source_fields_json))
) STRICT;
CREATE TABLE unit_context (
    unit_id TEXT PRIMARY KEY NOT NULL REFERENCES units(id),
    summary_json TEXT CHECK (summary_json IS NULL OR (json_valid(summary_json) AND length(CAST(summary_json AS BLOB)) <= 65536)),
    reason TEXT,
    retry_plan_json TEXT CHECK (retry_plan_json IS NULL OR (json_valid(retry_plan_json) AND length(CAST(retry_plan_json AS BLOB)) <= 65536)),
    decision_request_json TEXT CHECK (decision_request_json IS NULL OR (json_valid(decision_request_json) AND length(CAST(decision_request_json AS BLOB)) <= 65536)),
    verification_request_json TEXT CHECK (verification_request_json IS NULL OR (json_valid(verification_request_json) AND length(CAST(verification_request_json AS BLOB)) <= 65536)),
    checks_settled TEXT,
    last_observed_at REAL,
    settled_observed INTEGER CHECK (settled_observed IS NULL OR settled_observed IN (0,1)),
    observation_interruptions INTEGER NOT NULL DEFAULT 0 CHECK (observation_interruptions >= 0)
) STRICT;
CREATE TABLE unit_environment_observations (
    unit_id TEXT PRIMARY KEY NOT NULL REFERENCES units(id),
    choice_json TEXT CHECK (choice_json IS NULL OR (json_valid(choice_json) AND length(CAST(choice_json AS BLOB)) <= 65536)),
    override_json TEXT CHECK (override_json IS NULL OR (json_valid(override_json) AND length(CAST(override_json AS BLOB)) <= 65536)),
    retry_json TEXT CHECK (retry_json IS NULL OR (json_valid(retry_json) AND length(CAST(retry_json AS BLOB)) <= 65536)),
    memory_retry_json TEXT CHECK (memory_retry_json IS NULL OR (json_valid(memory_retry_json) AND length(CAST(memory_retry_json AS BLOB)) <= 65536)),
    label TEXT
, environment_hold_json TEXT CHECK(environment_hold_json IS NULL OR json_valid(environment_hold_json))) STRICT;
CREATE TABLE dependency_observations (
    unit_id TEXT PRIMARY KEY NOT NULL REFERENCES units(id),
    requirements_body TEXT,
    evidence TEXT,
    reason TEXT,
    checked_at REAL
) STRICT;
CREATE TABLE dependency_observation_members (
    unit_id TEXT NOT NULL REFERENCES dependency_observations(unit_id),
    kind TEXT NOT NULL CHECK (kind IN ('blocked','declared')),
    position INTEGER NOT NULL CHECK (position >= 0),
    value_json TEXT NOT NULL CHECK (json_valid(value_json) AND length(CAST(value_json AS BLOB)) <= 65536),
    PRIMARY KEY (unit_id,kind,position)
) STRICT;
CREATE TABLE attempt_failures (
    attempt_id TEXT PRIMARY KEY NOT NULL REFERENCES attempts(id),
    unit_id TEXT NOT NULL REFERENCES units(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    role TEXT,
    cause TEXT,
    investigation TEXT,
    retry_stage TEXT,
    evidence_kind TEXT NOT NULL REFERENCES artifact_kinds(code),
    findings_json TEXT CHECK (findings_json IS NULL OR (json_valid(findings_json) AND length(CAST(findings_json AS BLOB)) <= 65536)),
    summary_json TEXT CHECK (summary_json IS NULL OR (json_valid(summary_json) AND length(CAST(summary_json AS BLOB)) <= 65536)),
    at REAL NOT NULL, recovery_required INTEGER NOT NULL DEFAULT 0 CHECK (recovery_required IN (0,1)),
    UNIQUE (unit_id,position)
) STRICT;
CREATE TABLE job_launches (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
    slot TEXT NOT NULL,
    execution_fence TEXT NOT NULL UNIQUE,
    branch TEXT NOT NULL,
    publisher TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_id TEXT,
    model TEXT,
    runner_config_json TEXT NOT NULL CHECK (json_valid(runner_config_json) AND length(CAST(runner_config_json AS BLOB)) <= 65536),
    policy_json TEXT NOT NULL CHECK (json_valid(policy_json) AND length(CAST(policy_json AS BLOB)) <= 65536),
    context_json TEXT NOT NULL CHECK (json_valid(context_json) AND length(CAST(context_json AS BLOB)) <= 65536),
    deadline REAL NOT NULL
) STRICT;
CREATE TABLE job_execution_state (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES job_launches(job_id),
    container_id TEXT,
    captured_at REAL,
    capture_authorized INTEGER NOT NULL DEFAULT 0 CHECK (capture_authorized IN (0,1)),
    result_revoked INTEGER NOT NULL DEFAULT 0 CHECK (result_revoked IN (0,1)), observed_at REAL, observed_status TEXT
    CHECK (observed_status IN ('missing','created','running','paused','restarting','removing','exited','dead')), started_at REAL,
    CHECK (capture_authorized = 0 OR captured_at IS NOT NULL)
) STRICT;
CREATE TABLE candidate_effects (
    effect_id TEXT PRIMARY KEY NOT NULL REFERENCES external_effects(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_id TEXT REFERENCES jobs(id),
    fencing_generation INTEGER NOT NULL CHECK (fencing_generation > 0),
    membership_generation INTEGER NOT NULL CHECK (membership_generation > 0)
) STRICT;
CREATE TABLE dependency_checks (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    member_id TEXT NOT NULL REFERENCES unit_issues(id),
    requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
    generation INTEGER NOT NULL CHECK (generation>0),
    clear INTEGER NOT NULL CHECK (clear IN (0,1)),
    facts_json TEXT NOT NULL CHECK (json_valid(facts_json) AND length(CAST(facts_json AS BLOB))<=65536),
    observed_at REAL NOT NULL,
    UNIQUE(member_id,generation)
) STRICT;
CREATE TABLE environment_label_effects (
    effect_id TEXT PRIMARY KEY NOT NULL REFERENCES external_effects(id),
    unit_id TEXT NOT NULL REFERENCES units(id),
    revision INTEGER NOT NULL,
    member_id TEXT NOT NULL REFERENCES unit_issues(id),
    requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
    target_label TEXT NOT NULL CHECK(target_label IN ('env:standard','env:full')),
    before_json TEXT NOT NULL CHECK(json_valid(before_json)),
    FOREIGN KEY(unit_id,revision) REFERENCES unit_environment(unit_id,revision)
) STRICT;
CREATE TABLE environment_job_retries (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK(kind IN ('memory','services')),
    selection_json TEXT NOT NULL CHECK(json_valid(selection_json) AND length(CAST(selection_json AS BLOB))<=65536),
    at REAL NOT NULL
) STRICT;
CREATE TABLE operator_action_authority (
    request_id TEXT PRIMARY KEY NOT NULL REFERENCES issue_action_requests(id),
    actor_id TEXT NOT NULL,
    unit_id TEXT REFERENCES units(id),
    expected_unit_version INTEGER CHECK(expected_unit_version IS NULL OR expected_unit_version>0),
    expected_issue_version INTEGER NOT NULL CHECK(expected_issue_version>0),
    payload_digest TEXT NOT NULL,
    gate_id TEXT,
    result_json TEXT CHECK(result_json IS NULL OR (json_valid(result_json) AND length(CAST(result_json AS BLOB))<=65536)),
    updated_at REAL NOT NULL
) STRICT;
CREATE TABLE operator_action_effects (
    request_id TEXT NOT NULL REFERENCES issue_action_requests(id),
    step TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','uncertain')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(CAST(payload_json AS BLOB))<=65536),
    receipt_json TEXT CHECK(receipt_json IS NULL OR (json_valid(receipt_json) AND length(CAST(receipt_json AS BLOB))<=65536)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(request_id,step)
) STRICT;
CREATE TABLE operator_observation_requests (
    issue_id TEXT PRIMARY KEY NOT NULL REFERENCES issues(id),
    actor_id TEXT NOT NULL,
    requested_at REAL NOT NULL
) STRICT;
CREATE TABLE candidate_reviewer_requests (
 candidate_id TEXT NOT NULL REFERENCES candidates(id),
 request_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 logins_json TEXT NOT NULL CHECK(json_valid(logins_json) AND json_type(logins_json)='array' AND length(CAST(logins_json AS BLOB))<=4096),
 requested_at REAL NOT NULL,
 issue_assignment INTEGER NOT NULL CHECK(issue_assignment IN (0,1)),
 PRIMARY KEY(candidate_id,request_id)
) STRICT;
CREATE TABLE operator_verification_authority (
 request_id TEXT PRIMARY KEY NOT NULL,
 actor_id TEXT NOT NULL,
 issue_id TEXT NOT NULL REFERENCES issues(id),
 unit_id TEXT NOT NULL REFERENCES units(id),
 expected_unit_version INTEGER NOT NULL CHECK(expected_unit_version>0),
 expected_revision TEXT NOT NULL,
 gate_id TEXT NOT NULL REFERENCES unit_gates(id),
 statement_digest TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE job_public_metadata (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
 slot TEXT,
 provider TEXT,
 provider_id TEXT,
 model TEXT,
 deadline REAL,
 observed_at REAL,
 started_at REAL,
 source_phase TEXT,
 source_fenced INTEGER CHECK(source_fenced IS NULL OR source_fenced IN (0,1))
) STRICT;
CREATE TABLE auxiliary_runtime (
    execution_id TEXT PRIMARY KEY NOT NULL REFERENCES auxiliary_executions(id),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json) AND length(CAST(manifest_json AS BLOB)) <= 65536),
    deadline REAL NOT NULL,
    container_id TEXT,
    capacity_wait_since REAL,
    capture_digest TEXT
) STRICT;
CREATE TABLE discussion_runtime (
    execution_id TEXT PRIMARY KEY NOT NULL REFERENCES discussion_executions(id),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json) AND length(CAST(manifest_json AS BLOB))<=262144),
    container_id TEXT,
    joined_at REAL,
    boot TEXT,
    completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),
    ended TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0,1)),
    excerpt TEXT CHECK(excerpt IS NULL OR length(CAST(excerpt AS BLOB))<=16384)
) STRICT;
CREATE TABLE revision_presentation (
    revision_id TEXT PRIMARY KEY NOT NULL REFERENCES issue_revisions(id),
    body_truncated INTEGER NOT NULL CHECK(body_truncated IN (0,1)),
    source_content_hash TEXT NOT NULL
) STRICT;
CREATE TABLE revision_display_dependencies (
    revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
    issue_number INTEGER NOT NULL CHECK(issue_number>0),
    PRIMARY KEY(revision_id,issue_number)
) STRICT;
CREATE TABLE inventory_pull_issue_links (
    repository_id INTEGER NOT NULL,
    pull_number INTEGER NOT NULL,
    issue_number INTEGER NOT NULL CHECK(issue_number>0),
    forge_attempt INTEGER NOT NULL CHECK(forge_attempt IN (0,1)),
    PRIMARY KEY(repository_id,pull_number,issue_number),
    FOREIGN KEY(repository_id,pull_number) REFERENCES inventory_pull_summaries(repository_id,github_number) ON DELETE CASCADE
) STRICT;
CREATE TABLE issue_label_attributions (
    issue_id TEXT NOT NULL REFERENCES issues(id),
    label TEXT NOT NULL,
    login TEXT,
    applied_at TEXT,
    position INTEGER NOT NULL,
    labels_hash TEXT NOT NULL,
    PRIMARY KEY(issue_id,label)
) STRICT;
CREATE TABLE issue_discovery_observations (
 issue_id TEXT PRIMARY KEY NOT NULL REFERENCES issues(id),
 revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
 reason_code TEXT NOT NULL CHECK(reason_code IN ('requirements_stale','remote_timestamp_invalid','admission_conflict')),
 observed_at REAL NOT NULL,
 retry_after REAL NOT NULL CHECK(retry_after>=observed_at)
) STRICT;
CREATE TABLE job_members (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    member_id TEXT NOT NULL REFERENCES unit_issues(id),
    requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
    PRIMARY KEY(job_id,member_id)
) STRICT;
CREATE TABLE unit_builders (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL UNIQUE REFERENCES units(id),
    actor_id TEXT NOT NULL,
    created_at REAL NOT NULL
) STRICT;
CREATE TABLE builder_assignments (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
    builder_id TEXT NOT NULL REFERENCES unit_builders(id),
    stage TEXT NOT NULL CHECK(stage IN ('planning','integration'))
) STRICT;
CREATE TABLE delegation_plans (
    id TEXT PRIMARY KEY NOT NULL,
    builder_id TEXT NOT NULL REFERENCES unit_builders(id),
    parent_job_id TEXT NOT NULL UNIQUE REFERENCES builder_assignments(job_id),
    input_manifest_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL CHECK(json_valid(plan_json) AND json_type(plan_json)='object' AND length(CAST(plan_json AS BLOB))<=65536),
    created_at REAL NOT NULL
) STRICT;
CREATE TABLE unit_budget_lineage (
    unit_id TEXT NOT NULL REFERENCES units(id),
    budget_generation INTEGER NOT NULL CHECK(budget_generation>0),
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    PRIMARY KEY(unit_id,budget_generation,attempt_id)
) STRICT;
CREATE TABLE unit_composition_sources (
    unit_id TEXT NOT NULL REFERENCES units(id),
    source_unit_id TEXT NOT NULL REFERENCES units(id),
    source_candidate_id TEXT REFERENCES candidates(id),
    PRIMARY KEY(unit_id,source_unit_id)
) STRICT;
CREATE TABLE unit_publication_targets (
    unit_id TEXT PRIMARY KEY REFERENCES units(id),
    target_json TEXT NOT NULL CHECK(json_valid(target_json) AND length(target_json)<=2048)
) STRICT;
CREATE TABLE repository_planning (
 repository_id INTEGER PRIMARY KEY REFERENCES repositories(id),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 rules TEXT NOT NULL DEFAULT '' CHECK(length(CAST(rules AS BLOB))<=16384),
 rules_revision INTEGER NOT NULL DEFAULT 1 CHECK(rules_revision>0),
 proposals_enabled INTEGER NOT NULL DEFAULT 1 CHECK(proposals_enabled IN(0,1)),
 next_triage_at REAL NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL,
 actor_id TEXT NOT NULL
) STRICT;
CREATE TABLE repository_note_revisions (
 note_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),
 repository_id INTEGER NOT NULL REFERENCES repositories(id),
 kind TEXT NOT NULL CHECK(kind IN('convention','decision','lesson','question','next_action')),
 content TEXT NOT NULL CHECK(length(CAST(content AS BLOB)) BETWEEN 1 AND 8192),
 sources_json TEXT NOT NULL CHECK(json_valid(sources_json) AND length(CAST(sources_json AS BLOB))<=8192),
 deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN(0,1)),
 created_at REAL NOT NULL,
 actor_id TEXT NOT NULL,
 PRIMARY KEY(note_id,revision)
) STRICT;
CREATE TABLE repository_planning_requests (
 request_id TEXT PRIMARY KEY,
 actor_id TEXT NOT NULL,
 operation TEXT NOT NULL,
 payload_digest TEXT NOT NULL,
 result_json TEXT NOT NULL CHECK(json_valid(result_json) AND length(CAST(result_json AS BLOB))<=65536),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE triage_proposals (
 id TEXT PRIMARY KEY,
 repository_id INTEGER NOT NULL REFERENCES repositories(id),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
 rules_revision INTEGER NOT NULL CHECK(rules_revision>0),
 base_branch TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN('draft','proposed','accepted','rejected','superseded')),
 rationale TEXT NOT NULL CHECK(length(CAST(rationale AS BLOB))<=8192),
 created_at REAL NOT NULL,
 actor_id TEXT NOT NULL,
 accepted_at REAL,
 accepted_by TEXT
) STRICT;
CREATE TABLE triage_proposal_inputs (
 proposal_id TEXT NOT NULL REFERENCES triage_proposals(id),
 issue_id TEXT NOT NULL REFERENCES issues(id),
 issue_version INTEGER NOT NULL CHECK(issue_version>0),
 requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
 unit_id TEXT REFERENCES units(id),
 unit_version INTEGER,
 member_id TEXT REFERENCES unit_issues(id),
 candidate_id TEXT REFERENCES candidates(id),
 PRIMARY KEY(proposal_id,issue_id)
) STRICT;
CREATE TABLE triage_proposal_rows (
 id TEXT PRIMARY KEY,
 proposal_id TEXT NOT NULL REFERENCES triage_proposals(id),
 position INTEGER NOT NULL CHECK(position>=0),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 action TEXT NOT NULL CHECK(action IN('close','relabel','park','group','order','question','keep')),
 reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB)) BETWEEN 1 AND 8192),
 rule_quote TEXT CHECK(rule_quote IS NULL OR length(CAST(rule_quote AS BLOB))<=16384),
 parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json) AND length(CAST(parameters_json AS BLOB))<=65536),
 status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN('proposed','accepted','rejected')),
 decision_id TEXT,
 UNIQUE(proposal_id,position)
) STRICT;
CREATE TABLE triage_row_members (
 row_id TEXT NOT NULL REFERENCES triage_proposal_rows(id),
 issue_id TEXT NOT NULL REFERENCES issues(id),
 PRIMARY KEY(row_id,issue_id)
) STRICT;
CREATE TABLE triage_effect_authority (
 effect_id TEXT PRIMARY KEY REFERENCES external_effects(id),
 proposal_id TEXT NOT NULL REFERENCES triage_proposals(id),
 row_id TEXT NOT NULL REFERENCES triage_proposal_rows(id),
 issue_id TEXT NOT NULL REFERENCES issues(id),
 issue_version INTEGER NOT NULL,
 requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
 actor_id TEXT NOT NULL,
 gate_id TEXT REFERENCES unit_gates(id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id)
) STRICT;
CREATE TABLE triage_effect_members (
 effect_id TEXT NOT NULL REFERENCES external_effects(id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id),
 PRIMARY KEY(effect_id,member_id)
) STRICT;
CREATE TABLE triage_row_history (
 row_id TEXT NOT NULL REFERENCES triage_proposal_rows(id),
 revision INTEGER NOT NULL,
 action TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB))<=8192),
 rule_quote TEXT,
 parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json) AND length(CAST(parameters_json AS BLOB))<=65536),
 actor_id TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(row_id,revision)
) STRICT;
CREATE TABLE triage_row_member_history (
 row_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 issue_id TEXT NOT NULL REFERENCES issues(id),
 PRIMARY KEY(row_id,revision,issue_id),
 FOREIGN KEY(row_id,revision) REFERENCES triage_row_history(row_id,revision)
) STRICT;
CREATE TABLE repository_rule_history (
 repository_id INTEGER NOT NULL REFERENCES repositories(id),
 revision INTEGER NOT NULL,
 rules TEXT NOT NULL CHECK(length(CAST(rules AS BLOB))<=16384),
 actor_id TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(repository_id,revision)
) STRICT;
CREATE TABLE auxiliary_executions (
 id TEXT PRIMARY KEY NOT NULL,
 domain TEXT NOT NULL CHECK(domain IN('setup','deployment','qualification','triage')),
 owner_id TEXT NOT NULL,
 position INTEGER NOT NULL CHECK(position>=0),
 phase TEXT NOT NULL REFERENCES job_phases(code),
 fenced INTEGER NOT NULL CHECK(fenced IN(0,1)),
 cleanup INTEGER NOT NULL CHECK(cleanup IN(0,1)),
 created_at REAL,
 CHECK(cleanup=0 OR (fenced=1 AND phase='cleaned')),
 UNIQUE(domain,owner_id,position)
) STRICT;
CREATE TABLE repository_triage_runs (
 id TEXT PRIMARY KEY REFERENCES triage_proposals(id),
 repository_id INTEGER NOT NULL REFERENCES repositories(id),
 status TEXT NOT NULL CHECK(status IN('queued','running','captured','complete','failed','cancelled')),
 execution_id TEXT UNIQUE REFERENCES auxiliary_executions(id),
 requested_at REAL NOT NULL,
 requested_by TEXT NOT NULL,
 error TEXT,
 retry_at REAL
) STRICT;
CREATE TABLE triage_candidate_context (
 proposal_id TEXT NOT NULL REFERENCES triage_proposals(id),
 candidate_id TEXT NOT NULL REFERENCES candidates(id),
 author_json TEXT NOT NULL CHECK(json_valid(author_json) AND length(CAST(author_json AS BLOB))<=1024),
 PRIMARY KEY(proposal_id,candidate_id)
) STRICT;
CREATE TABLE prerequisite_orders (
    id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT UNIQUE NOT NULL,
    actor_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    requirements_revision_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation>0),
    references_json TEXT NOT NULL CHECK(json_valid(references_json) AND length(CAST(references_json AS BLOB))<=32768),
    created_at REAL NOT NULL,
    FOREIGN KEY(issue_id,requirements_revision_id) REFERENCES issue_revisions(issue_id,id),
    UNIQUE(issue_id,generation)
) STRICT;
CREATE TABLE issue_environment_observations (
    issue_id TEXT PRIMARY KEY NOT NULL REFERENCES issues(id),
    choice_json TEXT CHECK(choice_json IS NULL OR json_valid(choice_json)),
    override_json TEXT CHECK(override_json IS NULL OR json_valid(override_json)),
    retry_json TEXT CHECK(retry_json IS NULL OR json_valid(retry_json)),
    memory_retry_json TEXT CHECK(memory_retry_json IS NULL OR json_valid(memory_retry_json)),
    label TEXT,
    environment_hold_json TEXT CHECK(environment_hold_json IS NULL OR json_valid(environment_hold_json))
) STRICT;
CREATE TABLE prerequisite_identities (
    reference TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL CHECK(repository_id>0),
    github_id INTEGER NOT NULL CHECK(github_id>0),
    number INTEGER NOT NULL CHECK(number>0),
    kind TEXT NOT NULL CHECK(kind IN ('issue','pull_request')),
    observed_at REAL NOT NULL
) STRICT;
CREATE TABLE auxiliary_logs (
 execution_id TEXT PRIMARY KEY REFERENCES auxiliary_executions(id),
 digest TEXT NOT NULL,
 content BLOB CHECK(content IS NULL OR length(content)<=65536),
 created_at REAL NOT NULL,
 expires_at REAL NOT NULL CHECK(expires_at>=created_at),
 expired_at REAL,
 CHECK((content IS NOT NULL AND expired_at IS NULL) OR (content IS NULL AND expired_at IS NOT NULL AND expired_at>=expires_at))
) STRICT;
CREATE TABLE job_resource_observations (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    container_id TEXT NOT NULL,
    checked_at REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','partial','unavailable')),
    reason TEXT,
    sampled_at REAL,
    cpu_seconds REAL CHECK(cpu_seconds >= 0),
    memory_bytes INTEGER CHECK(memory_bytes >= 0),
    CHECK((sampled_at IS NULL) = (cpu_seconds IS NULL AND memory_bytes IS NULL))
) STRICT;
CREATE TABLE job_resource_receipts (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    usage_json TEXT NOT NULL CHECK(json_valid(usage_json) AND length(CAST(usage_json AS BLOB)) <= 2048)
) STRICT;
CREATE TABLE delivery_tasks (
 id TEXT PRIMARY KEY NOT NULL,
 builder_id TEXT NOT NULL REFERENCES unit_builders(id),
 origin_plan_id TEXT NOT NULL REFERENCES delegation_plans(id),
 task_key TEXT NOT NULL,
 executor TEXT NOT NULL CHECK(executor IN ('owner','child')),
 scope TEXT NOT NULL,
 internalizes_json TEXT NOT NULL CHECK(json_valid(internalizes_json)),
 created_at REAL NOT NULL,
 UNIQUE(origin_plan_id,task_key)
) STRICT;
CREATE TABLE delivery_task_members (
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id),
 requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
 PRIMARY KEY(task_id,member_id)
) STRICT;
CREATE TABLE delivery_task_dependencies (
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 prerequisite_task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 PRIMARY KEY(task_id,prerequisite_task_id), CHECK(task_id!=prerequisite_task_id)
) STRICT;
CREATE TABLE delegation_plan_tasks (
 plan_id TEXT NOT NULL REFERENCES delegation_plans(id),
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 PRIMARY KEY(plan_id,task_id)
) STRICT;
CREATE TABLE job_tasks (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
 plan_id TEXT NOT NULL REFERENCES delegation_plans(id),
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 worktree_key TEXT NOT NULL UNIQUE
) STRICT;
CREATE TABLE part_outputs (
 id TEXT PRIMARY KEY NOT NULL,
 unit_id TEXT NOT NULL REFERENCES units(id),
 member_id TEXT,
 attempt_id TEXT NOT NULL,
 requirements_revision_id TEXT REFERENCES issue_revisions(id),
 membership_generation INTEGER NOT NULL CHECK(membership_generation>0),
 commit_oid TEXT NOT NULL,base_oid TEXT NOT NULL,input_manifest_hash TEXT NOT NULL,accepted_at REAL NOT NULL,
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 FOREIGN KEY(unit_id,member_id) REFERENCES unit_issues(unit_id,id),
 FOREIGN KEY(unit_id,attempt_id) REFERENCES attempts(unit_id,id), UNIQUE(attempt_id),
 CHECK((member_id IS NULL)=(requirements_revision_id IS NULL))
) STRICT;
CREATE TABLE part_output_invalidations (
 output_id TEXT PRIMARY KEY NOT NULL REFERENCES part_outputs(id),reason TEXT NOT NULL,
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB))<=65536),created_at REAL NOT NULL
) STRICT;
CREATE TABLE unit_gates (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    candidate_id TEXT,
    member_id TEXT,
    subject_key TEXT NOT NULL,
    job_id TEXT REFERENCES jobs(id),
    code TEXT NOT NULL REFERENCES gate_codes(code),
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND json_type(details_json) = 'object' AND length(CAST(details_json AS BLOB)) <= 65536),
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolution TEXT CHECK (resolution IN ('operator','observation','recheck','time','evidence','transition','superseded')),
    CHECK ((resolved_at IS NULL) = (resolution IS NULL)),
    CHECK ((job_id IS NULL AND member_id IS NULL AND candidate_id IS NULL AND subject_key = 'unit') OR
           (job_id IS NULL AND member_id IS NOT NULL AND candidate_id IS NULL AND subject_key = 'member:' || member_id) OR
           (job_id IS NULL AND member_id IS NULL AND candidate_id IS NOT NULL AND subject_key = 'candidate:' || candidate_id) OR
           (job_id IS NOT NULL AND member_id IS NULL AND candidate_id IS NULL AND subject_key='job:'||job_id)),
    UNIQUE (unit_id, id),
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id)
) STRICT;
CREATE TABLE forge_problem_jobs (
 job_id TEXT NOT NULL REFERENCES jobs(id),
 gate_id TEXT NOT NULL REFERENCES unit_gates(id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id),
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=65536),
 PRIMARY KEY(job_id,gate_id,member_id)
) STRICT;
CREATE TABLE next_actions (
 unit_id TEXT NOT NULL REFERENCES units(id),
 owner TEXT NOT NULL,
 scope_key TEXT NOT NULL,
 unit_version INTEGER NOT NULL,
 fencing_generation INTEGER NOT NULL,
 membership_generation INTEGER NOT NULL,
 candidate_id TEXT REFERENCES candidates(id),
 member_id TEXT REFERENCES unit_issues(id),
 job_id TEXT REFERENCES jobs(id),
 reason TEXT NOT NULL,
 due_at REAL NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(unit_id,owner,scope_key)
) STRICT;
CREATE TABLE scheduling_orphans (
 member_id TEXT PRIMARY KEY NOT NULL REFERENCES unit_issues(id),
 unit_id TEXT NOT NULL REFERENCES units(id),
 unowned_since REAL NOT NULL
) STRICT;
CREATE TABLE candidate_merge_observations (
 candidate_id TEXT PRIMARY KEY NOT NULL REFERENCES candidates(id),
 state TEXT NOT NULL CHECK(state IN ('ready','conflict','unknown')),
 first_at REAL NOT NULL,
 observed_at REAL NOT NULL
) STRICT;
CREATE TABLE forge_problem_reports (
 signature TEXT PRIMARY KEY NOT NULL CHECK(length(signature)=64),
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=8192),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE forge_problem_report_jobs (
 signature TEXT NOT NULL REFERENCES forge_problem_reports(signature),
 job_id TEXT NOT NULL REFERENCES jobs(id),
 PRIMARY KEY(signature,job_id)
) STRICT;
CREATE TABLE recovery_assignments (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
 failed_job_id TEXT NOT NULL REFERENCES jobs(id),
 failed_attempt_id TEXT NOT NULL REFERENCES attempt_failures(attempt_id),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE recovery_findings (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES "recovery_assignments"(job_id),
 incomplete INTEGER NOT NULL CHECK(incomplete IN(0,1)),
 accepted INTEGER NOT NULL CHECK(accepted IN(0,1)),
 findings_json TEXT NOT NULL CHECK(json_valid(findings_json) AND length(CAST(findings_json AS BLOB))<=65536),
 recorded_at REAL NOT NULL
) STRICT;
CREATE TABLE recovery_memory_retries (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES recovery_assignments(job_id),
 allocation TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE auxiliary_execution_waits (
 execution_id TEXT PRIMARY KEY NOT NULL,
 domain TEXT NOT NULL CHECK(domain IN('triage','discussion')),
 owner_id TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN('forge_problem','provider_error','oom')),
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=8192),
 runner_config_json TEXT NOT NULL CHECK(json_valid(runner_config_json)),
 created_at REAL NOT NULL,
 resolved_at REAL
) STRICT;
CREATE TABLE forge_problem_report_auxiliary (
 signature TEXT NOT NULL REFERENCES forge_problem_reports(signature),
 execution_id TEXT NOT NULL REFERENCES auxiliary_execution_waits(execution_id),
 PRIMARY KEY(signature,execution_id)
) STRICT;
CREATE TABLE worker_claim_attempts (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id),
 claim_key TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(reason IN ('toolchain','sandbox','dependency_access','resources','ci_evidence','services','github_permission')),
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=65536),
 revisions_json TEXT NOT NULL CHECK(json_valid(revisions_json) AND length(revisions_json)<=8192),
 disposition TEXT NOT NULL CHECK(disposition IN ('deferred','repeated')),
 retry_at REAL NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE worker_claim_obligations (
 gate_id TEXT NOT NULL REFERENCES unit_gates(id),
 job_id TEXT NOT NULL REFERENCES worker_claim_attempts(job_id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id),
 released_at REAL,
 release_reason TEXT CHECK(release_reason IN ('operator','relevant_change','timer','environment_override')),
 CHECK((released_at IS NULL)=(release_reason IS NULL)),
 PRIMARY KEY(gate_id,job_id,member_id)
) STRICT;
CREATE TABLE publication_intents (
 id TEXT PRIMARY KEY NOT NULL,
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
 unit_id TEXT NOT NULL REFERENCES units(id),
 execution_fence TEXT NOT NULL,
 input_manifest_json TEXT NOT NULL CHECK(json_valid(input_manifest_json)),
 output_digest TEXT NOT NULL CHECK(length(output_digest)=64),
 output_json TEXT NOT NULL CHECK(json_valid(output_json) AND length(CAST(output_json AS BLOB))<=2097152),
 target_json TEXT NOT NULL CHECK(json_valid(target_json)),
 report_json TEXT NOT NULL CHECK(json_valid(report_json) AND length(CAST(report_json AS BLOB))<=65536),
 status TEXT NOT NULL CHECK(status IN('pending','working','uncertain','blocked','confirmed','superseded')),
 claim_json TEXT CHECK(claim_json IS NULL OR json_valid(claim_json)),
 version INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE TABLE publication_steps (
 intent_id TEXT NOT NULL REFERENCES publication_intents(id),
 key TEXT NOT NULL,
 request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
 status TEXT NOT NULL CHECK(status IN('prepared','sending','confirmed','refused')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 retry_at REAL,
 uncertain_since REAL,
 escalation_at REAL,
 receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(intent_id,key)
) STRICT;
CREATE TABLE publication_receipts (
 intent_id TEXT PRIMARY KEY NOT NULL REFERENCES publication_intents(id),
 receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json) AND length(CAST(receipt_json AS BLOB))<=65536),
 observed_at REAL NOT NULL
) STRICT;
CREATE TABLE mirror_policies (
 digest TEXT PRIMARY KEY CHECK(length(digest)=64),
 policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE mirror_sources (
 digest TEXT PRIMARY KEY CHECK(length(digest)=64),
 policy_digest TEXT NOT NULL REFERENCES mirror_policies(digest),
 source_json TEXT NOT NULL CHECK(json_valid(source_json)),
 created_at REAL NOT NULL,
 UNIQUE(digest,policy_digest)
) STRICT;
CREATE TABLE mirror_sessions (
 id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL,
 execution_fence TEXT NOT NULL,
 network_generation TEXT NOT NULL,
 source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
 policy_digest TEXT NOT NULL REFERENCES mirror_policies(digest),
 deadline REAL NOT NULL,
 created_at REAL NOT NULL,
 revoked_at REAL,
 FOREIGN KEY(source_digest,policy_digest) REFERENCES mirror_sources(digest,policy_digest),
 UNIQUE(id,job_id,execution_fence,source_digest,policy_digest),
 UNIQUE(job_id,execution_fence,network_generation)
) STRICT;
CREATE TABLE mirror_receipts (
 id TEXT PRIMARY KEY,
 session_id TEXT REFERENCES mirror_sessions(id),
 request_id TEXT REFERENCES mirror_receipts(id),
 job_id TEXT NOT NULL,
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=65536),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE mirror_claims (
 receipt_id TEXT PRIMARY KEY REFERENCES mirror_receipts(id),
 job_id TEXT NOT NULL,
 reason TEXT NOT NULL,
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=65536),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE mirror_job_inputs (
 job_id TEXT PRIMARY KEY,
 execution_fence TEXT NOT NULL,
 source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
 policy_digest TEXT NOT NULL REFERENCES mirror_policies(digest),
 session_id TEXT NOT NULL UNIQUE,
 notes_json TEXT NOT NULL CHECK(json_valid(notes_json) AND length(notes_json)<=1048576),
 publication_authorization_json TEXT NOT NULL CHECK(json_valid(publication_authorization_json) AND length(publication_authorization_json)<=65536),
 created_at REAL NOT NULL,
 FOREIGN KEY(source_digest,policy_digest) REFERENCES mirror_sources(digest,policy_digest),
 FOREIGN KEY(session_id,job_id,execution_fence,source_digest,policy_digest) REFERENCES mirror_sessions(id,job_id,execution_fence,source_digest,policy_digest)
) STRICT;
-- Registries. Their contents are part of the schema; triggers below make them immutable.
INSERT INTO artifact_kinds(code,retention) VALUES
('logs','raw'),
('output','raw'),
('live_tail','raw'),
('ci_transcript','raw'),
('launch_input','durable'),
('input_manifest','durable'),
('result','durable'),
('summary','durable'),
('failure_evidence','durable'),
('worker_capture','durable'),
('worktree_bundle','durable');
INSERT INTO close_codes(code,issue_outcome) VALUES
('github_completed','done'),
('github_not_planned','dropped'),
('github_duplicate','dropped'),
('operator_closed',NULL),
('combined',NULL),
('import_superseded',NULL);
INSERT INTO event_kinds(code) VALUES
('admitted'),
('attempt_fenced'),
('attempt_finished'),
('attempt_reserved'),
('budget_charged'),
('budget_corrected'),
('budget_reset'),
('candidate_selected'),
('candidate_superseded'),
('cleanup_verified'),
('closed'),
('decision_applied'),
('decision_rejected'),
('dissolved'),
('gate_opened'),
('gate_resolved'),
('issue_admitted'),
('issue_observed'),
('lease_released'),
('lifecycle_changed'),
('membership_changed'),
('merged'),
('part_accepted'),
('phase_changed'),
('unit_created'),
('legacy_event'),
('execution_progress'),
('external_effect_changed');
INSERT INTO exit_reasons(code) VALUES
('dropped'),
('dissolved'),
('merged'),
('closed'),
('combined');
INSERT INTO gate_codes(code,kind,priority,release,unit_scope,member_scope,candidate_scope,job_scope) VALUES
('operator_pause','intent',2,'operator',1,1,0,0),
('operator_backlog','intent',2,'operator',1,1,0,0),
('label_hold','intent',2,'observation',1,1,0,0),
('membership_decision','intent',2,'operator',1,1,0,0),
('operator_sync','intent',2,'observation',1,1,0,0),
('external_change','intent',2,'observation',1,1,0,0),
('ownership_conflict','intent',2,'observation',1,1,0,0),
('budget_exhausted','decision',3,'operator',1,0,0,0),
('approach_required','decision',3,'operator',1,1,0,0),
('decision_required','decision',3,'operator',1,1,0,0),
('repository_setup','readiness',4,'observation',1,0,0,0),
('environment','readiness',4,'recheck',1,1,0,0),
('observation_unavailable','readiness',4,'observation',1,1,1,0),
('legacy_reconciliation','readiness',4,'operator',1,1,0,0),
('dependency','dependency',5,'observation',1,1,0,0),
('recovery_pending','automatic',6,'transition',1,1,1,1),
('cleanup_pending','automatic',6,'transition',1,1,1,1),
('provider_unavailable','automatic',6,'time',1,0,0,0),
('retry_delay','automatic',6,'time',1,1,0,0),
('verification_human','candidate',7,'evidence',0,0,1,0),
('verification_ci','candidate',7,'evidence',0,0,1,0),
('approval_required','candidate',7,'evidence',0,0,1,0),
('merge_checks','candidate',7,'observation',0,0,1,0),
('result_pending','automatic',6,'transition',1,1,1,1),
('triage_sync','intent',2,'observation',0,1,0,0),
('forge_problem','readiness',4,'observation',1,1,1,1);
INSERT INTO job_phases(code) VALUES
('reserved'),
('starting'),
('running'),
('collecting'),
('finished'),
('cleaned');
INSERT INTO job_roles(code) VALUES
('build'),
('integration'),
('review'),
('audit'),
('recovery'),
('merge');
INSERT INTO part_states(code) VALUES
('pending'),
('building'),
('built'),
('failed'),
('removed'),
('done');
INSERT INTO unit_states(code,terminal) VALUES
('queued',0),
('building',0),
('integrating',0),
('reviewing',0),
('auditing',0),
('merging',0),
('merged',1),
('closed',1),
('dissolved',1);
INSERT INTO unit_transitions(from_state,to_state) VALUES
('queued','building'),
('queued','closed'),
('queued','dissolved'),
('queued','reviewing'),
('building','closed'),
('building','dissolved'),
('building','integrating'),
('building','reviewing'),
('integrating','building'),
('integrating','closed'),
('integrating','dissolved'),
('integrating','reviewing'),
('reviewing','auditing'),
('reviewing','building'),
('reviewing','closed'),
('reviewing','dissolved'),
('reviewing','integrating'),
('reviewing','merged'),
('auditing','building'),
('auditing','closed'),
('auditing','dissolved'),
('auditing','integrating'),
('auditing','merged'),
('auditing','merging'),
('merging','building'),
('merging','closed'),
('merging','dissolved'),
('merging','integrating'),
('merging','merged');
CREATE UNIQUE INDEX one_active_unit_per_issue ON unit_issues(issue_id) WHERE left_at IS NULL;
CREATE INDEX unit_members ON unit_issues(unit_id, left_at);
CREATE INDEX unit_jobs ON jobs(unit_id, cleanup, created_at);
CREATE INDEX unit_events ON events(unit_id, sequence);
CREATE INDEX issue_events ON events(issue_id, sequence);
CREATE INDEX open_units ON units(state, updated_at, id);
CREATE INDEX repository_units ON units(repository_id, state, updated_at, id);
CREATE INDEX repository_issues ON issues(repository_id, github_state, github_number);
CREATE INDEX attempt_budget ON attempts(unit_id, budget_generation);
CREATE INDEX job_attempt ON jobs(attempt_id);
CREATE INDEX latest_repository_observation ON repository_observations(repository_id, kind, collection_generation DESC);
CREATE INDEX prerequisite_dependents ON issue_dependencies(prerequisite_issue_id, issue_id);
CREATE INDEX candidate_verdicts ON candidate_evidence(candidate_id, kind, observed_at DESC);
CREATE INDEX pending_effects ON external_effects(status, retry_at, created_at);
CREATE UNIQUE INDEX one_pending_merge ON merge_requests(unit_id) WHERE status IN ('pending', 'uncertain');
CREATE INDEX provider_readiness ON provider_observations(account_ref, observed_at DESC);
CREATE UNIQUE INDEX one_active_configuration ON configuration(domain, subject) WHERE active = 1;
CREATE INDEX active_operator_sessions ON operator_sessions(operator_id, revoked_at, expires_at);
CREATE INDEX operator_connections ON client_connections(operator_id, revoked_at, expires_at);
CREATE UNIQUE INDEX one_live_job_per_member ON jobs(member_id)
    WHERE member_id IS NOT NULL AND fenced=0 AND phase IN ('reserved','starting','running','collecting');
CREATE INDEX discussion_execution_history ON discussion_executions(discussion_id, created_at DESC, id);
CREATE INDEX pending_issue_actions ON issue_action_requests(status, created_at, id);
CREATE UNIQUE INDEX client_access_hash ON client_connections(access_hash) WHERE access_hash IS NOT NULL AND access_hash != '';
CREATE UNIQUE INDEX client_refresh_hash ON client_connections(refresh_hash) WHERE refresh_hash IS NOT NULL AND refresh_hash != '';
CREATE INDEX jobs_pending_result ON jobs(id) WHERE outcome IS NULL;
CREATE INDEX jobs_awaiting_cleanup ON jobs(id) WHERE cleanup=0;
CREATE INDEX jobs_unit_history ON jobs(unit_id,created_at DESC,id DESC);
CREATE INDEX membership_issue_history ON unit_issues(issue_id,joined_at DESC,id DESC);
CREATE INDEX issues_state_page ON issues(github_state,repository_id,github_number);
CREATE INDEX candidate_effects_candidate ON candidate_effects(candidate_id,effect_id);
CREATE UNIQUE INDEX one_verdict_effect_per_job ON candidate_effects(job_id) WHERE job_id IS NOT NULL;
CREATE INDEX candidate_effect_page ON external_effects(created_at,id)
WHERE status IN ('pending','running','uncertain');
CREATE INDEX required_investigations ON attempt_failures(unit_id,position) WHERE recovery_required=1;
CREATE INDEX dependency_current ON dependency_checks(member_id,requirements_revision_id,generation DESC);
CREATE INDEX one_pending_operator_action_per_issue ON issue_action_requests(issue_id) WHERE status='pending';
CREATE INDEX operator_action_effect_pending ON operator_action_effects(status,created_at,request_id);
CREATE INDEX live_operator_flows ON operator_flows(expires_at) WHERE consumed_at IS NULL;
CREATE INDEX live_operator_sessions ON operator_sessions(expires_at) WHERE revoked_at IS NULL;
CREATE INDEX live_client_pairings ON client_pairings(expires_at);
CREATE INDEX live_client_connections ON client_connections(expires_at);
CREATE INDEX client_operation_history ON client_operations(connection_id,created_at DESC,id);
CREATE UNIQUE INDEX discussion_issue_identity ON discussions(issue_id);
CREATE INDEX issue_related_pulls ON inventory_pull_issue_links(repository_id,issue_number,pull_number);
CREATE INDEX operator_audit_request_kind
ON operator_audit(request_id, json_extract(details_json,'$.kind'), sequence DESC);
CREATE INDEX repository_notes ON repository_note_revisions(repository_id,note_id,revision DESC);
CREATE INDEX triage_repository_page ON triage_proposals(repository_id,created_at DESC,id);
CREATE UNIQUE INDEX one_repository_triage_run ON repository_triage_runs(repository_id)
WHERE status IN('queued','running','captured');
CREATE INDEX prerequisite_order_current ON prerequisite_orders(issue_id,requirements_revision_id,generation DESC);
CREATE INDEX environment_member_current ON unit_environment(member_id,revision DESC);
CREATE INDEX auxiliary_logs_expiry ON auxiliary_logs(expires_at) WHERE expired_at IS NULL;
CREATE INDEX unit_reservation_events ON events(unit_id,sequence DESC,job_id)
WHERE kind='attempt_reserved';
CREATE INDEX task_members_scope ON delivery_task_members(member_id,task_id);
CREATE INDEX task_dependents ON delivery_task_dependencies(prerequisite_task_id,task_id);
CREATE INDEX task_executions ON job_tasks(task_id,job_id);
CREATE INDEX outputs_task_current ON part_outputs(task_id,accepted_at DESC,id DESC);
CREATE INDEX outputs_member_current ON part_outputs(member_id,accepted_at DESC,id DESC);
CREATE UNIQUE INDEX one_live_unit_job ON jobs(unit_id) WHERE member_id IS NULL AND execution_kind!='task'
 AND fenced=0 AND phase IN ('reserved','starting','running','collecting');
CREATE UNIQUE INDEX one_current_gate_code ON unit_gates(unit_id,code,subject_key) WHERE resolved_at IS NULL;
CREATE INDEX unresolved_gates ON unit_gates(unit_id,resolved_at,code);
CREATE INDEX task_job_gates ON unit_gates(job_id,code) WHERE resolved_at IS NULL;
CREATE INDEX forge_problem_jobs_gate ON forge_problem_jobs(gate_id,job_id);
CREATE INDEX next_actions_due ON next_actions(due_at,unit_id);
CREATE INDEX recovery_assignment_history ON recovery_assignments(failed_job_id,created_at);
CREATE UNIQUE INDEX auxiliary_wait_owner ON auxiliary_execution_waits(domain,owner_id) WHERE resolved_at IS NULL;
CREATE INDEX auxiliary_provider_cooldown ON auxiliary_execution_waits(
 json_extract(details_json,'$.account_ref'),json_extract(runner_config_json,'$.provider'),json_extract(details_json,'$.retry_at'))
 WHERE kind='provider_error';
CREATE INDEX worker_claim_identity ON worker_claim_attempts(claim_key,created_at);
CREATE INDEX worker_claim_pending ON worker_claim_obligations(gate_id,released_at);
CREATE INDEX publication_pending ON publication_intents(status,updated_at,id);
CREATE UNIQUE INDEX mirror_request_terminal ON mirror_receipts(request_id) WHERE request_id IS NOT NULL;
CREATE INDEX mirror_job_receipts ON mirror_receipts(job_id,created_at);
CREATE INDEX mirror_session_receipts ON mirror_receipts(session_id,created_at);
CREATE INDEX unit_completion_events ON events(unit_id,sequence DESC,at)
WHERE kind IN ('merged','closed','dissolved');
CREATE VIEW job_delegations AS SELECT j.job_id,j.plan_id,t.task_key AS delegation_key,j.worktree_key
 FROM job_tasks j JOIN delivery_tasks t ON t.id=j.task_id;
CREATE VIEW issue_selection_facts AS
WITH latest AS (
 SELECT i.id AS issue_id,(SELECT m.id FROM unit_issues m WHERE m.issue_id=i.id
   ORDER BY (m.left_at IS NULL) DESC,m.joined_at DESC,m.id DESC LIMIT 1) AS member_id FROM issues i
), facts AS (
 SELECT i.id,i.repository_id,i.github_number,i.github_state,i.state_reason,i.reopened_version,
   m.id AS member_id,m.left_at,m.completion_issue_version,m.requirements_revision_id,
   u.id AS unit_id,u.state AS unit_state,u.current_candidate_id,u.membership_generation,
   CASE WHEN m.left_at IS NULL AND u.state NOT IN ('merged','closed','dissolved') THEN 1 ELSE 0 END AS current,
   r.enabled,ir.requirements_hash AS current_requirements,pinned.requirements_hash AS pinned_requirements
 FROM issues i JOIN repositories r ON r.id=i.repository_id JOIN latest l ON l.issue_id=i.id
 JOIN issue_revisions ir ON ir.id=i.current_revision_id LEFT JOIN unit_issues m ON m.id=l.member_id
 LEFT JOIN units u ON u.id=m.unit_id LEFT JOIN issue_revisions pinned ON pinned.id=m.requirements_revision_id
), classified AS (
 SELECT f.*,COALESCE((SELECT b.code FROM (
 SELECT f.id AS issue_id,g.code,g.created_at,g.id AS gate_id FROM unit_gates g
 WHERE g.unit_id=f.unit_id AND f.current=1 AND g.resolved_at IS NULL AND (g.candidate_id IS NULL OR g.candidate_id=f.current_candidate_id)
 AND (g.job_id IS NULL OR EXISTS(SELECT 1 FROM job_members scope WHERE scope.job_id=g.job_id AND scope.member_id=f.member_id))
 AND (g.member_id IS NULL OR g.member_id=f.member_id OR f.unit_state NOT IN ('queued','building'))
 UNION ALL SELECT f.id,'repository_setup',0,'derived:repository' WHERE f.current=1 AND f.enabled=0
 UNION ALL SELECT f.id,'membership_decision',0,'derived:requirements' WHERE f.current=1 AND f.current_requirements!=f.pinned_requirements
 UNION ALL SELECT f.id,'external_change',0,'derived:closed' WHERE f.current=1 AND f.github_state='closed'
 UNION ALL SELECT f.id,'dependency',0,'derived:dependency' WHERE f.current=1 AND (
   (EXISTS(SELECT 1 FROM issue_dependencies d WHERE d.issue_id=f.id AND d.requirements_revision_id=f.requirements_revision_id)
    AND NOT COALESCE((SELECT clear FROM dependency_checks c WHERE c.member_id=f.member_id AND c.requirements_revision_id=f.requirements_revision_id ORDER BY generation DESC LIMIT 1),0))
   OR EXISTS(SELECT 1 FROM member_dependencies d WHERE d.unit_id=f.unit_id AND d.member_id=f.member_id AND d.membership_generation=f.membership_generation))
) b JOIN gate_codes c ON c.code=b.code
 ORDER BY c.priority,c.rowid,b.created_at,b.gate_id LIMIT 1),CASE WHEN f.current=0 AND f.github_state='open' THEN d.reason_code END) AS indexed_reason_code,
 CASE WHEN f.github_state='closed' AND f.state_reason='completed' THEN 'done'
      WHEN f.github_state='closed' AND f.state_reason IN ('not_planned','duplicate') THEN 'dropped'
      WHEN f.github_state='closed' THEN 'blocked'
      WHEN f.unit_state='merged' AND ((f.completion_issue_version IS NOT NULL AND f.reopened_version<=f.completion_issue_version)
        OR (f.completion_issue_version IS NULL AND f.reopened_version=0 AND COALESCE(f.state_reason,'')!='reopened')) THEN 'done'
      WHEN f.current=0 THEN 'untracked' ELSE 'current' END AS issue_state,
 EXISTS(SELECT 1 FROM jobs j WHERE j.unit_id=f.unit_id AND j.cleanup=0 AND j.fenced=0 AND j.outcome IS NULL
 AND EXISTS(SELECT 1 FROM job_members scope WHERE scope.job_id=j.id AND scope.member_id=f.member_id)) AS active
 FROM facts f
 LEFT JOIN issues observed ON observed.id=f.id
 LEFT JOIN issue_discovery_observations d ON d.issue_id=f.id AND d.revision_id=observed.current_revision_id
)
SELECT *,CASE WHEN issue_state='done' THEN 'completed' WHEN issue_state='dropped' THEN 'dropped'
 WHEN indexed_reason_code IN ('requirements_stale','remote_timestamp_invalid','admission_conflict') THEN 'attention'
 WHEN issue_state='untracked' OR indexed_reason_code IN ('operator_pause','operator_backlog') THEN 'backlog'
 WHEN indexed_reason_code IN ('dependency','label_hold','ownership_conflict') THEN 'blocked'
 WHEN indexed_reason_code IN ('forge_problem','budget_exhausted','approach_required','decision_required','repository_setup','environment','legacy_reconciliation',
   'membership_decision','external_change','verification_human','approval_required','observation_unavailable')
   OR (github_state='closed' AND state_reason IS NULL) THEN 'attention'
 WHEN indexed_reason_code IN ('recovery_pending','cleanup_pending','result_pending') THEN 'working'
 WHEN indexed_reason_code IS NOT NULL THEN 'waiting' WHEN active THEN 'working'
 WHEN EXISTS(SELECT 1 FROM next_actions n JOIN units u ON u.id=n.unit_id WHERE n.unit_id=classified.unit_id
 AND n.unit_version=u.version AND n.fencing_generation=u.fencing_generation AND n.membership_generation=u.membership_generation
 AND (n.candidate_id IS NULL OR n.candidate_id=u.current_candidate_id) AND (n.member_id IS NULL OR n.member_id=classified.member_id)
 AND (n.job_id IS NULL OR EXISTS(SELECT 1 FROM jobs j JOIN attempts assignment ON assignment.id=j.attempt_id JOIN units assigned_unit ON assigned_unit.id=j.unit_id JOIN job_members s ON s.job_id=j.id
 WHERE assignment.assignment_generation=assigned_unit.fencing_generation AND assignment.membership_generation=assigned_unit.membership_generation AND assignment.budget_generation=assigned_unit.budget_generation AND (j.candidate_id IS NULL OR j.candidate_id=assigned_unit.current_candidate_id) AND j.id=n.job_id AND j.outcome IS NULL AND (j.fenced=0 OR EXISTS(SELECT 1 FROM job_execution_state capture
 WHERE capture.job_id=j.id AND capture.capture_authorized=1 AND capture.result_revoked=0))
 AND NOT EXISTS(SELECT 1 FROM job_execution_state revoked WHERE revoked.job_id=j.id AND revoked.result_revoked=1)
 AND (n.reason NOT IN ('cleanup','cleanup_pending') OR j.cleanup=0) AND s.member_id=classified.member_id))) THEN 'queued'
 ELSE 'waiting' END AS indexed_status
FROM classified;
CREATE TRIGGER unit_states_no_insert BEFORE INSERT ON unit_states BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_states_no_update BEFORE UPDATE ON unit_states BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_states_no_delete BEFORE DELETE ON unit_states BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_transitions_no_insert BEFORE INSERT ON unit_transitions BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_transitions_no_update BEFORE UPDATE ON unit_transitions BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_transitions_no_delete BEFORE DELETE ON unit_transitions BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER close_codes_no_update BEFORE UPDATE ON close_codes BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER close_codes_no_delete BEFORE DELETE ON close_codes BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER job_roles_no_insert BEFORE INSERT ON job_roles BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER job_roles_no_update BEFORE UPDATE ON job_roles BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER job_roles_no_delete BEFORE DELETE ON job_roles BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER job_phases_no_insert BEFORE INSERT ON job_phases BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER job_phases_no_update BEFORE UPDATE ON job_phases BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER job_phases_no_delete BEFORE DELETE ON job_phases BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER part_states_no_insert BEFORE INSERT ON part_states BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER part_states_no_update BEFORE UPDATE ON part_states BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER part_states_no_delete BEFORE DELETE ON part_states BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER exit_reasons_no_insert BEFORE INSERT ON exit_reasons BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER exit_reasons_no_update BEFORE UPDATE ON exit_reasons BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER exit_reasons_no_delete BEFORE DELETE ON exit_reasons BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER event_kinds_no_update BEFORE UPDATE ON event_kinds BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER event_kinds_no_delete BEFORE DELETE ON event_kinds BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER artifact_kinds_no_update BEFORE UPDATE ON artifact_kinds BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER artifact_kinds_no_delete BEFORE DELETE ON artifact_kinds BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER artifact_pins_unexpired BEFORE INSERT ON artifact_pins
WHEN EXISTS (SELECT 1 FROM job_artifacts WHERE job_id=NEW.job_id AND kind=NEW.kind AND sequence=NEW.sequence AND expired_at IS NOT NULL)
BEGIN SELECT RAISE(ABORT, 'an expired artifact cannot be pinned'); END;
CREATE TRIGGER artifact_pins_no_update BEFORE UPDATE ON artifact_pins
BEGIN SELECT RAISE(ABORT, 'artifact pins are immutable'); END;
CREATE TRIGGER artifact_pins_no_delete BEFORE DELETE ON artifact_pins
BEGIN SELECT RAISE(ABORT, 'artifact pins require explicit retention policy'); END;
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
CREATE TRIGGER artifacts_expire_only BEFORE UPDATE ON job_artifacts
WHEN OLD.content IS NULL OR NEW.content IS NOT NULL OR NEW.expired_at IS NULL
    OR NEW.job_id IS NOT OLD.job_id OR NEW.kind IS NOT OLD.kind OR NEW.sequence IS NOT OLD.sequence
    OR NEW.media_type IS NOT OLD.media_type OR NEW.digest IS NOT OLD.digest
    OR NEW.size IS NOT OLD.size OR NEW.codec IS NOT OLD.codec OR NEW.raw_size IS NOT OLD.raw_size
    OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
    OR (SELECT retention FROM artifact_kinds WHERE code=OLD.kind) != 'raw'
    OR NOT EXISTS (SELECT 1 FROM jobs WHERE id=OLD.job_id AND cleanup=1)
    OR EXISTS (SELECT 1 FROM artifact_pins WHERE job_id=OLD.job_id AND kind=OLD.kind AND sequence=OLD.sequence)
    OR EXISTS (SELECT 1 FROM jobs j JOIN units u ON u.id=j.unit_id
               WHERE j.id=OLD.job_id AND u.state NOT IN ('merged','closed','dissolved') AND u.current_candidate_id IS NOT NULL AND
                 (j.candidate_id=u.current_candidate_id OR EXISTS (
                    SELECT 1 FROM candidate_evidence e WHERE e.attempt_id=j.attempt_id AND e.candidate_id=u.current_candidate_id)))
    OR EXISTS (SELECT 1 FROM jobs j JOIN unit_gates g ON g.unit_id=j.unit_id
               WHERE j.id=OLD.job_id AND g.resolved_at IS NULL AND g.code IN ('recovery_pending','cleanup_pending'))
BEGIN SELECT RAISE(ABORT, 'only unpinned completed raw artifacts may expire'); END;
CREATE TRIGGER issue_revisions_no_update BEFORE UPDATE ON issue_revisions BEGIN
    SELECT RAISE(ABORT, 'issue revisions are immutable');
END;
CREATE TRIGGER issue_revisions_no_delete BEFORE DELETE ON issue_revisions BEGIN
    SELECT RAISE(ABORT, 'issue revisions are retained');
END;
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
CREATE TRIGGER candidate_evidence_no_update BEFORE UPDATE ON candidate_evidence
BEGIN SELECT RAISE(ABORT, 'candidate evidence is immutable'); END;
CREATE TRIGGER candidate_evidence_no_delete BEFORE DELETE ON candidate_evidence
BEGIN SELECT RAISE(ABORT, 'candidate evidence is retained'); END;
CREATE TRIGGER operator_audit_no_update BEFORE UPDATE ON operator_audit
BEGIN SELECT RAISE(ABORT, 'operator audit is append-only'); END;
CREATE TRIGGER operator_audit_no_delete BEFORE DELETE ON operator_audit
BEGIN SELECT RAISE(ABORT, 'operator audit is append-only'); END;
CREATE TRIGGER discussion_artifacts_no_update BEFORE UPDATE ON discussion_artifacts
BEGIN SELECT RAISE(ABORT, 'artifacts are immutable'); END;
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
CREATE TRIGGER unit_transition_legal BEFORE UPDATE OF state ON units
WHEN NEW.state != OLD.state AND NOT EXISTS (
    SELECT 1 FROM unit_transitions WHERE from_state=OLD.state AND to_state=NEW.state)
BEGIN SELECT RAISE(ABORT, 'illegal unit transition'); END;
CREATE TRIGGER unit_identity_fixed BEFORE UPDATE ON units
WHEN NEW.id IS NOT OLD.id OR NEW.repository_id IS NOT OLD.repository_id OR NEW.base_branch IS NOT OLD.base_branch
BEGIN SELECT RAISE(ABORT, 'unit identity is immutable'); END;
CREATE TRIGGER unit_terminal_facts_fixed BEFORE UPDATE ON units
WHEN OLD.state IN ('merged','closed','dissolved') AND
    (NEW.close_code IS NOT OLD.close_code OR NEW.merge_sha IS NOT OLD.merge_sha OR NEW.current_candidate_id IS NOT OLD.current_candidate_id)
BEGIN SELECT RAISE(ABORT, 'terminal delivery facts are immutable'); END;
CREATE TRIGGER unit_terminal_no_live_members BEFORE UPDATE OF state ON units
WHEN NEW.state IN ('merged','closed','dissolved') AND EXISTS (
    SELECT 1 FROM unit_issues WHERE unit_id=NEW.id AND left_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'release memberships before ending a unit'); END;
CREATE TRIGGER unit_terminal_no_live_jobs BEFORE UPDATE OF state ON units
WHEN NEW.state IN ('merged','closed','dissolved') AND EXISTS (
    SELECT 1 FROM jobs WHERE unit_id=NEW.id AND fenced=0 AND phase IN ('reserved','starting','running','collecting'))
BEGIN SELECT RAISE(ABORT, 'fence live jobs before ending a unit'); END;
CREATE TRIGGER unit_terminal_no_open_gates BEFORE UPDATE OF state ON units
WHEN NEW.state IN ('merged','closed','dissolved') AND EXISTS (
    SELECT 1 FROM unit_gates WHERE unit_id=NEW.id AND resolved_at IS NULL)
BEGIN SELECT RAISE(ABORT, 'resolve gates before ending a unit'); END;
CREATE TRIGGER memberships_join_open_unit BEFORE INSERT ON unit_issues
WHEN NEW.left_at IS NULL AND EXISTS (SELECT 1 FROM units WHERE id=NEW.unit_id AND state IN ('merged','closed','dissolved'))
BEGIN SELECT RAISE(ABORT, 'a terminal unit cannot gain members'); END;
CREATE TRIGGER memberships_history_fixed BEFORE UPDATE ON unit_issues
WHEN OLD.left_at IS NOT NULL OR NEW.id IS NOT OLD.id OR NEW.unit_id IS NOT OLD.unit_id
    OR NEW.issue_id IS NOT OLD.issue_id OR NEW.repository_id IS NOT OLD.repository_id
    OR NEW.base_branch IS NOT OLD.base_branch OR NEW.joined_at IS NOT OLD.joined_at
BEGIN SELECT RAISE(ABORT, 'membership history is immutable'); END;
CREATE TRIGGER memberships_no_delete BEFORE DELETE ON unit_issues
BEGIN SELECT RAISE(ABORT, 'membership history is retained'); END;
CREATE TRIGGER jobs_forward_only BEFORE UPDATE ON jobs
WHEN (CASE NEW.phase WHEN 'reserved' THEN 0 WHEN 'starting' THEN 1 WHEN 'running' THEN 2 WHEN 'collecting' THEN 3 WHEN 'finished' THEN 4 ELSE 5 END)
   < (CASE OLD.phase WHEN 'reserved' THEN 0 WHEN 'starting' THEN 1 WHEN 'running' THEN 2 WHEN 'collecting' THEN 3 WHEN 'finished' THEN 4 ELSE 5 END)
    OR (OLD.outcome IS NOT NULL AND NEW.outcome IS NOT OLD.outcome)
BEGIN SELECT RAISE(ABORT, 'job execution facts only move forward'); END;
CREATE TRIGGER jobs_no_delete BEFORE DELETE ON jobs
BEGIN SELECT RAISE(ABORT, 'job history is retained'); END;
CREATE TRIGGER issues_identity_fixed BEFORE UPDATE ON issues
WHEN NEW.id IS NOT OLD.id OR NEW.repository_id IS NOT OLD.repository_id OR NEW.github_number IS NOT OLD.github_number
BEGIN SELECT RAISE(ABORT, 'issue identity is immutable'); END;
CREATE TRIGGER repository_observations_no_update BEFORE UPDATE ON repository_observations
BEGIN SELECT RAISE(ABORT, 'repository observations are immutable history'); END;
CREATE TRIGGER jobs_open_unit BEFORE INSERT ON jobs
WHEN EXISTS (SELECT 1 FROM units WHERE id=NEW.unit_id AND state IN ('merged','closed','dissolved'))
AND NOT (NEW.phase='cleaned' AND NEW.cleanup=1 AND NEW.fenced=1
    AND EXISTS (SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'))
BEGIN SELECT RAISE(ABORT,'a terminal unit cannot start a job'); END;
CREATE TRIGGER auxiliary_capacity BEFORE INSERT ON auxiliary_leases
WHEN EXISTS (SELECT 1 FROM job_leases WHERE slot=NEW.slot OR execution_fence=NEW.execution_fence)
  OR EXISTS (SELECT 1 FROM discussion_leases WHERE slot=NEW.slot OR execution_fence=NEW.execution_fence)
BEGIN SELECT RAISE(ABORT,'capacity is already leased'); END;
CREATE TRIGGER pipeline_auxiliary_capacity BEFORE INSERT ON job_leases
WHEN EXISTS (SELECT 1 FROM auxiliary_leases WHERE slot=NEW.slot OR execution_fence=NEW.execution_fence)
BEGIN SELECT RAISE(ABORT,'capacity is leased by an auxiliary execution'); END;
CREATE TRIGGER discussion_auxiliary_capacity BEFORE INSERT ON discussion_leases
WHEN EXISTS (SELECT 1 FROM auxiliary_leases WHERE slot=NEW.slot OR execution_fence=NEW.execution_fence)
BEGIN SELECT RAISE(ABORT,'capacity is leased by an auxiliary execution'); END;
CREATE TRIGGER auxiliary_lease_no_reassign BEFORE UPDATE ON auxiliary_leases
BEGIN SELECT RAISE(ABORT,'leases cannot be reassigned'); END;
CREATE TRIGGER auxiliary_artifacts_no_update BEFORE UPDATE ON auxiliary_artifacts
BEGIN SELECT RAISE(ABORT,'execution artifacts are immutable'); END;
CREATE TRIGGER auxiliary_artifacts_no_delete BEFORE DELETE ON auxiliary_artifacts
BEGIN SELECT RAISE(ABORT,'execution artifact retention requires explicit policy'); END;
CREATE TRIGGER job_launches_no_update BEFORE UPDATE ON job_launches
BEGIN SELECT RAISE(ABORT,'launch authority is immutable'); END;
CREATE TRIGGER job_launches_no_delete BEFORE DELETE ON job_launches
BEGIN SELECT RAISE(ABORT,'launch authority is retained'); END;
CREATE TRIGGER job_launches_own_lease BEFORE INSERT ON job_launches
WHEN NOT EXISTS (SELECT 1 FROM jobs j JOIN job_leases l ON l.job_id=j.id
    WHERE j.id=NEW.job_id AND j.phase='reserved' AND j.fenced=0
      AND l.slot=NEW.slot AND l.execution_fence=NEW.execution_fence)
BEGIN SELECT RAISE(ABORT,'launch authority requires the exact reserved lease'); END;
CREATE TRIGGER execution_identity_fixed BEFORE UPDATE ON job_execution_state
WHEN NEW.job_id IS NOT OLD.job_id
    OR NEW.result_revoked < OLD.result_revoked
    OR (OLD.container_id IS NOT NULL AND NEW.container_id IS NOT OLD.container_id)
    OR (OLD.captured_at IS NOT NULL AND (NEW.captured_at IS NOT OLD.captured_at
        OR NEW.capture_authorized IS NOT OLD.capture_authorized))
BEGIN SELECT RAISE(ABORT,'execution identity and capture authority are immutable'); END;
CREATE TRIGGER execution_state_no_delete BEFORE DELETE ON job_execution_state
BEGIN SELECT RAISE(ABORT,'execution observations are retained'); END;
CREATE TRIGGER artifacts_preserve_pending_result BEFORE UPDATE ON job_artifacts
WHEN NEW.content IS NULL AND EXISTS (
    SELECT 1 FROM jobs j JOIN unit_gates g ON g.unit_id=j.unit_id
    WHERE j.id=OLD.job_id AND g.code='result_pending' AND g.resolved_at IS NULL)
BEGIN SELECT RAISE(ABORT,'pending result evidence is retained'); END;
CREATE TRIGGER candidate_effects_no_update BEFORE UPDATE ON candidate_effects
BEGIN SELECT RAISE(ABORT,'effect authority is immutable'); END;
CREATE TRIGGER candidate_effects_no_delete BEFORE DELETE ON candidate_effects
BEGIN SELECT RAISE(ABORT,'effect authority is retained'); END;
CREATE TRIGGER candidate_effect_intent_fixed BEFORE UPDATE ON external_effects
WHEN EXISTS (SELECT 1 FROM candidate_effects c WHERE c.effect_id=OLD.id)
 AND (NEW.id IS NOT OLD.id OR NEW.request_id IS NOT OLD.request_id
      OR NEW.unit_id IS NOT OLD.unit_id OR NEW.kind IS NOT OLD.kind
      OR NEW.expected_version IS NOT OLD.expected_version
      OR NEW.payload_json IS NOT OLD.payload_json OR NEW.created_at IS NOT OLD.created_at)
BEGIN SELECT RAISE(ABORT,'candidate effect intent is immutable'); END;
CREATE TRIGGER event_kinds_no_insert BEFORE INSERT ON event_kinds
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
CREATE TRIGGER dependency_checks_no_update BEFORE UPDATE ON dependency_checks
BEGIN SELECT RAISE(ABORT,'dependency observations are immutable'); END;
CREATE TRIGGER dependency_checks_no_delete BEFORE DELETE ON dependency_checks
BEGIN SELECT RAISE(ABORT,'dependency observations are retained'); END;
CREATE TRIGGER environment_label_effects_no_update BEFORE UPDATE ON environment_label_effects
BEGIN SELECT RAISE(ABORT,'environment label intent is immutable'); END;
CREATE TRIGGER environment_label_effects_no_delete BEFORE DELETE ON environment_label_effects
BEGIN SELECT RAISE(ABORT,'environment label intent is retained'); END;
CREATE TRIGGER client_operation_request_fixed BEFORE UPDATE ON client_operations
WHEN NEW.id IS NOT OLD.id OR NEW.request_id IS NOT OLD.request_id
 OR NEW.connection_id IS NOT OLD.connection_id OR NEW.kind IS NOT OLD.kind
 OR NEW.payload_json IS NOT OLD.payload_json OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'admitted client operation identity is immutable'); END;
CREATE TRIGGER client_grant_scope_fixed BEFORE UPDATE ON client_connections
WHEN NEW.id IS NOT OLD.id OR NEW.operator_id IS NOT OLD.operator_id
 OR NEW.authorization_epoch IS NOT OLD.authorization_epoch OR NEW.source_epoch IS NOT OLD.source_epoch
 OR NEW.instance_id IS NOT OLD.instance_id OR NEW.setup_generation IS NOT OLD.setup_generation
 OR NEW.scopes_json IS NOT OLD.scopes_json OR NEW.scope_json IS NOT OLD.scope_json
 OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
BEGIN SELECT RAISE(ABORT,'approved client grant authority is immutable'); END;
CREATE TRIGGER auxiliary_manifest_immutable BEFORE UPDATE ON auxiliary_runtime
WHEN NEW.manifest_json != OLD.manifest_json OR NEW.execution_id != OLD.execution_id
BEGIN SELECT RAISE(ABORT,'auxiliary launch identity is immutable'); END;
CREATE TRIGGER discussion_manifest_fixed BEFORE UPDATE ON discussion_runtime
WHEN NEW.execution_id!=OLD.execution_id OR NEW.manifest_json!=OLD.manifest_json
BEGIN SELECT RAISE(ABORT,'discussion launch identity is immutable'); END;
CREATE TRIGGER discussion_artifacts_no_delete BEFORE DELETE ON discussion_artifacts
WHEN OLD.kind!='logs' OR NOT EXISTS(SELECT 1 FROM discussion_executions WHERE id=OLD.execution_id AND cleanup=1)
BEGIN SELECT RAISE(ABORT,'discussion evidence is retained'); END;
CREATE TRIGGER issues_timestamp_insert BEFORE INSERT ON issues
WHEN NEW.remote_updated_at IS NOT NULL AND
 (NEW.remote_updated_at NOT GLOB '????-??-??T??:??:??.??????+00:00' OR julianday(NEW.remote_updated_at) IS NULL)
BEGIN SELECT RAISE(ABORT,'GitHub timestamp must use canonical UTC microseconds'); END;
CREATE TRIGGER issues_observed_in_order BEFORE UPDATE ON issues
WHEN (NEW.remote_updated_at IS NOT NULL AND
       (NEW.remote_updated_at NOT GLOB '????-??-??T??:??:??.??????+00:00' OR julianday(NEW.remote_updated_at) IS NULL))
    OR (OLD.remote_updated_at IS NOT NULL AND NEW.remote_updated_at IS NULL)
    OR NEW.remote_updated_at < OLD.remote_updated_at
    OR (NEW.remote_updated_at IS OLD.remote_updated_at AND NEW.collection_generation < OLD.collection_generation)
BEGIN SELECT RAISE(ABORT,'an older or noncanonical observation cannot replace a newer one'); END;
CREATE TRIGGER pull_requests_timestamp_insert BEFORE INSERT ON pull_requests
WHEN NEW.remote_updated_at NOT GLOB '????-??-??T??:??:??.??????+00:00' OR julianday(NEW.remote_updated_at) IS NULL
BEGIN SELECT RAISE(ABORT,'GitHub timestamp must use canonical UTC microseconds'); END;
CREATE TRIGGER pull_requests_timestamp_update BEFORE UPDATE OF remote_updated_at ON pull_requests
WHEN NEW.remote_updated_at NOT GLOB '????-??-??T??:??:??.??????+00:00' OR julianday(NEW.remote_updated_at) IS NULL
BEGIN SELECT RAISE(ABORT,'GitHub timestamp must use canonical UTC microseconds'); END;
CREATE TRIGGER job_members_match BEFORE INSERT ON job_members
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN unit_issues m ON m.unit_id=j.unit_id
 JOIN issue_revisions r ON r.issue_id=m.issue_id
 WHERE j.id=NEW.job_id AND m.id=NEW.member_id AND r.id=NEW.requirements_revision_id)
BEGIN SELECT RAISE(ABORT,'job scope must match its unit and issue revision'); END;
CREATE TRIGGER builder_assignment_scope BEFORE INSERT ON builder_assignments
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN unit_builders b ON b.unit_id=j.unit_id
 WHERE j.id=NEW.job_id AND b.id=NEW.builder_id AND j.role='build' AND j.member_id IS NULL AND j.candidate_id IS NULL)
BEGIN SELECT RAISE(ABORT,'builder assignment must own its whole delivery'); END;
CREATE TRIGGER artifact_kinds_no_insert BEFORE INSERT ON artifact_kinds
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
CREATE TRIGGER job_members_no_update BEFORE UPDATE ON job_members
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER job_members_no_delete BEFORE DELETE ON job_members
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER unit_builders_no_update BEFORE UPDATE ON unit_builders
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER unit_builders_no_delete BEFORE DELETE ON unit_builders
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER builder_assignments_no_update BEFORE UPDATE ON builder_assignments
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER builder_assignments_no_delete BEFORE DELETE ON builder_assignments
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER delegation_plans_no_update BEFORE UPDATE ON delegation_plans
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER delegation_plans_no_delete BEFORE DELETE ON delegation_plans
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER unit_budget_lineage_no_update BEFORE UPDATE ON unit_budget_lineage
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER unit_budget_lineage_no_delete BEFORE DELETE ON unit_budget_lineage
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER composition_sources_no_update BEFORE UPDATE ON unit_composition_sources
BEGIN SELECT RAISE(ABORT,'composition provenance is immutable'); END;
CREATE TRIGGER composition_sources_no_delete BEFORE DELETE ON unit_composition_sources
BEGIN SELECT RAISE(ABORT,'composition provenance is retained'); END;
CREATE TRIGGER publication_targets_no_update BEFORE UPDATE ON unit_publication_targets
BEGIN SELECT RAISE(ABORT,'approved publication target is immutable'); END;
CREATE TRIGGER publication_targets_no_delete BEFORE DELETE ON unit_publication_targets
BEGIN SELECT RAISE(ABORT,'approved publication target is retained'); END;
CREATE TRIGGER triage_effect_authority_fixed BEFORE UPDATE ON triage_effect_authority
BEGIN SELECT RAISE(ABORT,'triage effect authority is immutable'); END;
CREATE TRIGGER repository_note_history_fixed BEFORE UPDATE ON repository_note_revisions
BEGIN SELECT RAISE(ABORT,'repository note history is immutable'); END;
CREATE TRIGGER repository_note_history_retained BEFORE DELETE ON repository_note_revisions
BEGIN SELECT RAISE(ABORT,'repository note history is retained'); END;
CREATE TRIGGER auxiliary_release_verified BEFORE DELETE ON auxiliary_leases
WHEN NOT EXISTS(SELECT 1 FROM auxiliary_executions WHERE id=OLD.execution_id AND fenced=1 AND cleanup=1)
BEGIN SELECT RAISE(ABORT,'cleanup must be verified before release'); END;
CREATE TRIGGER auxiliary_lease_requires_live_execution BEFORE INSERT ON auxiliary_leases
WHEN NOT EXISTS(SELECT 1 FROM auxiliary_executions WHERE id=NEW.execution_id AND cleanup=0 AND fenced=0)
BEGIN SELECT RAISE(ABORT,'lease requires an unfenced execution awaiting cleanup'); END;
CREATE TRIGGER auxiliary_cleanup_monotonic BEFORE UPDATE ON auxiliary_executions
WHEN NEW.fenced<OLD.fenced OR NEW.cleanup<OLD.cleanup
BEGIN SELECT RAISE(ABORT,'execution fencing and cleanup cannot be reversed'); END;
CREATE TRIGGER prerequisite_orders_no_update BEFORE UPDATE ON prerequisite_orders
BEGIN SELECT RAISE(ABORT,'prerequisite orders are immutable'); END;
CREATE TRIGGER prerequisite_orders_no_delete BEFORE DELETE ON prerequisite_orders
BEGIN SELECT RAISE(ABORT,'prerequisite orders are retained'); END;
CREATE TRIGGER prerequisite_identity_no_update BEFORE UPDATE ON prerequisite_identities
BEGIN SELECT RAISE(ABORT,'prerequisite identity cannot be rebound'); END;
CREATE TRIGGER prerequisite_identity_no_delete BEFORE DELETE ON prerequisite_identities
BEGIN SELECT RAISE(ABORT,'prerequisite identities are retained'); END;
CREATE TRIGGER execution_observation_consistent BEFORE UPDATE ON job_execution_state
WHEN (NEW.observed_at IS NULL) != (NEW.observed_status IS NULL)
BEGIN SELECT RAISE(ABORT,'execution observations require both time and status'); END;
CREATE TRIGGER auxiliary_logs_expiry_only BEFORE UPDATE ON auxiliary_logs
WHEN NEW.execution_id!=OLD.execution_id OR NEW.digest!=OLD.digest
 OR NEW.created_at!=OLD.created_at OR NEW.expires_at!=OLD.expires_at
 OR OLD.expired_at IS NOT NULL OR NEW.content IS NOT NULL OR NEW.expired_at IS NULL
 OR NEW.expired_at<OLD.expires_at
 OR NOT EXISTS(SELECT 1 FROM auxiliary_executions e WHERE e.id=OLD.execution_id AND e.cleanup=1)
 OR EXISTS(SELECT 1 FROM auxiliary_leases l WHERE l.execution_id=OLD.execution_id)
BEGIN SELECT RAISE(ABORT,'auxiliary logs may only expire after retention and cleanup'); END;
CREATE TRIGGER auxiliary_logs_no_delete BEFORE DELETE ON auxiliary_logs
BEGIN SELECT RAISE(ABORT,'auxiliary log provenance must be retained'); END;
CREATE TRIGGER resource_receipts_no_update BEFORE UPDATE ON job_resource_receipts
BEGIN SELECT RAISE(ABORT,'resource capture is immutable'); END;
CREATE TRIGGER resource_receipts_no_delete BEFORE DELETE ON job_resource_receipts
BEGIN SELECT RAISE(ABORT,'resource capture is immutable'); END;
CREATE TRIGGER jobs_pin_members AFTER INSERT ON jobs BEGIN
 INSERT INTO job_members(job_id,member_id,requirements_revision_id)
 SELECT NEW.id,m.id,COALESCE(json_extract(a.input_manifest_json,'$.requirements_revision_id'),
 (SELECT json_extract(value,'$.requirements_revision_id') FROM json_each(a.input_manifest_json,'$.members')
 WHERE json_extract(value,'$.member_id')=m.id),m.requirements_revision_id)
 FROM unit_issues m JOIN attempts a ON a.id=NEW.attempt_id
 WHERE NEW.role!='recovery' AND m.unit_id=NEW.unit_id AND m.left_at IS NULL AND (NEW.member_id IS NULL OR NEW.member_id=m.id)
 AND (json_extract(a.input_manifest_json,'$.manifest_version') IS NOT 2 OR EXISTS(
 SELECT 1 FROM json_each(a.input_manifest_json,'$.members') pin WHERE json_extract(pin.value,'$.member_id')=m.id));
END;
CREATE TRIGGER gates_scope BEFORE INSERT ON unit_gates
WHEN NOT EXISTS(SELECT 1 FROM gate_codes WHERE code=NEW.code AND (
 (NEW.job_id IS NOT NULL AND job_scope=1 AND EXISTS(SELECT 1 FROM jobs j WHERE j.id=NEW.job_id AND j.unit_id=NEW.unit_id)) OR
 (NEW.job_id IS NULL AND ((NEW.member_id IS NOT NULL AND member_scope=1) OR
 (NEW.member_id IS NULL AND NEW.candidate_id IS NOT NULL AND candidate_scope=1) OR
 (NEW.member_id IS NULL AND NEW.candidate_id IS NULL AND unit_scope=1)))))
BEGIN SELECT RAISE(ABORT,'gate scope does not fit its code'); END;
CREATE TRIGGER gates_open_unit BEFORE INSERT ON unit_gates
WHEN EXISTS (SELECT 1 FROM units WHERE id=NEW.unit_id AND state IN ('merged','closed','dissolved'))
BEGIN SELECT RAISE(ABORT, 'a terminal unit cannot gain gates'); END;
CREATE TRIGGER gates_no_delete BEFORE DELETE ON unit_gates
BEGIN SELECT RAISE(ABORT, 'gate history is retained'); END;
CREATE TRIGGER gates_resolve_only BEFORE UPDATE ON unit_gates
WHEN OLD.resolved_at IS NOT NULL OR NEW.resolved_at IS NULL
    OR NEW.id IS NOT OLD.id OR NEW.unit_id IS NOT OLD.unit_id OR NEW.code IS NOT OLD.code
    OR NEW.job_id IS NOT OLD.job_id OR NEW.subject_key IS NOT OLD.subject_key OR NEW.candidate_id IS NOT OLD.candidate_id
    OR NEW.member_id IS NOT OLD.member_id OR NEW.details_json IS NOT OLD.details_json
    OR NEW.created_at IS NOT OLD.created_at
    OR (NEW.resolution != 'superseded' AND NEW.resolution IS NOT (SELECT release FROM gate_codes WHERE code=OLD.code))
BEGIN SELECT RAISE(ABORT, 'a gate can only resolve once using its release rule'); END;
CREATE TRIGGER delivery_tasks_no_update BEFORE UPDATE ON delivery_tasks BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER delivery_tasks_no_delete BEFORE DELETE ON delivery_tasks BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER delivery_task_members_no_update BEFORE UPDATE ON delivery_task_members BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER delivery_task_members_no_delete BEFORE DELETE ON delivery_task_members BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER delivery_task_dependencies_no_update BEFORE UPDATE ON delivery_task_dependencies BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER delivery_task_dependencies_no_delete BEFORE DELETE ON delivery_task_dependencies BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER delegation_plan_tasks_no_update BEFORE UPDATE ON delegation_plan_tasks BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER delegation_plan_tasks_no_delete BEFORE DELETE ON delegation_plan_tasks BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER job_tasks_no_update BEFORE UPDATE ON job_tasks BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER job_tasks_no_delete BEFORE DELETE ON job_tasks BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER part_outputs_no_update BEFORE UPDATE ON part_outputs BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER part_outputs_no_delete BEFORE DELETE ON part_outputs BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER part_output_invalidations_no_update BEFORE UPDATE ON part_output_invalidations BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER part_output_invalidations_no_delete BEFORE DELETE ON part_output_invalidations BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER task_member_scope BEFORE INSERT ON delivery_task_members
WHEN NOT EXISTS(SELECT 1 FROM delivery_tasks t JOIN unit_builders b ON b.id=t.builder_id
 JOIN unit_issues m ON m.unit_id=b.unit_id JOIN issue_revisions r ON r.issue_id=m.issue_id
 WHERE t.id=NEW.task_id AND m.id=NEW.member_id AND r.id=NEW.requirements_revision_id)
BEGIN SELECT RAISE(ABORT,'task member must match approved unit scope'); END;
CREATE TRIGGER task_job_scope BEFORE INSERT ON job_tasks
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN delivery_tasks t ON t.id=NEW.task_id
 JOIN unit_builders b ON b.id=t.builder_id JOIN delegation_plan_tasks p ON p.task_id=t.id AND p.plan_id=NEW.plan_id
 WHERE j.id=NEW.job_id AND j.unit_id=b.unit_id AND j.role='build')
 OR EXISTS(SELECT member_id,requirements_revision_id FROM delivery_task_members WHERE task_id=NEW.task_id
 EXCEPT SELECT member_id,requirements_revision_id FROM job_members WHERE job_id=NEW.job_id)
 OR EXISTS(SELECT member_id,requirements_revision_id FROM job_members WHERE job_id=NEW.job_id
 EXCEPT SELECT member_id,requirements_revision_id FROM delivery_task_members WHERE task_id=NEW.task_id)
BEGIN SELECT RAISE(ABORT,'task assignment must pin its exact member scope'); END;
CREATE TRIGGER task_output_scope BEFORE INSERT ON part_outputs
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN job_tasks t ON t.job_id=j.id
 WHERE j.attempt_id=NEW.attempt_id AND j.unit_id=NEW.unit_id AND t.task_id=NEW.task_id)
BEGIN SELECT RAISE(ABORT,'output must match its exact task attempt'); END;
CREATE TRIGGER jobs_execution_kind_immutable BEFORE UPDATE OF execution_kind ON jobs
WHEN NEW.execution_kind IS NOT OLD.execution_kind BEGIN SELECT RAISE(ABORT,'execution scope kind is immutable'); END;
CREATE TRIGGER task_dependency_owner BEFORE INSERT ON delivery_task_dependencies
WHEN NOT EXISTS(SELECT 1 FROM delivery_tasks a JOIN delivery_tasks b ON a.builder_id=b.builder_id WHERE a.id=NEW.task_id AND b.id=NEW.prerequisite_task_id)
BEGIN SELECT RAISE(ABORT,'task dependency must stay within its owner'); END;
CREATE TRIGGER plan_task_owner BEFORE INSERT ON delegation_plan_tasks
WHEN NOT EXISTS(SELECT 1 FROM delivery_tasks t JOIN delegation_plans p ON p.builder_id=t.builder_id WHERE t.id=NEW.task_id AND p.id=NEW.plan_id)
BEGIN SELECT RAISE(ABORT,'plan task must retain its owner'); END;
CREATE TRIGGER task_one_live_execution BEFORE INSERT ON job_tasks
WHEN EXISTS(SELECT 1 FROM job_tasks t JOIN jobs j ON j.id=t.job_id WHERE t.task_id=NEW.task_id AND (j.cleanup=0 OR j.outcome IS NULL))
BEGIN SELECT RAISE(ABORT,'task already owns a physical execution'); END;
CREATE TRIGGER gate_codes_no_insert BEFORE INSERT ON gate_codes BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
CREATE TRIGGER forge_problem_jobs_no_update BEFORE UPDATE ON forge_problem_jobs
 BEGIN SELECT RAISE(ABORT,'Forge problem evidence is immutable'); END;
CREATE TRIGGER forge_problem_jobs_no_delete BEFORE DELETE ON forge_problem_jobs
 BEGIN SELECT RAISE(ABORT,'Forge problem evidence is immutable'); END;
CREATE TRIGGER gate_codes_no_update BEFORE UPDATE ON gate_codes
 BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
CREATE TRIGGER schedule_new_unit AFTER INSERT ON units
BEGIN
 INSERT INTO next_actions VALUES(NEW.id,'dispatch','unit',NEW.version,NEW.fencing_generation,NEW.membership_generation,
 NULL,NULL,NULL,'admitted',NEW.created_at,NEW.created_at,NEW.created_at);
END;
CREATE TRIGGER forge_problem_reports_no_update BEFORE UPDATE ON forge_problem_reports
 BEGIN SELECT RAISE(ABORT,'Forge report drafts retain their original safe signature'); END;
CREATE TRIGGER forge_problem_reports_no_delete BEFORE DELETE ON forge_problem_reports
 BEGIN SELECT RAISE(ABORT,'Forge report drafts are durable'); END;
CREATE TRIGGER forge_problem_report_jobs_no_update BEFORE UPDATE ON forge_problem_report_jobs
 BEGIN SELECT RAISE(ABORT,'Forge report associations are immutable'); END;
CREATE TRIGGER forge_problem_report_jobs_no_delete BEFORE DELETE ON forge_problem_report_jobs
 BEGIN SELECT RAISE(ABORT,'Forge report associations are immutable'); END;
CREATE TRIGGER recovery_assignment_one_live BEFORE INSERT ON recovery_assignments
WHEN EXISTS(SELECT 1 FROM recovery_assignments r JOIN jobs j ON j.id=r.job_id
 WHERE r.failed_job_id=NEW.failed_job_id AND (j.outcome IS NULL OR j.outcome!='deferred'))
BEGIN SELECT RAISE(ABORT,'failed assignment already has an investigation'); END;
CREATE TRIGGER recovery_assignments_no_update BEFORE UPDATE ON recovery_assignments
BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END;
CREATE TRIGGER recovery_assignments_no_delete BEFORE DELETE ON recovery_assignments
BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END;
CREATE TRIGGER recovery_findings_no_update BEFORE UPDATE ON recovery_findings
BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END;
CREATE TRIGGER recovery_findings_no_delete BEFORE DELETE ON recovery_findings
BEGIN SELECT RAISE(ABORT,'recovery history is immutable'); END;
CREATE TRIGGER recovery_memory_retries_no_update BEFORE UPDATE ON recovery_memory_retries
 BEGIN SELECT RAISE(ABORT,'memory retry evidence is immutable'); END;
CREATE TRIGGER recovery_memory_retries_no_delete BEFORE DELETE ON recovery_memory_retries
 BEGIN SELECT RAISE(ABORT,'memory retry evidence is retained'); END;
CREATE TRIGGER worker_claim_attempts_no_update BEFORE UPDATE ON worker_claim_attempts
 BEGIN SELECT RAISE(ABORT,'worker claim evidence is immutable'); END;
CREATE TRIGGER worker_claim_attempts_no_delete BEFORE DELETE ON worker_claim_attempts
 BEGIN SELECT RAISE(ABORT,'worker claim evidence is retained'); END;
CREATE TRIGGER worker_claim_obligations_release_only BEFORE UPDATE ON worker_claim_obligations
 WHEN OLD.released_at IS NOT NULL OR NEW.released_at IS NULL OR NEW.release_reason IS NULL
 OR NEW.gate_id IS NOT OLD.gate_id OR NEW.job_id IS NOT OLD.job_id OR NEW.member_id IS NOT OLD.member_id
 BEGIN SELECT RAISE(ABORT,'worker claim obligations only release once'); END;
CREATE TRIGGER worker_claim_obligations_no_delete BEFORE DELETE ON worker_claim_obligations
 BEGIN SELECT RAISE(ABORT,'worker claim obligations are retained'); END;
CREATE TRIGGER publication_identity_immutable BEFORE UPDATE ON publication_intents
WHEN NEW.id!=OLD.id OR NEW.job_id!=OLD.job_id OR NEW.unit_id!=OLD.unit_id OR NEW.execution_fence!=OLD.execution_fence
 OR NEW.input_manifest_json!=OLD.input_manifest_json OR NEW.output_digest!=OLD.output_digest OR NEW.output_json!=OLD.output_json
 OR NEW.target_json!=OLD.target_json OR NEW.report_json!=OLD.report_json OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'publication identity is immutable'); END;
CREATE TRIGGER publication_step_identity_immutable BEFORE UPDATE ON publication_steps
WHEN NEW.intent_id!=OLD.intent_id OR NEW.key!=OLD.key OR NEW.request_digest!=OLD.request_digest OR NEW.created_at!=OLD.created_at
 OR OLD.status='confirmed' AND (NEW.status!='confirmed' OR NEW.receipt_json!=OLD.receipt_json)
BEGIN SELECT RAISE(ABORT,'publication step evidence is immutable'); END;
CREATE TRIGGER publication_receipt_no_update BEFORE UPDATE ON publication_receipts
BEGIN SELECT RAISE(ABORT,'publication receipt is immutable'); END;
CREATE TRIGGER publication_receipt_no_delete BEFORE DELETE ON publication_receipts
BEGIN SELECT RAISE(ABORT,'publication receipt is retained'); END;
CREATE TRIGGER publication_intent_no_delete BEFORE DELETE ON publication_intents
BEGIN SELECT RAISE(ABORT,'publication intent is retained'); END;
CREATE TRIGGER publication_step_no_delete BEFORE DELETE ON publication_steps
BEGIN SELECT RAISE(ABORT,'publication step is retained'); END;
CREATE TRIGGER mirror_sources_immutable BEFORE UPDATE ON mirror_sources
 BEGIN SELECT RAISE(ABORT,'mirror source is immutable'); END;
CREATE TRIGGER mirror_sources_retained BEFORE DELETE ON mirror_sources
 BEGIN SELECT RAISE(ABORT,'mirror source is retained'); END;
CREATE TRIGGER mirror_policies_immutable BEFORE UPDATE ON mirror_policies
 BEGIN SELECT RAISE(ABORT,'mirror input is immutable'); END;
CREATE TRIGGER mirror_policies_retained BEFORE DELETE ON mirror_policies
 BEGIN SELECT RAISE(ABORT,'mirror input is retained'); END;
CREATE TRIGGER mirror_sessions_revoke_only BEFORE UPDATE ON mirror_sessions
 WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR
 NEW.id IS NOT OLD.id OR NEW.job_id IS NOT OLD.job_id OR
 NEW.execution_fence IS NOT OLD.execution_fence OR NEW.network_generation IS NOT OLD.network_generation OR
 NEW.source_digest IS NOT OLD.source_digest OR NEW.policy_digest IS NOT OLD.policy_digest OR
 NEW.deadline IS NOT OLD.deadline OR NEW.created_at IS NOT OLD.created_at
 BEGIN SELECT RAISE(ABORT,'mirror sessions only revoke'); END;
CREATE TRIGGER mirror_sessions_retained BEFORE DELETE ON mirror_sessions
 BEGIN SELECT RAISE(ABORT,'mirror sessions are retained'); END;
CREATE TRIGGER mirror_receipts_immutable BEFORE UPDATE ON mirror_receipts
 BEGIN SELECT RAISE(ABORT,'mirror receipt is immutable'); END;
CREATE TRIGGER mirror_receipts_retained BEFORE DELETE ON mirror_receipts
 BEGIN SELECT RAISE(ABORT,'mirror receipt is retained'); END;
CREATE TRIGGER mirror_claims_immutable BEFORE UPDATE ON mirror_claims
 BEGIN SELECT RAISE(ABORT,'mirror claim is immutable'); END;
CREATE TRIGGER mirror_claims_retained BEFORE DELETE ON mirror_claims
 BEGIN SELECT RAISE(ABORT,'mirror claim is retained'); END;
CREATE TRIGGER mirror_job_inputs_immutable BEFORE UPDATE ON mirror_job_inputs
 BEGIN SELECT RAISE(ABORT,'prepared job inputs are immutable'); END;
CREATE TRIGGER mirror_job_inputs_retained BEFORE DELETE ON mirror_job_inputs
 BEGIN SELECT RAISE(ABORT,'prepared job inputs are retained'); END;
CREATE TRIGGER close_codes_no_insert BEFORE INSERT ON close_codes BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER recovery_assignment_valid BEFORE INSERT ON recovery_assignments
WHEN NOT EXISTS (SELECT 1 FROM jobs r JOIN jobs f ON f.id=NEW.failed_job_id
 JOIN attempt_failures a ON a.attempt_id=f.attempt_id
 WHERE r.id=NEW.job_id AND r.role='recovery' AND r.unit_id=f.unit_id
 AND f.role!='recovery' AND f.cleanup=1 AND f.outcome IN ('failure','rejected')
 AND a.attempt_id=NEW.failed_attempt_id AND a.recovery_required=1)
BEGIN SELECT RAISE(ABORT,'recovery must own a cleaned confirmed failure'); END;
CREATE TRIGGER gate_codes_no_delete BEFORE DELETE ON gate_codes BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
