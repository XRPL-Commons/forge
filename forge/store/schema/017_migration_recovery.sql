-- Explicit provenance for upgrading old investigations without fabricating an
-- original runnable launch or rewriting the preserved import evidence.
CREATE TABLE migration_recovery_adoptions (
 failed_job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
 source_manifest TEXT NOT NULL REFERENCES import_runs(id),
 disposition TEXT NOT NULL CHECK(disposition IN ('investigate','operator_required')),
 actor_id TEXT NOT NULL,
 original_json TEXT NOT NULL CHECK(json_valid(original_json) AND length(CAST(original_json AS BLOB))<=65536),
 created_at REAL NOT NULL
) STRICT;
CREATE TRIGGER migration_recovery_adoptions_no_update BEFORE UPDATE ON migration_recovery_adoptions
BEGIN SELECT RAISE(ABORT,'migration investigation provenance is immutable'); END;
CREATE TRIGGER migration_recovery_adoptions_no_delete BEFORE DELETE ON migration_recovery_adoptions
BEGIN SELECT RAISE(ABORT,'migration investigation provenance is retained'); END;
DROP TRIGGER recovery_assignment_valid;
CREATE TRIGGER recovery_assignment_valid BEFORE INSERT ON recovery_assignments
WHEN NOT EXISTS (SELECT 1 FROM jobs r JOIN jobs f ON f.id=NEW.failed_job_id
 JOIN attempt_failures a ON a.attempt_id=f.attempt_id
 WHERE r.id=NEW.job_id AND r.role='recovery' AND r.unit_id=f.unit_id
 AND f.role!='recovery' AND f.cleanup=1
 AND (f.outcome IN ('failure','rejected') OR (f.outcome='failed' AND EXISTS(
   SELECT 1 FROM migration_recovery_adoptions m WHERE m.failed_job_id=f.id AND m.disposition='investigate')))
 AND a.attempt_id=NEW.failed_attempt_id AND a.recovery_required=1)
BEGIN SELECT RAISE(ABORT,'recovery must own a cleaned confirmed failure'); END;
