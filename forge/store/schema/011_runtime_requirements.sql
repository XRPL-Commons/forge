-- Current-revision prerequisite proofs and durable environment projection intent.
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
CREATE INDEX dependency_current ON dependency_checks(member_id,requirements_revision_id,generation DESC);
CREATE TRIGGER dependency_checks_no_update BEFORE UPDATE ON dependency_checks
BEGIN SELECT RAISE(ABORT,'dependency observations are immutable'); END;
CREATE TRIGGER dependency_checks_no_delete BEFORE DELETE ON dependency_checks
BEGIN SELECT RAISE(ABORT,'dependency observations are retained'); END;
ALTER TABLE unit_environment ADD COLUMN requirements_revision_id TEXT REFERENCES issue_revisions(id);
ALTER TABLE unit_environment ADD COLUMN config_hash TEXT;
ALTER TABLE unit_environment ADD COLUMN source_job_id TEXT REFERENCES jobs(id);
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
CREATE TRIGGER environment_label_effects_no_update BEFORE UPDATE ON environment_label_effects
BEGIN SELECT RAISE(ABORT,'environment label intent is immutable'); END;
CREATE TRIGGER environment_label_effects_no_delete BEFORE DELETE ON environment_label_effects
BEGIN SELECT RAISE(ABORT,'environment label intent is retained'); END;
CREATE TABLE environment_job_retries (
    job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
    kind TEXT NOT NULL CHECK(kind IN ('memory','services')),
    selection_json TEXT NOT NULL CHECK(json_valid(selection_json) AND length(CAST(selection_json AS BLOB))<=65536),
    at REAL NOT NULL
) STRICT;
