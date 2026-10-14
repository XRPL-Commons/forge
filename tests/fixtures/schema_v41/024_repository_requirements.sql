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

-- Earlier checks recorded only local issue numbers and open/closed state.
-- Preserve those observations, then append a pending observation in the new
-- format: neither immutable GitHub identity nor completion reason was recorded.
INSERT INTO dependency_checks(id,unit_id,member_id,requirements_revision_id,generation,clear,facts_json,observed_at)
SELECT c.id||':normalized24',c.unit_id,c.member_id,c.requirements_revision_id,c.generation+1,0,
       (SELECT json_group_array(json_object('reference',lower(r.full_name)||'#'||json_extract(f.value,'$.number'),
                                           'error','prerequisite_unobserved')) FROM json_each(c.facts_json) f),c.observed_at
FROM dependency_checks c JOIN units u ON u.id=c.unit_id JOIN repositories r ON r.id=u.repository_id
WHERE c.generation=(SELECT MAX(previous.generation) FROM dependency_checks previous WHERE previous.member_id=c.member_id)
  AND json_array_length(c.facts_json)>0 AND json_extract(c.facts_json,'$[0].reference') IS NULL;
-- Gates are immutable evidence. Supersede only live legacy gates and retain
-- their original numeric payload beside the new reference-based gate.
CREATE TEMP TABLE prerequisite_gates_24 AS
SELECT g.*,json_set(g.details_json,'$.blocked',json(
    (SELECT json_group_array(json_object('reference',lower(r.full_name)||'#'||f.value,'reason','prerequisite_unobserved'))
     FROM json_each(g.details_json,'$.blocked') f))) AS normalized_details
FROM unit_gates g JOIN units u ON u.id=g.unit_id JOIN repositories r ON r.id=u.repository_id
WHERE g.code='dependency' AND g.resolved_at IS NULL
  AND u.state NOT IN ('merged','closed','dissolved')
  AND json_extract(g.details_json,'$.source')='runtime_dependencies'
  AND json_type(g.details_json,'$.blocked[0]')='integer';
UPDATE unit_gates SET resolved_at=unixepoch(),resolution='superseded'
WHERE id IN (SELECT id FROM prerequisite_gates_24);
INSERT INTO unit_gates(id,unit_id,candidate_id,member_id,subject_key,code,details_json,created_at)
SELECT id||':normalized24',unit_id,candidate_id,member_id,subject_key,code,normalized_details,unixepoch()
FROM prerequisite_gates_24;
DROP TABLE prerequisite_gates_24;
