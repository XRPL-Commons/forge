-- Task topology is execution authority within existing approved deliveries.
PRAGMA legacy_alter_table=ON;
CREATE TABLE delivery_tasks (
 id TEXT PRIMARY KEY NOT NULL,
 builder_id TEXT NOT NULL REFERENCES unit_builders(id),
 origin_plan_id TEXT NOT NULL REFERENCES delegation_plans(id),
 task_key TEXT NOT NULL,
 executor TEXT NOT NULL CHECK(executor IN ('owner','child')),
 scope TEXT NOT NULL,
 internalizes_json TEXT NOT NULL CHECK(json_valid(internalizes_json)),
 created_at REAL NOT NULL,
 UNIQUE(origin_plan_id,task_key)
) STRICT;
CREATE TABLE delivery_task_members (
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id),
 requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
 PRIMARY KEY(task_id,member_id)
) STRICT;
CREATE INDEX task_members_scope ON delivery_task_members(member_id,task_id);
CREATE TABLE delivery_task_dependencies (
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 prerequisite_task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 PRIMARY KEY(task_id,prerequisite_task_id), CHECK(task_id!=prerequisite_task_id)
) STRICT;
CREATE INDEX task_dependents ON delivery_task_dependencies(prerequisite_task_id,task_id);
CREATE TABLE delegation_plan_tasks (
 plan_id TEXT NOT NULL REFERENCES delegation_plans(id),
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 PRIMARY KEY(plan_id,task_id)
) STRICT;
INSERT INTO delivery_tasks
SELECT 'v1:'||p.id||':'||json_extract(c.value,'$.key'),p.builder_id,p.id,
 json_extract(c.value,'$.key'),'child',json_extract(c.value,'$.scope'),'[]',p.created_at
FROM delegation_plans p,json_each(p.plan_json,'$.children') c;
INSERT INTO delivery_task_members
SELECT t.id,json_extract(c.value,'$.member_id'),json_extract(m.value,'$.requirements_revision_id')
FROM delivery_tasks t JOIN delegation_plans p ON p.id=t.origin_plan_id
JOIN jobs j ON j.id=p.parent_job_id JOIN attempts a ON a.id=j.attempt_id,
 json_each(p.plan_json,'$.children') c,json_each(a.input_manifest_json,'$.members') m
WHERE json_extract(c.value,'$.key')=t.task_key AND
 json_extract(m.value,'$.member_id')=json_extract(c.value,'$.member_id');
INSERT INTO delegation_plan_tasks SELECT origin_plan_id,id FROM delivery_tasks;
CREATE TABLE job_tasks (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
 plan_id TEXT NOT NULL REFERENCES delegation_plans(id),
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 worktree_key TEXT NOT NULL UNIQUE
) STRICT;
INSERT INTO job_tasks SELECT d.job_id,d.plan_id,t.id,d.worktree_key FROM job_delegations d
 JOIN delivery_tasks t ON t.origin_plan_id=d.plan_id AND t.task_key=d.delegation_key;
CREATE INDEX task_executions ON job_tasks(task_id,job_id);
CREATE TEMP TABLE old_invalidations AS SELECT * FROM part_output_invalidations;
DROP TABLE part_output_invalidations;
DROP TRIGGER part_outputs_no_delete;
CREATE TABLE new_part_outputs (
 id TEXT PRIMARY KEY NOT NULL,
 unit_id TEXT NOT NULL REFERENCES units(id),
 member_id TEXT,
 attempt_id TEXT NOT NULL,
 requirements_revision_id TEXT REFERENCES issue_revisions(id),
 membership_generation INTEGER NOT NULL CHECK(membership_generation>0),
 commit_oid TEXT NOT NULL,base_oid TEXT NOT NULL,input_manifest_hash TEXT NOT NULL,accepted_at REAL NOT NULL,
 task_id TEXT NOT NULL REFERENCES delivery_tasks(id),
 FOREIGN KEY(unit_id,member_id) REFERENCES unit_issues(unit_id,id),
 FOREIGN KEY(unit_id,attempt_id) REFERENCES attempts(unit_id,id), UNIQUE(attempt_id),
 CHECK((member_id IS NULL)=(requirements_revision_id IS NULL))
) STRICT;
INSERT INTO new_part_outputs SELECT o.*,t.task_id FROM part_outputs o
 JOIN jobs j ON j.attempt_id=o.attempt_id JOIN job_tasks t ON t.job_id=j.id;
