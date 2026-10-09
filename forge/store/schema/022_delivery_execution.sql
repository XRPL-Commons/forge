-- One logical builder owns a delivery across planning, child and integration jobs.
-- Jobs, attempts, leases, artifacts and effects remain execution authority.
CREATE TABLE job_members (
    job_id TEXT NOT NULL REFERENCES jobs(id),
    member_id TEXT NOT NULL REFERENCES unit_issues(id),
    requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
    PRIMARY KEY(job_id,member_id)
) STRICT;
INSERT INTO job_members(job_id,member_id,requirements_revision_id)
SELECT j.id,m.id,COALESCE(l.requirements_revision_id,
       json_extract(a.input_manifest_json,'$.requirements_revision_id'),m.requirements_revision_id)
FROM jobs j JOIN attempts a ON a.id=j.attempt_id
JOIN unit_issues m ON m.unit_id=j.unit_id AND (j.member_id IS NULL OR j.member_id=m.id)
LEFT JOIN job_launches l ON l.job_id=j.id
WHERE (COALESCE(l.requirements_revision_id,json_extract(a.input_manifest_json,'$.requirements_revision_id')) IS NULL
       AND m.joined_at<=j.created_at AND (m.left_at IS NULL OR m.left_at>=j.created_at))
   OR EXISTS(SELECT 1 FROM issue_revisions r WHERE r.id=COALESCE(l.requirements_revision_id,
       json_extract(a.input_manifest_json,'$.requirements_revision_id')) AND r.issue_id=m.issue_id);
ALTER TABLE job_launches DROP COLUMN requirements_revision_id;
CREATE TRIGGER job_members_match BEFORE INSERT ON job_members
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN unit_issues m ON m.unit_id=j.unit_id
 JOIN issue_revisions r ON r.issue_id=m.issue_id
 WHERE j.id=NEW.job_id AND m.id=NEW.member_id AND r.id=NEW.requirements_revision_id)
BEGIN SELECT RAISE(ABORT,'job scope must match its unit and issue revision'); END;
CREATE TRIGGER jobs_pin_members AFTER INSERT ON jobs BEGIN
 INSERT INTO job_members(job_id,member_id,requirements_revision_id)
 SELECT NEW.id,m.id,COALESCE(json_extract(a.input_manifest_json,'$.requirements_revision_id'),
 (SELECT json_extract(value,'$.requirements_revision_id') FROM json_each(a.input_manifest_json,'$.members')
 WHERE json_extract(value,'$.member_id')=m.id),m.requirements_revision_id)
 FROM unit_issues m JOIN attempts a ON a.id=NEW.attempt_id
 WHERE m.unit_id=NEW.unit_id AND m.left_at IS NULL AND (NEW.member_id IS NULL OR NEW.member_id=m.id);
END;
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
CREATE TRIGGER builder_assignment_scope BEFORE INSERT ON builder_assignments
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN unit_builders b ON b.unit_id=j.unit_id
 WHERE j.id=NEW.job_id AND b.id=NEW.builder_id AND j.role='build' AND j.member_id IS NULL AND j.candidate_id IS NULL)
BEGIN SELECT RAISE(ABORT,'builder assignment must own its whole delivery'); END;
CREATE TABLE delegation_plans (
    id TEXT PRIMARY KEY NOT NULL,
    builder_id TEXT NOT NULL REFERENCES unit_builders(id),
    parent_job_id TEXT NOT NULL UNIQUE REFERENCES builder_assignments(job_id),
    input_manifest_hash TEXT NOT NULL,
    plan_json TEXT NOT NULL CHECK(json_valid(plan_json) AND json_type(plan_json)='object' AND length(CAST(plan_json AS BLOB))<=65536),
    created_at REAL NOT NULL
) STRICT;
CREATE TABLE job_delegations (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
    plan_id TEXT NOT NULL REFERENCES delegation_plans(id),
    delegation_key TEXT NOT NULL,
    worktree_key TEXT NOT NULL UNIQUE,
    UNIQUE(plan_id,delegation_key)
) STRICT;
CREATE TRIGGER delegation_scope BEFORE INSERT ON job_delegations
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN delegation_plans p ON p.id=NEW.plan_id
 JOIN unit_builders b ON b.id=p.builder_id
 WHERE j.id=NEW.job_id AND j.unit_id=b.unit_id AND j.role='build' AND j.member_id IS NOT NULL)
BEGIN SELECT RAISE(ABORT,'delegation must stay within its builder delivery'); END;
CREATE TABLE part_output_invalidations (
    output_id TEXT PRIMARY KEY NOT NULL REFERENCES part_outputs(id),
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB))<=65536),
    created_at REAL NOT NULL
) STRICT;
-- Composition carries existing charge authority without moving immutable attempts.
CREATE TABLE unit_budget_lineage (
    unit_id TEXT NOT NULL REFERENCES units(id),
    budget_generation INTEGER NOT NULL CHECK(budget_generation>0),
    attempt_id TEXT NOT NULL REFERENCES attempts(id),
    PRIMARY KEY(unit_id,budget_generation,attempt_id)
) STRICT;
CREATE INDEX delegations_plan ON job_delegations(plan_id,job_id);
CREATE INDEX outputs_member_current ON part_outputs(member_id,accepted_at DESC,id DESC);
DROP TRIGGER artifact_kinds_no_insert;
INSERT INTO artifact_kinds(code,retention) VALUES('worktree_bundle','durable');
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
CREATE TRIGGER job_delegations_no_update BEFORE UPDATE ON job_delegations
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER job_delegations_no_delete BEFORE DELETE ON job_delegations
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER part_output_invalidations_no_update BEFORE UPDATE ON part_output_invalidations
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER part_output_invalidations_no_delete BEFORE DELETE ON part_output_invalidations
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER unit_budget_lineage_no_update BEFORE UPDATE ON unit_budget_lineage
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
CREATE TRIGGER unit_budget_lineage_no_delete BEFORE DELETE ON unit_budget_lineage
BEGIN SELECT RAISE(ABORT,'delivery authority is immutable'); END;
-- Source candidates remain exact, visible inputs to the new owning builder.
CREATE TABLE unit_composition_sources (
    unit_id TEXT NOT NULL REFERENCES units(id),
    source_unit_id TEXT NOT NULL REFERENCES units(id),
    source_candidate_id TEXT REFERENCES candidates(id),
    PRIMARY KEY(unit_id,source_unit_id)
) STRICT;
CREATE TRIGGER composition_sources_no_update BEFORE UPDATE ON unit_composition_sources
BEGIN SELECT RAISE(ABORT,'composition provenance is immutable'); END;
CREATE TRIGGER composition_sources_no_delete BEFORE DELETE ON unit_composition_sources
BEGIN SELECT RAISE(ABORT,'composition provenance is retained'); END;

CREATE TABLE unit_publication_targets (
    unit_id TEXT PRIMARY KEY REFERENCES units(id),
    target_json TEXT NOT NULL CHECK(json_valid(target_json) AND length(target_json)<=2048)
) STRICT;
CREATE TRIGGER publication_targets_no_update BEFORE UPDATE ON unit_publication_targets
BEGIN SELECT RAISE(ABORT,'approved publication target is immutable'); END;
CREATE TRIGGER publication_targets_no_delete BEFORE DELETE ON unit_publication_targets
BEGIN SELECT RAISE(ABORT,'approved publication target is retained'); END;
