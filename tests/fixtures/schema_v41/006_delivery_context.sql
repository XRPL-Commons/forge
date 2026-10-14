-- Controller observations and decision inputs are distinct from lifecycle state.
-- Their names never select a transition; typed commands own phase and gates.
DROP TRIGGER artifact_kinds_no_insert;
INSERT INTO artifact_kinds(code,retention) VALUES('failure_evidence','durable');
CREATE TRIGGER artifact_kinds_no_insert BEFORE INSERT ON artifact_kinds
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
CREATE TABLE unit_context (
    unit_id TEXT PRIMARY KEY NOT NULL REFERENCES units(id),
    summary_json TEXT CHECK (summary_json IS NULL OR (json_valid(summary_json) AND length(CAST(summary_json AS BLOB)) <= 65536)),
    reason TEXT,
    retry_plan_json TEXT CHECK (retry_plan_json IS NULL OR (json_valid(retry_plan_json) AND length(CAST(retry_plan_json AS BLOB)) <= 65536)),
    decision_request_json TEXT CHECK (decision_request_json IS NULL OR (json_valid(decision_request_json) AND length(CAST(decision_request_json AS BLOB)) <= 65536)),
    verification_request_json TEXT CHECK (verification_request_json IS NULL OR (json_valid(verification_request_json) AND length(CAST(verification_request_json AS BLOB)) <= 65536)),
    checks_settled TEXT,
    last_observed_at REAL,
    settled_observed INTEGER CHECK (settled_observed IS NULL OR settled_observed IN (0,1)),
    observation_interruptions INTEGER NOT NULL DEFAULT 0 CHECK (observation_interruptions >= 0)
) STRICT;
CREATE TABLE unit_environment_observations (
    unit_id TEXT PRIMARY KEY NOT NULL REFERENCES units(id),
    choice_json TEXT CHECK (choice_json IS NULL OR (json_valid(choice_json) AND length(CAST(choice_json AS BLOB)) <= 65536)),
    override_json TEXT CHECK (override_json IS NULL OR (json_valid(override_json) AND length(CAST(override_json AS BLOB)) <= 65536)),
    retry_json TEXT CHECK (retry_json IS NULL OR (json_valid(retry_json) AND length(CAST(retry_json AS BLOB)) <= 65536)),
    memory_retry_json TEXT CHECK (memory_retry_json IS NULL OR (json_valid(memory_retry_json) AND length(CAST(memory_retry_json AS BLOB)) <= 65536)),
    label TEXT
) STRICT;
CREATE TABLE dependency_observations (
    unit_id TEXT PRIMARY KEY NOT NULL REFERENCES units(id),
    requirements_body TEXT,
    evidence TEXT,
    reason TEXT,
    checked_at REAL
) STRICT;
CREATE TABLE dependency_observation_members (
    unit_id TEXT NOT NULL REFERENCES dependency_observations(unit_id),
    kind TEXT NOT NULL CHECK (kind IN ('blocked','declared')),
    position INTEGER NOT NULL CHECK (position >= 0),
    value_json TEXT NOT NULL CHECK (json_valid(value_json) AND length(CAST(value_json AS BLOB)) <= 65536),
    PRIMARY KEY (unit_id,kind,position)
) STRICT;
CREATE TABLE attempt_failures (
    attempt_id TEXT PRIMARY KEY NOT NULL REFERENCES attempts(id),
    unit_id TEXT NOT NULL REFERENCES units(id),
    position INTEGER NOT NULL CHECK (position >= 0),
    role TEXT,
    cause TEXT,
    investigation TEXT,
    retry_stage TEXT,
    evidence_kind TEXT NOT NULL REFERENCES artifact_kinds(code),
    findings_json TEXT CHECK (findings_json IS NULL OR (json_valid(findings_json) AND length(CAST(findings_json AS BLOB)) <= 65536)),
    summary_json TEXT CHECK (summary_json IS NULL OR (json_valid(summary_json) AND length(CAST(summary_json AS BLOB)) <= 65536)),
    at REAL NOT NULL,
    UNIQUE (unit_id,position)
) STRICT;
