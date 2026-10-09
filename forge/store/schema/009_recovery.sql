-- One read-only investigation owns one failed assignment. Imported history is
-- explicitly distinct until its legacy reconciliation is complete.
ALTER TABLE attempt_failures ADD COLUMN recovery_required INTEGER NOT NULL DEFAULT 0 CHECK (recovery_required IN (0,1));
CREATE TABLE recovery_assignments (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
    failed_job_id TEXT UNIQUE NOT NULL REFERENCES jobs(id),
    failed_attempt_id TEXT NOT NULL REFERENCES attempt_failures(attempt_id),
    created_at REAL NOT NULL
) STRICT;
CREATE TRIGGER recovery_assignment_valid BEFORE INSERT ON recovery_assignments
WHEN NOT EXISTS (SELECT 1 FROM jobs r JOIN jobs f ON f.id=NEW.failed_job_id
    JOIN attempt_failures a ON a.attempt_id=f.attempt_id
    WHERE r.id=NEW.job_id AND r.role='recovery' AND r.unit_id=f.unit_id
      AND f.role!='recovery' AND f.cleanup=1 AND f.outcome IN ('failure','rejected')
      AND a.attempt_id=NEW.failed_attempt_id AND a.recovery_required=1)
BEGIN SELECT RAISE(ABORT,'recovery must own a cleaned confirmed failure'); END;
CREATE TRIGGER recovery_assignments_no_update BEFORE UPDATE ON recovery_assignments
BEGIN SELECT RAISE(ABORT,'recovery assignment is immutable'); END;
CREATE TRIGGER recovery_assignments_no_delete BEFORE DELETE ON recovery_assignments
BEGIN SELECT RAISE(ABORT,'recovery assignment is retained'); END;
CREATE TABLE recovery_findings (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES recovery_assignments(job_id),
    incomplete INTEGER NOT NULL CHECK (incomplete IN (0,1)),
    accepted INTEGER NOT NULL CHECK (accepted IN (0,1)),
    findings_json TEXT NOT NULL CHECK (json_valid(findings_json) AND length(CAST(findings_json AS BLOB))<=65536),
    recorded_at REAL NOT NULL
) STRICT;
CREATE TRIGGER recovery_findings_no_update BEFORE UPDATE ON recovery_findings
BEGIN SELECT RAISE(ABORT,'investigation findings are immutable'); END;
CREATE TRIGGER recovery_findings_no_delete BEFORE DELETE ON recovery_findings
BEGIN SELECT RAISE(ABORT,'investigation findings are retained'); END;
CREATE INDEX required_investigations ON attempt_failures(unit_id,position) WHERE recovery_required=1;