CREATE TEMP TABLE migration33_assertions(value INTEGER NOT NULL CHECK(value=1));
INSERT INTO migration33_assertions SELECT (SELECT count(*) FROM new_part_outputs)=(SELECT count(*) FROM part_outputs);
INSERT INTO migration33_assertions SELECT NOT EXISTS(SELECT 1 FROM delivery_tasks t WHERE NOT EXISTS(SELECT 1 FROM delivery_task_members m WHERE m.task_id=t.id));
DROP TABLE part_outputs;
ALTER TABLE new_part_outputs RENAME TO part_outputs;
CREATE TABLE part_output_invalidations (
 output_id TEXT PRIMARY KEY NOT NULL REFERENCES part_outputs(id),reason TEXT NOT NULL,
 evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB))<=65536),created_at REAL NOT NULL
) STRICT;
INSERT INTO part_output_invalidations SELECT * FROM old_invalidations;
DROP TABLE old_invalidations;
CREATE INDEX outputs_task_current ON part_outputs(task_id,accepted_at DESC,id DESC);
CREATE INDEX outputs_member_current ON part_outputs(member_id,accepted_at DESC,id DESC);
-- Each old child pins its exact predecessor outputs, or the same-plan task not yet run.
INSERT OR IGNORE INTO delivery_task_dependencies
SELECT t.id,COALESCE(same.id,prior.task_id)
FROM delivery_tasks t JOIN delegation_plans p ON p.id=t.origin_plan_id
JOIN jobs j ON j.id=p.parent_job_id JOIN attempts a ON a.id=j.attempt_id,
 json_each(p.plan_json,'$.children') c,json_each(c.value,'$.dependencies') dep
LEFT JOIN delivery_tasks same ON same.origin_plan_id=p.id AND EXISTS(
 SELECT 1 FROM delivery_task_members m WHERE m.task_id=same.id AND m.member_id=dep.value)
LEFT JOIN json_each(a.input_manifest_json,'$.accepted_outputs') ref ON json_extract(ref.value,'$.member_id')=dep.value
LEFT JOIN part_outputs prior ON prior.id=json_extract(ref.value,'$.output_id')
WHERE json_extract(c.value,'$.key')=t.task_key;
-- Retained inputs are explicit plan nodes, including their full prerequisite closure.
INSERT OR IGNORE INTO delegation_plan_tasks
SELECT p.id,o.task_id FROM delegation_plans p JOIN jobs j ON j.id=p.parent_job_id
 JOIN attempts a ON a.id=j.attempt_id,json_each(a.input_manifest_json,'$.accepted_outputs') ref
 JOIN part_outputs o ON o.id=json_extract(ref.value,'$.output_id');
INSERT OR IGNORE INTO delegation_plan_tasks
WITH RECURSIVE closure(plan_id,task_id) AS (
 SELECT plan_id,task_id FROM delegation_plan_tasks UNION
 SELECT c.plan_id,d.prerequisite_task_id FROM closure c JOIN delivery_task_dependencies d ON d.task_id=c.task_id)
SELECT plan_id,task_id FROM closure;
DROP TRIGGER job_delegations_no_delete;
DROP TABLE job_delegations;
CREATE VIEW job_delegations AS SELECT j.job_id,j.plan_id,t.task_key AS delegation_key,j.worktree_key
 FROM job_tasks j JOIN delivery_tasks t ON t.id=j.task_id;
