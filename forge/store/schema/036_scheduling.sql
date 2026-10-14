-- Exact-scope scheduling decisions survive controller restarts. They grant no
-- admission authority: each controller still revalidates the underlying facts.
CREATE TABLE next_actions (
 unit_id TEXT NOT NULL REFERENCES units(id),
 owner TEXT NOT NULL,
 scope_key TEXT NOT NULL,
 unit_version INTEGER NOT NULL,
 fencing_generation INTEGER NOT NULL,
 membership_generation INTEGER NOT NULL,
 candidate_id TEXT REFERENCES candidates(id),
 member_id TEXT REFERENCES unit_issues(id),
 job_id TEXT REFERENCES jobs(id),
 reason TEXT NOT NULL,
 due_at REAL NOT NULL,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(unit_id,owner,scope_key)
) STRICT;
CREATE INDEX next_actions_due ON next_actions(due_at,unit_id);
CREATE TABLE scheduling_orphans (
 member_id TEXT PRIMARY KEY NOT NULL REFERENCES unit_issues(id),
 unit_id TEXT NOT NULL REFERENCES units(id),
 unowned_since REAL NOT NULL
) STRICT;
ALTER TABLE external_effects ADD COLUMN uncertain_since REAL;
ALTER TABLE external_effects ADD COLUMN conflict_since REAL;
ALTER TABLE external_effects ADD COLUMN escalation_at REAL;

DROP TRIGGER gate_codes_no_update;
UPDATE gate_codes SET member_scope=1,candidate_scope=1,job_scope=1 WHERE code='forge_problem';
CREATE TRIGGER gate_codes_no_update BEFORE UPDATE ON gate_codes
 BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;

CREATE TRIGGER schedule_new_unit AFTER INSERT ON units
BEGIN
 INSERT INTO next_actions VALUES(NEW.id,'dispatch','unit',NEW.version,NEW.fencing_generation,NEW.membership_generation,
 NULL,NULL,NULL,'admitted',NEW.created_at,NEW.created_at,NEW.created_at);
END;
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
 WHEN indexed_reason_code IN ('forge_problem','budget_exhausted','approach_required','decision_required','repository_setup','environment','legacy_reconciliation',
   'membership_decision','external_change','verification_human','approval_required','observation_unavailable')
   OR (github_state='closed' AND state_reason IS NULL) THEN 'attention'
 WHEN indexed_reason_code IN ('recovery_pending','cleanup_pending','result_pending') THEN 'working'
 WHEN indexed_reason_code IS NOT NULL THEN 'waiting' WHEN active THEN 'working'
 WHEN EXISTS(SELECT 1 FROM next_actions n JOIN units u ON u.id=n.unit_id WHERE n.unit_id=classified.unit_id
 AND n.unit_version=u.version AND n.fencing_generation=u.fencing_generation AND n.membership_generation=u.membership_generation
 AND (n.candidate_id IS NULL OR n.candidate_id=u.current_candidate_id) AND (n.member_id IS NULL OR n.member_id=classified.member_id)
 AND (n.job_id IS NULL OR EXISTS(SELECT 1 FROM jobs j JOIN attempts assignment ON assignment.id=j.attempt_id JOIN units assigned_unit ON assigned_unit.id=j.unit_id JOIN job_members s ON s.job_id=j.id
 WHERE assignment.assignment_generation=assigned_unit.fencing_generation AND assignment.membership_generation=assigned_unit.membership_generation AND assignment.budget_generation=assigned_unit.budget_generation AND (j.candidate_id IS NULL OR j.candidate_id=assigned_unit.current_candidate_id) AND j.id=n.job_id AND j.outcome IS NULL AND (j.fenced=0 OR EXISTS(SELECT 1 FROM job_execution_state capture
 WHERE capture.job_id=j.id AND capture.capture_authorized=1 AND capture.result_revoked=0))
 AND NOT EXISTS(SELECT 1 FROM job_execution_state revoked WHERE revoked.job_id=j.id AND revoked.result_revoked=1)
 AND (n.reason NOT IN ('cleanup','cleanup_pending') OR j.cleanup=0) AND s.member_id=classified.member_id))) THEN 'queued'
 ELSE 'waiting' END AS indexed_status
FROM classified;

-- PR mergeability can wait before any merge effect is authorized.
CREATE TABLE candidate_merge_observations (
 candidate_id TEXT PRIMARY KEY NOT NULL REFERENCES candidates(id),
 state TEXT NOT NULL CHECK(state IN ('ready','conflict','unknown')),
 first_at REAL NOT NULL,
 observed_at REAL NOT NULL
) STRICT;
