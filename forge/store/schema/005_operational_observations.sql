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
CREATE TABLE observation_requests (
    issue_id TEXT PRIMARY KEY NOT NULL REFERENCES issues(id),
    requested_at REAL NOT NULL
) STRICT;
CREATE TABLE setup_instances (
    scope TEXT PRIMARY KEY NOT NULL CHECK (scope IN ('live','setup')),
    generation TEXT NOT NULL,
    phase TEXT NOT NULL,
    owner TEXT NOT NULL,
    account_type TEXT NOT NULL,
    settings_json TEXT NOT NULL CHECK (json_valid(settings_json) AND length(CAST(settings_json AS BLOB)) <= 65536)
) STRICT;
CREATE TABLE auxiliary_executions (
    id TEXT PRIMARY KEY NOT NULL,
    domain TEXT NOT NULL CHECK (domain IN ('setup','deployment','qualification')),
    owner_id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    phase TEXT NOT NULL REFERENCES job_phases(code),
    fenced INTEGER NOT NULL CHECK (fenced IN (0,1)),
    cleanup INTEGER NOT NULL CHECK (cleanup IN (0,1)),
    created_at REAL,
    CHECK (cleanup = 0 OR (fenced = 1 AND phase = 'cleaned')),
    UNIQUE (domain,owner_id,position)
) STRICT;
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
CREATE TRIGGER auxiliary_release_verified BEFORE DELETE ON auxiliary_leases
WHEN NOT EXISTS (SELECT 1 FROM auxiliary_executions WHERE id=OLD.execution_id AND fenced=1 AND cleanup=1)
BEGIN SELECT RAISE(ABORT,'cleanup must be verified before release'); END;
CREATE TRIGGER auxiliary_lease_no_reassign BEFORE UPDATE ON auxiliary_leases
BEGIN SELECT RAISE(ABORT,'leases cannot be reassigned'); END;
CREATE TRIGGER auxiliary_lease_requires_live_execution BEFORE INSERT ON auxiliary_leases
WHEN NOT EXISTS (SELECT 1 FROM auxiliary_executions WHERE id=NEW.execution_id AND cleanup=0 AND fenced=0)
BEGIN SELECT RAISE(ABORT,'lease requires an unfenced execution awaiting cleanup'); END;
CREATE TRIGGER auxiliary_cleanup_monotonic BEFORE UPDATE ON auxiliary_executions
WHEN NEW.fenced < OLD.fenced OR NEW.cleanup < OLD.cleanup
BEGIN SELECT RAISE(ABORT,'execution fencing and cleanup cannot be reversed'); END;
CREATE TRIGGER auxiliary_artifacts_no_update BEFORE UPDATE ON auxiliary_artifacts
BEGIN SELECT RAISE(ABORT,'execution artifacts are immutable'); END;
CREATE TRIGGER auxiliary_artifacts_no_delete BEFORE DELETE ON auxiliary_artifacts
BEGIN SELECT RAISE(ABORT,'execution artifact retention requires explicit policy'); END;
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