ALTER TABLE jobs ADD COLUMN execution_kind TEXT NOT NULL DEFAULT 'legacy' CHECK(execution_kind IN ('legacy','task'));
DROP INDEX one_live_unit_job;
CREATE UNIQUE INDEX one_live_unit_job ON jobs(unit_id) WHERE member_id IS NULL AND execution_kind!='task'
 AND fenced=0 AND phase IN ('reserved','starting','running','collecting');
DROP TRIGGER jobs_pin_members;
CREATE TRIGGER jobs_pin_members AFTER INSERT ON jobs BEGIN
 INSERT INTO job_members(job_id,member_id,requirements_revision_id)
 SELECT NEW.id,m.id,COALESCE(json_extract(a.input_manifest_json,'$.requirements_revision_id'),
 (SELECT json_extract(value,'$.requirements_revision_id') FROM json_each(a.input_manifest_json,'$.members')
 WHERE json_extract(value,'$.member_id')=m.id),m.requirements_revision_id)
 FROM unit_issues m JOIN attempts a ON a.id=NEW.attempt_id
 WHERE NEW.role!='recovery' AND m.unit_id=NEW.unit_id AND m.left_at IS NULL AND (NEW.member_id IS NULL OR NEW.member_id=m.id)
 AND (json_extract(a.input_manifest_json,'$.manifest_version') IS NOT 2 OR EXISTS(
 SELECT 1 FROM json_each(a.input_manifest_json,'$.members') pin WHERE json_extract(pin.value,'$.member_id')=m.id));
END;
CREATE TABLE new_unit_gates (
    id TEXT PRIMARY KEY NOT NULL,
    unit_id TEXT NOT NULL REFERENCES units(id),
    candidate_id TEXT,
    member_id TEXT,
    subject_key TEXT NOT NULL,
    job_id TEXT REFERENCES jobs(id),
    code TEXT NOT NULL REFERENCES gate_codes(code),
    details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND json_type(details_json) = 'object' AND length(CAST(details_json AS BLOB)) <= 65536),
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolution TEXT CHECK (resolution IN ('operator','observation','recheck','time','evidence','transition','superseded')),
    CHECK ((resolved_at IS NULL) = (resolution IS NULL)),
    CHECK ((job_id IS NULL AND member_id IS NULL AND candidate_id IS NULL AND subject_key = 'unit') OR
           (job_id IS NULL AND member_id IS NOT NULL AND candidate_id IS NULL AND subject_key = 'member:' || member_id) OR
           (job_id IS NULL AND member_id IS NULL AND candidate_id IS NOT NULL AND subject_key = 'candidate:' || candidate_id) OR
           (job_id IS NOT NULL AND member_id IS NULL AND candidate_id IS NULL AND subject_key='job:'||job_id)),
    UNIQUE (unit_id, id),
    FOREIGN KEY (unit_id, candidate_id) REFERENCES candidates(unit_id, id),
    FOREIGN KEY (unit_id, member_id) REFERENCES unit_issues(unit_id, id)
) STRICT;
INSERT INTO new_unit_gates(id,unit_id,candidate_id,member_id,subject_key,code,details_json,created_at,resolved_at,resolution)
 SELECT id,unit_id,candidate_id,member_id,subject_key,code,details_json,created_at,resolved_at,resolution FROM unit_gates;
DROP TRIGGER gates_no_delete;
DROP TABLE unit_gates;
ALTER TABLE new_unit_gates RENAME TO unit_gates;
CREATE UNIQUE INDEX one_current_gate_code ON unit_gates(unit_id,code,subject_key) WHERE resolved_at IS NULL;
CREATE INDEX unresolved_gates ON unit_gates(unit_id,resolved_at,code);
CREATE INDEX task_job_gates ON unit_gates(job_id,code) WHERE resolved_at IS NULL;
ALTER TABLE gate_codes ADD COLUMN job_scope INTEGER NOT NULL DEFAULT 0 CHECK(job_scope IN (0,1));
DROP TRIGGER gate_codes_no_update;
UPDATE gate_codes SET job_scope=1 WHERE code IN ('cleanup_pending','result_pending','recovery_pending');
CREATE TRIGGER gate_codes_no_update BEFORE UPDATE ON gate_codes BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
CREATE TRIGGER gates_scope BEFORE INSERT ON unit_gates
WHEN NOT EXISTS(SELECT 1 FROM gate_codes WHERE code=NEW.code AND (
 (NEW.job_id IS NOT NULL AND job_scope=1 AND EXISTS(SELECT 1 FROM jobs j WHERE j.id=NEW.job_id AND j.unit_id=NEW.unit_id)) OR
 (NEW.job_id IS NULL AND ((NEW.member_id IS NOT NULL AND member_scope=1) OR
 (NEW.member_id IS NULL AND NEW.candidate_id IS NOT NULL AND candidate_scope=1) OR
 (NEW.member_id IS NULL AND NEW.candidate_id IS NULL AND unit_scope=1)))))
