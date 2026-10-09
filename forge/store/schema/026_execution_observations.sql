-- These are observations of the exact leased container, never inferred from
-- job phase, final output capture, imported metadata, or projection read time.
ALTER TABLE job_execution_state ADD COLUMN observed_at REAL;
ALTER TABLE job_execution_state ADD COLUMN observed_status TEXT
    CHECK (observed_status IN ('missing','created','running','paused','restarting','removing','exited','dead'));
ALTER TABLE job_execution_state ADD COLUMN started_at REAL;
CREATE TRIGGER execution_observation_consistent BEFORE UPDATE ON job_execution_state
WHEN (NEW.observed_at IS NULL) != (NEW.observed_status IS NULL)
    OR NEW.observed_at < OLD.observed_at
BEGIN SELECT RAISE(ABORT,'execution observations require ordered time and status'); END;
