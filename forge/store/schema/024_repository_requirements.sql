-- Operator-approved ordering is revision-bound intent. Remote observations supply
-- immutable GitHub identities and completion evidence separately.
CREATE TABLE prerequisite_orders (
    id TEXT PRIMARY KEY NOT NULL,
    request_id TEXT UNIQUE NOT NULL,
    actor_id TEXT NOT NULL,
    issue_id TEXT NOT NULL,
    requirements_revision_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation>0),
    references_json TEXT NOT NULL CHECK(json_valid(references_json) AND length(CAST(references_json AS BLOB))<=32768),
    created_at REAL NOT NULL,
    FOREIGN KEY(issue_id,requirements_revision_id) REFERENCES issue_revisions(issue_id,id),
    UNIQUE(issue_id,generation)
) STRICT;
CREATE INDEX prerequisite_order_current ON prerequisite_orders(issue_id,requirements_revision_id,generation DESC);
CREATE TRIGGER prerequisite_orders_no_update BEFORE UPDATE ON prerequisite_orders
BEGIN SELECT RAISE(ABORT,'prerequisite orders are immutable'); END;
CREATE TRIGGER prerequisite_orders_no_delete BEFORE DELETE ON prerequisite_orders
BEGIN SELECT RAISE(ABORT,'prerequisite orders are retained'); END;

-- An issue's selected environment survives delivery composition. Launch history
-- pins the member that consumed it; a unit is never an environment authority.
CREATE TABLE issue_environment_observations (
    issue_id TEXT PRIMARY KEY NOT NULL REFERENCES issues(id),
    choice_json TEXT CHECK(choice_json IS NULL OR json_valid(choice_json)),
    override_json TEXT CHECK(override_json IS NULL OR json_valid(override_json)),
    retry_json TEXT CHECK(retry_json IS NULL OR json_valid(retry_json)),
    memory_retry_json TEXT CHECK(memory_retry_json IS NULL OR json_valid(memory_retry_json)),
    label TEXT,
    environment_hold_json TEXT CHECK(environment_hold_json IS NULL OR json_valid(environment_hold_json))
) STRICT;
INSERT INTO issue_environment_observations
SELECT issue_id,choice_json,override_json,retry_json,memory_retry_json,label,environment_hold_json FROM (
    SELECT m.issue_id,e.*,row_number() OVER(PARTITION BY m.issue_id ORDER BY m.joined_at DESC,m.id DESC) AS rank
    FROM unit_environment_observations e JOIN unit_issues m ON m.unit_id=e.unit_id
) WHERE rank=1;
ALTER TABLE unit_environment ADD COLUMN member_id TEXT REFERENCES unit_issues(id);
UPDATE unit_environment SET member_id=(
    SELECT m.id FROM unit_issues m WHERE m.unit_id=unit_environment.unit_id
    AND m.requirements_revision_id=unit_environment.requirements_revision_id
    ORDER BY m.joined_at DESC,m.id DESC LIMIT 1
);
CREATE INDEX environment_member_current ON unit_environment(member_id,revision DESC);
CREATE TABLE prerequisite_identities (
    reference TEXT PRIMARY KEY NOT NULL,
    repository_id INTEGER NOT NULL CHECK(repository_id>0),
    github_id INTEGER NOT NULL CHECK(github_id>0),
    number INTEGER NOT NULL CHECK(number>0),
    kind TEXT NOT NULL CHECK(kind IN ('issue','pull_request')),
    observed_at REAL NOT NULL
) STRICT;
CREATE TRIGGER prerequisite_identity_no_update BEFORE UPDATE ON prerequisite_identities
BEGIN SELECT RAISE(ABORT,'prerequisite identity cannot be rebound'); END;
CREATE TRIGGER prerequisite_identity_no_delete BEFORE DELETE ON prerequisite_identities
BEGIN SELECT RAISE(ABORT,'prerequisite identities are retained'); END;