BEGIN SELECT RAISE(ABORT,'gate scope does not fit its code'); END;
CREATE TRIGGER gates_open_unit BEFORE INSERT ON unit_gates
WHEN EXISTS (SELECT 1 FROM units WHERE id=NEW.unit_id AND state IN ('merged','closed','dissolved'))
BEGIN SELECT RAISE(ABORT, 'a terminal unit cannot gain gates'); END;
CREATE TRIGGER gates_resolve_only BEFORE UPDATE ON unit_gates
WHEN OLD.resolved_at IS NOT NULL OR NEW.resolved_at IS NULL
    OR NEW.id IS NOT OLD.id OR NEW.unit_id IS NOT OLD.unit_id OR NEW.code IS NOT OLD.code
    OR NEW.job_id IS NOT OLD.job_id OR NEW.subject_key IS NOT OLD.subject_key OR NEW.candidate_id IS NOT OLD.candidate_id
    OR NEW.member_id IS NOT OLD.member_id OR NEW.details_json IS NOT OLD.details_json
    OR NEW.created_at IS NOT OLD.created_at
    OR (NEW.resolution != 'superseded' AND NEW.resolution IS NOT (SELECT release FROM gate_codes WHERE code=OLD.code))
BEGIN SELECT RAISE(ABORT, 'a gate can only resolve once using its release rule'); END;
CREATE TRIGGER gates_no_delete BEFORE DELETE ON unit_gates
BEGIN SELECT RAISE(ABORT, 'gate history is retained'); END;
-- Existing unresolved process gates keep their exact recorded job identity.
-- Coalesced cleanup waits are reconstructed from every uncleaned assignment.
CREATE TEMP TABLE old_process_gates AS SELECT * FROM unit_gates WHERE resolved_at IS NULL AND code IN ('cleanup_pending','result_pending','recovery_pending');
DROP TRIGGER gates_resolve_only;
UPDATE unit_gates SET resolved_at=created_at,resolution='superseded' WHERE resolved_at IS NULL
 AND code IN ('cleanup_pending','result_pending','recovery_pending')
 AND EXISTS(SELECT 1 FROM jobs j WHERE j.id=json_extract(details_json,'$.job_id') AND j.unit_id=unit_gates.unit_id);
INSERT INTO unit_gates(id,unit_id,job_id,subject_key,code,details_json,created_at)
SELECT old.id||':job33',old.unit_id,json_extract(old.details_json,'$.job_id'),'job:'||json_extract(old.details_json,'$.job_id'),old.code,old.details_json,old.created_at
FROM old_process_gates old WHERE old.code IN ('result_pending','recovery_pending')
 AND EXISTS(SELECT 1 FROM jobs j WHERE j.id=json_extract(old.details_json,'$.job_id') AND j.unit_id=old.unit_id)
 AND NOT EXISTS(SELECT 1 FROM unit_gates g WHERE g.unit_id=old.unit_id AND g.job_id=json_extract(old.details_json,'$.job_id') AND g.code=old.code AND g.resolved_at IS NULL);
