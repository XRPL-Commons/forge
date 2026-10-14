-- Steward turns refer to normal jobs, immutable artifacts and lifecycle decisions.
DROP TRIGGER job_roles_no_insert;
INSERT INTO job_roles VALUES('steward');
CREATE TRIGGER job_roles_no_insert BEFORE INSERT ON job_roles BEGIN
 SELECT RAISE(ABORT,'registry changes require a migration'); END;
DROP TRIGGER artifact_kinds_no_insert;
INSERT INTO artifact_kinds VALUES('steward_dossier','durable'),('steward_decision','durable');
CREATE TRIGGER artifact_kinds_no_insert BEFORE INSERT ON artifact_kinds BEGIN
 SELECT RAISE(ABORT,'registry changes require a migration'); END;

CREATE TABLE steward_turns (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id),
 unit_id TEXT NOT NULL REFERENCES units(id),
 cycle INTEGER NOT NULL CHECK(cycle BETWEEN 1 AND 3),
 mode TEXT NOT NULL CHECK(mode IN ('recovery','stall','brief')),
 trigger_key TEXT NOT NULL,
 dossier_digest TEXT NOT NULL,
 created_at REAL NOT NULL,
 physical_attempt INTEGER NOT NULL CHECK(physical_attempt BETWEEN 1 AND 3),
 UNIQUE(unit_id,cycle,physical_attempt)
) STRICT;
CREATE TABLE steward_decisions (
 decision_id TEXT PRIMARY KEY REFERENCES decisions(id) DEFERRABLE INITIALLY DEFERRED,
 job_id TEXT NOT NULL UNIQUE REFERENCES steward_turns(job_id),
 output_digest TEXT NOT NULL,
 unit_id TEXT NOT NULL REFERENCES units(id),
 trigger_key TEXT NOT NULL,
 accepted_at REAL NOT NULL,
 UNIQUE(unit_id,trigger_key)
) STRICT;
CREATE TABLE steward_applications (
 decision_id TEXT PRIMARY KEY REFERENCES steward_selections(decision_id),
 command_id TEXT NOT NULL UNIQUE REFERENCES decisions(id) DEFERRABLE INITIALLY DEFERRED,
 brief_decision_id TEXT REFERENCES steward_decisions(decision_id),
 applied_at REAL NOT NULL
) STRICT;
CREATE TABLE steward_selections (
 decision_id TEXT PRIMARY KEY REFERENCES steward_decisions(decision_id),
 choice_id TEXT NOT NULL,
 choice_digest TEXT NOT NULL,
 command_id TEXT NOT NULL UNIQUE REFERENCES decisions(id) DEFERRABLE INITIALLY DEFERRED,
 selected_by TEXT NOT NULL CHECK(selected_by IN ('auto','operator')),
 selected_at REAL NOT NULL
) STRICT;
CREATE TRIGGER steward_turns_no_update BEFORE UPDATE ON steward_turns
 BEGIN SELECT RAISE(ABORT,'steward assignment is immutable'); END;
CREATE TRIGGER steward_turns_no_delete BEFORE DELETE ON steward_turns
 BEGIN SELECT RAISE(ABORT,'steward assignment is retained'); END;
CREATE TRIGGER steward_decisions_no_update BEFORE UPDATE ON steward_decisions
 BEGIN SELECT RAISE(ABORT,'steward decision is immutable'); END;
CREATE TRIGGER steward_decisions_no_delete BEFORE DELETE ON steward_decisions
 BEGIN SELECT RAISE(ABORT,'steward decision is retained'); END;
CREATE TRIGGER steward_selections_no_update BEFORE UPDATE ON steward_selections
 BEGIN SELECT RAISE(ABORT,'steward selection is immutable'); END;
CREATE TRIGGER steward_selections_no_delete BEFORE DELETE ON steward_selections
 BEGIN SELECT RAISE(ABORT,'steward selection is retained'); END;
CREATE TRIGGER steward_applications_no_update BEFORE UPDATE ON steward_applications
 BEGIN SELECT RAISE(ABORT,'steward application is immutable'); END;
CREATE TRIGGER steward_applications_no_delete BEFORE DELETE ON steward_applications
 BEGIN SELECT RAISE(ABORT,'steward application is retained'); END;

