-- Deduplicated local drafts. Nothing in this table authorizes GitHub publication.
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
CREATE TRIGGER forge_problem_reports_no_update BEFORE UPDATE ON forge_problem_reports
 BEGIN SELECT RAISE(ABORT,'Forge report drafts retain their original safe signature'); END;
CREATE TRIGGER forge_problem_reports_no_delete BEFORE DELETE ON forge_problem_reports
 BEGIN SELECT RAISE(ABORT,'Forge report drafts are durable'); END;
CREATE TRIGGER forge_problem_report_jobs_no_update BEFORE UPDATE ON forge_problem_report_jobs
 BEGIN SELECT RAISE(ABORT,'Forge report associations are immutable'); END;
CREATE TRIGGER forge_problem_report_jobs_no_delete BEFORE DELETE ON forge_problem_report_jobs
 BEGIN SELECT RAISE(ABORT,'Forge report associations are immutable'); END;

-- Retain every physical investigation while allowing a repaired Forge failure
-- to retry the same original work failure. Copy children before dropping the
-- original graph; foreign keys remain enabled throughout.
CREATE TABLE recovery_assignments_next (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
 failed_job_id TEXT NOT NULL REFERENCES jobs(id),
 failed_attempt_id TEXT NOT NULL REFERENCES attempt_failures(attempt_id),
 created_at REAL NOT NULL
) STRICT;
INSERT INTO recovery_assignments_next SELECT * FROM recovery_assignments;
CREATE TABLE recovery_findings_next (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES recovery_assignments_next(job_id),
 incomplete INTEGER NOT NULL CHECK(incomplete IN(0,1)),
 accepted INTEGER NOT NULL CHECK(accepted IN(0,1)),
 findings_json TEXT NOT NULL CHECK(json_valid(findings_json) AND length(CAST(findings_json AS BLOB))<=65536),
 recorded_at REAL NOT NULL
) STRICT;
INSERT INTO recovery_findings_next SELECT * FROM recovery_findings;
DROP TABLE recovery_findings;
DROP TABLE recovery_assignments;
ALTER TABLE recovery_assignments_next RENAME TO recovery_assignments;
ALTER TABLE recovery_findings_next RENAME TO recovery_findings;
CREATE INDEX recovery_assignment_history ON recovery_assignments(failed_job_id,created_at);
CREATE TRIGGER recovery_assignment_valid BEFORE INSERT ON recovery_assignments
WHEN NOT EXISTS (SELECT 1 FROM jobs r JOIN jobs f ON f.id=NEW.failed_job_id
 JOIN attempt_failures a ON a.attempt_id=f.attempt_id
 WHERE r.id=NEW.job_id AND r.role='recovery' AND r.unit_id=f.unit_id
 AND f.role!='recovery' AND f.cleanup=1
 AND (f.outcome IN ('failure','rejected') OR (f.outcome='failed' AND EXISTS(
   SELECT 1 FROM migration_recovery_adoptions m WHERE m.failed_job_id=f.id AND m.disposition='investigate')))
 AND a.attempt_id=NEW.failed_attempt_id AND a.recovery_required=1)
BEGIN SELECT RAISE(ABORT,'recovery must own a cleaned confirmed failure'); END;
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
CREATE TABLE recovery_memory_retries (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES recovery_assignments(job_id),
 allocation TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE TRIGGER recovery_memory_retries_no_update BEFORE UPDATE ON recovery_memory_retries
 BEGIN SELECT RAISE(ABORT,'memory retry evidence is immutable'); END;
CREATE TRIGGER recovery_memory_retries_no_delete BEFORE DELETE ON recovery_memory_retries
 BEGIN SELECT RAISE(ABORT,'memory retry evidence is retained'); END;
-- Non-pipeline workers retain owned waits without creating issue failures.
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
CREATE UNIQUE INDEX auxiliary_wait_owner ON auxiliary_execution_waits(domain,owner_id) WHERE resolved_at IS NULL;
CREATE TABLE forge_problem_report_auxiliary (
 signature TEXT NOT NULL REFERENCES forge_problem_reports(signature),
 execution_id TEXT NOT NULL REFERENCES auxiliary_execution_waits(execution_id),
 PRIMARY KEY(signature,execution_id)
) STRICT;
CREATE INDEX auxiliary_provider_cooldown ON auxiliary_execution_waits(
 json_extract(details_json,'$.account_ref'),json_extract(runner_config_json,'$.provider'),json_extract(details_json,'$.retry_at'))
 WHERE kind='provider_error';