INSERT INTO unit_gates(id,unit_id,job_id,subject_key,code,details_json,created_at)
SELECT 'cleanup33:'||j.id,j.unit_id,j.id,'job:'||j.id,'cleanup_pending',json_object('job_id',j.id),j.created_at
FROM jobs j JOIN units u ON u.id=j.unit_id WHERE j.cleanup=0 AND u.state NOT IN ('merged','closed','dissolved')
 AND EXISTS(SELECT 1 FROM old_process_gates g WHERE g.unit_id=j.unit_id AND g.code='cleanup_pending');
DROP TABLE old_process_gates;
CREATE TRIGGER gates_resolve_only BEFORE UPDATE ON unit_gates
WHEN OLD.resolved_at IS NOT NULL OR NEW.resolved_at IS NULL
    OR NEW.id IS NOT OLD.id OR NEW.unit_id IS NOT OLD.unit_id OR NEW.code IS NOT OLD.code
    OR NEW.job_id IS NOT OLD.job_id OR NEW.subject_key IS NOT OLD.subject_key OR NEW.candidate_id IS NOT OLD.candidate_id
    OR NEW.member_id IS NOT OLD.member_id OR NEW.details_json IS NOT OLD.details_json
    OR NEW.created_at IS NOT OLD.created_at
    OR (NEW.resolution != 'superseded' AND NEW.resolution IS NOT (SELECT release FROM gate_codes WHERE code=OLD.code))
BEGIN SELECT RAISE(ABORT, 'a gate can only resolve once using its release rule'); END;
CREATE TRIGGER delivery_tasks_no_update BEFORE UPDATE ON delivery_tasks BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER delivery_tasks_no_delete BEFORE DELETE ON delivery_tasks BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER delivery_task_members_no_update BEFORE UPDATE ON delivery_task_members BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER delivery_task_members_no_delete BEFORE DELETE ON delivery_task_members BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER delivery_task_dependencies_no_update BEFORE UPDATE ON delivery_task_dependencies BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER delivery_task_dependencies_no_delete BEFORE DELETE ON delivery_task_dependencies BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER delegation_plan_tasks_no_update BEFORE UPDATE ON delegation_plan_tasks BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER delegation_plan_tasks_no_delete BEFORE DELETE ON delegation_plan_tasks BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER job_tasks_no_update BEFORE UPDATE ON job_tasks BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER job_tasks_no_delete BEFORE DELETE ON job_tasks BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER part_outputs_no_update BEFORE UPDATE ON part_outputs BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER part_outputs_no_delete BEFORE DELETE ON part_outputs BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER part_output_invalidations_no_update BEFORE UPDATE ON part_output_invalidations BEGIN SELECT RAISE(ABORT,'task authority is immutable'); END;
CREATE TRIGGER part_output_invalidations_no_delete BEFORE DELETE ON part_output_invalidations BEGIN SELECT RAISE(ABORT,'task history is retained'); END;
CREATE TRIGGER task_member_scope BEFORE INSERT ON delivery_task_members
WHEN NOT EXISTS(SELECT 1 FROM delivery_tasks t JOIN unit_builders b ON b.id=t.builder_id
 JOIN unit_issues m ON m.unit_id=b.unit_id JOIN issue_revisions r ON r.issue_id=m.issue_id
 WHERE t.id=NEW.task_id AND m.id=NEW.member_id AND r.id=NEW.requirements_revision_id)
BEGIN SELECT RAISE(ABORT,'task member must match approved unit scope'); END;
CREATE TRIGGER task_job_scope BEFORE INSERT ON job_tasks
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN delivery_tasks t ON t.id=NEW.task_id
 JOIN unit_builders b ON b.id=t.builder_id JOIN delegation_plan_tasks p ON p.task_id=t.id AND p.plan_id=NEW.plan_id
 WHERE j.id=NEW.job_id AND j.unit_id=b.unit_id AND j.role='build')
 OR EXISTS(SELECT member_id,requirements_revision_id FROM delivery_task_members WHERE task_id=NEW.task_id
 EXCEPT SELECT member_id,requirements_revision_id FROM job_members WHERE job_id=NEW.job_id)
 OR EXISTS(SELECT member_id,requirements_revision_id FROM job_members WHERE job_id=NEW.job_id
 EXCEPT SELECT member_id,requirements_revision_id FROM delivery_task_members WHERE task_id=NEW.task_id)
