-- Indexed selection facts are not a second dependency/output evaluator.
-- A snapshot-scoped canonical selector resolves dependency-bearing deliveries
-- and approved grouped deliveries through the same lifecycle projector used
-- by issue cards before filtering, counting or paging. The indexed columns
-- deliberately do not claim final public status; callers use that selector.
DROP VIEW public_issue_index;
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
 EXISTS(SELECT 1 FROM jobs j WHERE j.unit_id=f.unit_id AND j.cleanup=0 AND j.fenced=0 AND j.outcome IS NULL) AS active
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
