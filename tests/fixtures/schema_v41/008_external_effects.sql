-- Candidate-bound external intents. Legacy imported effects remain readable;
-- only effects with this immutable authority can be executed by the new driver.
ALTER TABLE merge_requests ADD COLUMN send_count INTEGER NOT NULL DEFAULT 0 CHECK (send_count >= 0);
ALTER TABLE merge_requests ADD COLUMN last_sent_at REAL;
CREATE TABLE candidate_effects (
    effect_id TEXT PRIMARY KEY NOT NULL REFERENCES external_effects(id),
    candidate_id TEXT NOT NULL REFERENCES candidates(id),
    job_id TEXT REFERENCES jobs(id),
    fencing_generation INTEGER NOT NULL CHECK (fencing_generation > 0),
    membership_generation INTEGER NOT NULL CHECK (membership_generation > 0)
) STRICT;
CREATE TRIGGER candidate_effects_no_update BEFORE UPDATE ON candidate_effects
BEGIN SELECT RAISE(ABORT,'effect authority is immutable'); END;
CREATE TRIGGER candidate_effects_no_delete BEFORE DELETE ON candidate_effects
BEGIN SELECT RAISE(ABORT,'effect authority is retained'); END;
CREATE INDEX candidate_effects_candidate ON candidate_effects(candidate_id,effect_id);
CREATE UNIQUE INDEX one_verdict_effect_per_job ON candidate_effects(job_id) WHERE job_id IS NOT NULL;
CREATE INDEX candidate_effect_page ON external_effects(created_at,id)
WHERE status IN ('pending','running','uncertain');
CREATE TRIGGER candidate_effect_intent_fixed BEFORE UPDATE ON external_effects
WHEN EXISTS (SELECT 1 FROM candidate_effects c WHERE c.effect_id=OLD.id)
 AND (NEW.id IS NOT OLD.id OR NEW.request_id IS NOT OLD.request_id
      OR NEW.unit_id IS NOT OLD.unit_id OR NEW.kind IS NOT OLD.kind
      OR NEW.expected_version IS NOT OLD.expected_version
      OR NEW.payload_json IS NOT OLD.payload_json OR NEW.created_at IS NOT OLD.created_at)
BEGIN SELECT RAISE(ABORT,'candidate effect intent is immutable'); END;
DROP TRIGGER event_kinds_no_insert;
INSERT INTO event_kinds(code) VALUES('external_effect_changed');
CREATE TRIGGER event_kinds_no_insert BEFORE INSERT ON event_kinds
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