BEGIN SELECT RAISE(ABORT,'task assignment must pin its exact member scope'); END;
CREATE TRIGGER task_output_scope BEFORE INSERT ON part_outputs
WHEN NOT EXISTS(SELECT 1 FROM jobs j JOIN job_tasks t ON t.job_id=j.id
 WHERE j.attempt_id=NEW.attempt_id AND j.unit_id=NEW.unit_id AND t.task_id=NEW.task_id)
BEGIN SELECT RAISE(ABORT,'output must match its exact task attempt'); END;
DROP TABLE migration33_assertions;
PRAGMA legacy_alter_table=OFF;

CREATE TRIGGER jobs_execution_kind_immutable BEFORE UPDATE OF execution_kind ON jobs
WHEN NEW.execution_kind IS NOT OLD.execution_kind BEGIN SELECT RAISE(ABORT,'execution scope kind is immutable'); END;
CREATE TRIGGER task_dependency_owner BEFORE INSERT ON delivery_task_dependencies
WHEN NOT EXISTS(SELECT 1 FROM delivery_tasks a JOIN delivery_tasks b ON a.builder_id=b.builder_id WHERE a.id=NEW.task_id AND b.id=NEW.prerequisite_task_id)
BEGIN SELECT RAISE(ABORT,'task dependency must stay within its owner'); END;
CREATE TRIGGER plan_task_owner BEFORE INSERT ON delegation_plan_tasks
WHEN NOT EXISTS(SELECT 1 FROM delivery_tasks t JOIN delegation_plans p ON p.builder_id=t.builder_id WHERE t.id=NEW.task_id AND p.id=NEW.plan_id)
BEGIN SELECT RAISE(ABORT,'plan task must retain its owner'); END;

CREATE TRIGGER task_one_live_execution BEFORE INSERT ON job_tasks
WHEN EXISTS(SELECT 1 FROM job_tasks t JOIN jobs j ON j.id=t.job_id WHERE t.task_id=NEW.task_id AND (j.cleanup=0 OR j.outcome IS NULL))
BEGIN SELECT RAISE(ABORT,'task already owns a physical execution'); END;

