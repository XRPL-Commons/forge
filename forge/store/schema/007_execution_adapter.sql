-- Immutable launch authority and bounded execution observations. No worker token
-- is persisted here. The execution fence survives release of its capacity lease.
ALTER TABLE issues ADD COLUMN reopened_version INTEGER NOT NULL DEFAULT 0 CHECK (reopened_version >= 0);
ALTER TABLE unit_issues ADD COLUMN completion_issue_version INTEGER CHECK (completion_issue_version > 0);
CREATE TABLE job_launches (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
    slot TEXT NOT NULL,
    execution_fence TEXT NOT NULL UNIQUE,
    requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
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
    result_revoked INTEGER NOT NULL DEFAULT 0 CHECK (result_revoked IN (0,1)),
    CHECK (capture_authorized = 0 OR captured_at IS NOT NULL)
) STRICT;
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
CREATE INDEX jobs_pending_result ON jobs(id) WHERE outcome IS NULL;
CREATE INDEX jobs_awaiting_cleanup ON jobs(id) WHERE cleanup=0;
CREATE INDEX jobs_unit_history ON jobs(unit_id,created_at DESC,id DESC);
CREATE INDEX membership_issue_history ON unit_issues(issue_id,joined_at DESC,id DESC);
CREATE INDEX issues_state_page ON issues(github_state,repository_id,github_number);
CREATE TRIGGER artifacts_preserve_pending_result BEFORE UPDATE ON job_artifacts
WHEN NEW.content IS NULL AND EXISTS (
    SELECT 1 FROM jobs j JOIN unit_gates g ON g.unit_id=j.unit_id
    WHERE j.id=OLD.job_id AND g.code='result_pending' AND g.resolved_at IS NULL)
BEGIN SELECT RAISE(ABORT,'pending result evidence is retained'); END;
DROP TRIGGER artifact_kinds_no_insert;
INSERT INTO artifact_kinds(code,retention) VALUES('worker_capture','durable');
CREATE TRIGGER artifact_kinds_no_insert BEFORE INSERT ON artifact_kinds
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
DROP TRIGGER event_kinds_no_insert;
INSERT INTO event_kinds(code) VALUES('execution_progress');
CREATE TRIGGER event_kinds_no_insert BEFORE INSERT ON event_kinds
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
DROP TRIGGER gate_codes_no_insert;
INSERT INTO gate_codes(code,kind,priority,release,unit_scope,member_scope,candidate_scope)
VALUES('result_pending','automatic',6,'transition',1,1,1);
CREATE TRIGGER gate_codes_no_insert BEFORE INSERT ON gate_codes
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