CREATE TRIGGER steward_turn_assignment BEFORE INSERT ON steward_turns
WHEN NOT EXISTS(SELECT 1 FROM jobs j WHERE j.id=NEW.job_id AND j.unit_id=NEW.unit_id AND j.role='steward')
 OR EXISTS(SELECT 1 FROM steward_turns t WHERE t.unit_id=NEW.unit_id AND t.cycle=NEW.cycle AND t.trigger_key!=NEW.trigger_key)
BEGIN SELECT RAISE(ABORT,'steward turn requires its exact physical assignment'); END;
CREATE TRIGGER steward_decision_assignment BEFORE INSERT ON steward_decisions
WHEN NOT EXISTS(SELECT 1 FROM steward_turns t WHERE t.job_id=NEW.job_id AND t.unit_id=NEW.unit_id AND t.trigger_key=NEW.trigger_key)
BEGIN SELECT RAISE(ABORT,'steward decision requires its assigned trigger'); END;

-- Add the explicit new role without rewriting historical configuration or jobs.
CREATE TEMP TABLE steward_role_configs AS
SELECT c.domain,c.subject,c.revision AS previous_revision,
 (SELECT MAX(h.revision)+1 FROM configuration h WHERE h.domain=c.domain AND h.subject=c.subject) AS revision,
 json_set(c.settings_json,
  '$.slots.steward',COALESCE(json_extract(c.settings_json,'$.slots.steward'),json_extract(c.settings_json,'$.slots.recovery'),1),
  '$.timeout_seconds.steward',COALESCE(json_extract(c.settings_json,'$.timeout_seconds.steward'),json_extract(c.settings_json,'$.timeout_seconds.recovery'),600)) AS settings_json,
 c.credential_ref
FROM configuration c WHERE c.active=1 AND c.subject='instance'
 AND c.domain IN ('runtime.json','setup/runtime.json','host-runtime.json')
 AND (json_type(c.settings_json,'$.slots.steward') IS NULL OR json_type(c.settings_json,'$.timeout_seconds.steward') IS NULL);
UPDATE configuration SET active=0 WHERE (domain,subject,revision) IN
 (SELECT domain,subject,previous_revision FROM steward_role_configs);
INSERT INTO configuration(domain,subject,revision,active,settings_json,credential_ref,created_at)
SELECT domain,subject,revision,1,settings_json,credential_ref,unixepoch('now') FROM steward_role_configs;
DROP TABLE steward_role_configs;
CREATE INDEX steward_decisions_unit_page ON steward_decisions(unit_id,accepted_at DESC,decision_id DESC);

-- Each remote mutation retains the existing outbox retry and uncertainty state.
CREATE TABLE steward_effect_groups (
 decision_id TEXT PRIMARY KEY REFERENCES steward_selections(decision_id),
 actor_id TEXT NOT NULL,
 scope_json TEXT NOT NULL CHECK(json_valid(scope_json) AND length(CAST(scope_json AS BLOB))<=65536),
 completed_at REAL
) STRICT;
CREATE TABLE steward_issue_effects (
 effect_id TEXT PRIMARY KEY REFERENCES external_effects(id),
 decision_id TEXT NOT NULL REFERENCES steward_effect_groups(decision_id),
 position INTEGER NOT NULL CHECK(position>=0),
 UNIQUE(decision_id,position)
) STRICT;
CREATE TRIGGER steward_effect_group_identity BEFORE UPDATE ON steward_effect_groups
WHEN NEW.decision_id!=OLD.decision_id OR NEW.actor_id!=OLD.actor_id OR NEW.scope_json!=OLD.scope_json
 OR OLD.completed_at IS NOT NULL AND NEW.completed_at IS NOT OLD.completed_at
BEGIN SELECT RAISE(ABORT,'steward effect authority is immutable'); END;
CREATE TRIGGER steward_effect_group_retained BEFORE DELETE ON steward_effect_groups
BEGIN SELECT RAISE(ABORT,'steward effect authority is retained'); END;
CREATE TRIGGER steward_issue_effect_identity BEFORE UPDATE ON steward_issue_effects
BEGIN SELECT RAISE(ABORT,'steward effect identity is immutable'); END;
CREATE TRIGGER steward_issue_effect_retained BEFORE DELETE ON steward_issue_effects
BEGIN SELECT RAISE(ABORT,'steward effects are retained'); END;