-- Indexed facts retain exact physical job scope; canonical selectors still resolve topology.
DROP VIEW issue_selection_facts;
CREATE VIEW issue_selection_facts AS
WITH latest AS (
 SELECT i.id AS issue_id,(SELECT m.id FROM unit_issues m WHERE m.issue_id=i.id
   ORDER BY (m.left_at IS NULL) DESC,m.joined_at DESC,m.id DESC LIMIT 1) AS member_id FROM issues i
), facts AS (
 SELECT i.id,i.repository_id,i.github_number,i.github_state,i.state_reason,i.reopened_version,
   m.id AS member_id,m.left_at,m.completion_issue_version,m.requirements_revision_id,
   u.id AS unit_id,u.state AS unit_state,u.current_candidate_id,u.membership_generation,
   CASE WHEN m.left_at IS NULL AND u.state NOT IN ('merged','closed','dissolved') THEN 1 ELSE 0 END AS current,
   r.enabled,ir.requirements_hash AS current_requirements,pinned.requirements_hash AS pinned_requirements
 FROM issues i JOIN repositories r ON r.id=i.repository_id JOIN latest l ON l.issue_id=i.id
 JOIN issue_revisions ir ON ir.id=i.current_revision_id LEFT JOIN unit_issues m ON m.id=l.member_id
 LEFT JOIN units u ON u.id=m.unit_id LEFT JOIN issue_revisions pinned ON pinned.id=m.requirements_revision_id
), classified AS (
 SELECT f.*,COALESCE((SELECT b.code FROM (
 SELECT f.id AS issue_id,g.code,g.created_at,g.id AS gate_id FROM unit_gates g
 WHERE g.unit_id=f.unit_id AND f.current=1 AND g.resolved_at IS NULL AND (g.candidate_id IS NULL OR g.candidate_id=f.current_candidate_id)
 AND (g.job_id IS NULL OR EXISTS(SELECT 1 FROM job_members scope WHERE scope.job_id=g.job_id AND scope.member_id=f.member_id))
 AND (g.member_id IS NULL OR g.member_id=f.member_id OR f.unit_state NOT IN ('queued','building'))
 UNION ALL SELECT f.id,'repository_setup',0,'derived:repository' WHERE f.current=1 AND f.enabled=0
 UNION ALL SELECT f.id,'membership_decision',0,'derived:requirements' WHERE f.current=1 AND f.current_requirements!=f.pinned_requirements
 UNION ALL SELECT f.id,'external_change',0,'derived:closed' WHERE f.current=1 AND f.github_state='closed'
 UNION ALL SELECT f.id,'dependency',0,'derived:dependency' WHERE f.current=1 AND (
   (EXISTS(SELECT 1 FROM issue_dependencies d WHERE d.issue_id=f.id AND d.requirements_revision_id=f.requirements_revision_id)
    AND NOT COALESCE((SELECT clear FROM dependency_checks c WHERE c.member_id=f.member_id AND c.requirements_revision_id=f.requirements_revision_id ORDER BY generation DESC LIMIT 1),0))
   OR EXISTS(SELECT 1 FROM member_dependencies d WHERE d.unit_id=f.unit_id AND d.member_id=f.member_id AND d.membership_generation=f.membership_generation))
) b JOIN gate_codes c ON c.code=b.code
 ORDER BY c.priority,c.rowid,b.created_at,b.gate_id LIMIT 1),CASE WHEN f.current=0 AND f.github_state='open' THEN d.reason_code END) AS indexed_reason_code,
 CASE WHEN f.github_state='closed' AND f.state_reason='completed' THEN 'done'
      WHEN f.github_state='closed' AND f.state_reason IN ('not_planned','duplicate') THEN 'dropped'
      WHEN f.github_state='closed' THEN 'blocked'
      WHEN f.unit_state='merged' AND ((f.completion_issue_version IS NOT NULL AND f.reopened_version<=f.completion_issue_version)
        OR (f.completion_issue_version IS NULL AND f.reopened_version=0 AND COALESCE(f.state_reason,'')!='reopened')) THEN 'done'
      WHEN f.current=0 THEN 'untracked' ELSE 'current' END AS issue_state,
 EXISTS(SELECT 1 FROM jobs j WHERE j.unit_id=f.unit_id AND j.cleanup=0 AND j.fenced=0 AND j.outcome IS NULL
 AND EXISTS(SELECT 1 FROM job_members scope WHERE scope.job_id=j.id AND scope.member_id=f.member_id)) AS active
 FROM facts f
 LEFT JOIN issues observed ON observed.id=f.id
 LEFT JOIN issue_discovery_observations d ON d.issue_id=f.id AND d.revision_id=observed.current_revision_id
)
SELECT *,CASE WHEN issue_state='done' THEN 'completed' WHEN issue_state='dropped' THEN 'dropped'
 WHEN indexed_reason_code IN ('requirements_stale','remote_timestamp_invalid','admission_conflict') THEN 'attention'
 WHEN issue_state='untracked' OR indexed_reason_code IN ('operator_pause','operator_backlog') THEN 'backlog'
 WHEN indexed_reason_code IN ('dependency','label_hold','ownership_conflict') THEN 'blocked'
 WHEN indexed_reason_code IN ('budget_exhausted','approach_required','decision_required','repository_setup','environment','legacy_reconciliation',
   'membership_decision','external_change','verification_human','approval_required','observation_unavailable')
   OR (github_state='closed' AND state_reason IS NULL) THEN 'attention'
 WHEN indexed_reason_code IN ('recovery_pending','cleanup_pending','result_pending') THEN 'working'
 WHEN indexed_reason_code IS NOT NULL THEN 'waiting' WHEN active THEN 'working' ELSE 'queued' END AS indexed_status
FROM classified;
