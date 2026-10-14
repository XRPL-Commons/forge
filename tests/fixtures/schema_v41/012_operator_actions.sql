-- Authority and resumable external work for one authenticated operator request.
CREATE TABLE operator_action_authority (
    request_id TEXT PRIMARY KEY NOT NULL REFERENCES issue_action_requests(id),
    actor_id TEXT NOT NULL,
    unit_id TEXT REFERENCES units(id),
    expected_unit_version INTEGER CHECK(expected_unit_version IS NULL OR expected_unit_version>0),
    expected_issue_version INTEGER NOT NULL CHECK(expected_issue_version>0),
    payload_digest TEXT NOT NULL,
    gate_id TEXT,
    result_json TEXT CHECK(result_json IS NULL OR (json_valid(result_json) AND length(CAST(result_json AS BLOB))<=65536)),
    updated_at REAL NOT NULL
) STRICT;
CREATE INDEX one_pending_operator_action_per_issue ON issue_action_requests(issue_id) WHERE status='pending';
CREATE TABLE operator_action_effects (
    request_id TEXT NOT NULL REFERENCES issue_action_requests(id),
    step TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','confirmed','uncertain')),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json) AND length(CAST(payload_json AS BLOB))<=65536),
    receipt_json TEXT CHECK(receipt_json IS NULL OR (json_valid(receipt_json) AND length(CAST(receipt_json AS BLOB))<=65536)),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY(request_id,step)
) STRICT;
CREATE INDEX operator_action_effect_pending ON operator_action_effects(status,created_at,request_id);
CREATE TABLE operator_observation_requests (
    issue_id TEXT PRIMARY KEY NOT NULL REFERENCES issues(id),
    actor_id TEXT NOT NULL,
    requested_at REAL NOT NULL
) STRICT;

-- SQL filter/count authority for public issue pages, using the same gate order,
-- subject applicability and terminal rules as the canonical row projection.
CREATE VIEW public_issue_index AS
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
), blockers AS (
 SELECT f.id AS issue_id,g.code,g.created_at,g.id AS gate_id FROM facts f JOIN unit_gates g ON g.unit_id=f.unit_id
 WHERE f.current=1 AND g.resolved_at IS NULL AND (g.candidate_id IS NULL OR g.candidate_id=f.current_candidate_id)
 AND (g.member_id IS NULL OR g.member_id=f.member_id OR f.unit_state NOT IN ('queued','building'))
 UNION ALL SELECT id,'repository_setup',0,'derived:repository' FROM facts WHERE current=1 AND enabled=0
 UNION ALL SELECT id,'membership_decision',0,'derived:requirements' FROM facts WHERE current=1 AND current_requirements!=pinned_requirements
 UNION ALL SELECT id,'external_change',0,'derived:closed' FROM facts WHERE current=1 AND github_state='closed'
 UNION ALL SELECT f.id,'dependency',0,'derived:dependency' FROM facts f WHERE f.current=1 AND (
   (EXISTS(SELECT 1 FROM issue_dependencies d WHERE d.issue_id=f.id AND d.requirements_revision_id=f.requirements_revision_id)
    AND NOT COALESCE((SELECT clear FROM dependency_checks c WHERE c.member_id=f.member_id AND c.requirements_revision_id=f.requirements_revision_id ORDER BY generation DESC LIMIT 1),0))
   OR EXISTS(SELECT 1 FROM member_dependencies d WHERE d.unit_id=f.unit_id AND d.member_id=f.member_id AND d.membership_generation=f.membership_generation))
), ranked AS (
 SELECT b.*,ROW_NUMBER() OVER(PARTITION BY issue_id ORDER BY c.priority,c.rowid,b.created_at,b.gate_id) AS rank
 FROM blockers b JOIN gate_codes c ON c.code=b.code
), classified AS (
 SELECT f.*,b.code AS reason_code,
 CASE WHEN f.github_state='closed' AND f.state_reason='completed' THEN 'done'
      WHEN f.github_state='closed' AND f.state_reason IN ('not_planned','duplicate') THEN 'dropped'
      WHEN f.github_state='closed' THEN 'blocked'
      WHEN f.unit_state='merged' AND ((f.completion_issue_version IS NOT NULL AND f.reopened_version<=f.completion_issue_version)
        OR (f.completion_issue_version IS NULL AND f.reopened_version=0 AND COALESCE(f.state_reason,'')!='reopened')) THEN 'done'
      WHEN f.current=0 THEN 'untracked' ELSE 'current' END AS issue_state,
 EXISTS(SELECT 1 FROM jobs j WHERE j.unit_id=f.unit_id AND j.cleanup=0 AND j.fenced=0 AND j.outcome IS NULL) AS active
 FROM facts f LEFT JOIN ranked b ON b.issue_id=f.id AND b.rank=1
)
SELECT *,CASE WHEN issue_state='done' THEN 'completed' WHEN issue_state='dropped' THEN 'dropped'
 WHEN issue_state='untracked' OR reason_code IN ('operator_pause','operator_backlog') THEN 'backlog'
 WHEN reason_code IN ('dependency','label_hold','ownership_conflict') THEN 'blocked'
 WHEN reason_code IN ('budget_exhausted','approach_required','decision_required','repository_setup','environment','legacy_reconciliation',
   'membership_decision','external_change','verification_human','approval_required','observation_unavailable')
   OR (github_state='closed' AND state_reason IS NULL) THEN 'attention'
 WHEN reason_code IS NOT NULL THEN 'waiting' WHEN active THEN 'working' ELSE 'queued' END AS public_status
FROM classified;

CREATE TABLE candidate_reviewer_requests (
 candidate_id TEXT NOT NULL REFERENCES candidates(id),
 request_id TEXT NOT NULL,
 actor_id TEXT NOT NULL,
 logins_json TEXT NOT NULL CHECK(json_valid(logins_json) AND json_type(logins_json)='array' AND length(CAST(logins_json AS BLOB))<=4096),
 requested_at REAL NOT NULL,
 issue_assignment INTEGER NOT NULL CHECK(issue_assignment IN (0,1)),
 PRIMARY KEY(candidate_id,request_id)
) STRICT;
CREATE TABLE operator_verification_authority (
 request_id TEXT PRIMARY KEY NOT NULL,
 actor_id TEXT NOT NULL,
 issue_id TEXT NOT NULL REFERENCES issues(id),
 unit_id TEXT NOT NULL REFERENCES units(id),
 expected_unit_version INTEGER NOT NULL CHECK(expected_unit_version>0),
 expected_revision TEXT NOT NULL,
 gate_id TEXT NOT NULL REFERENCES unit_gates(id),
 statement_digest TEXT NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE job_public_metadata (
 job_id TEXT PRIMARY KEY NOT NULL REFERENCES jobs(id),
 slot TEXT,
 provider TEXT,
 provider_id TEXT,
 model TEXT,
 deadline REAL,
 observed_at REAL,
 started_at REAL,
 source_phase TEXT,
 source_fenced INTEGER CHECK(source_fenced IS NULL OR source_fenced IN (0,1))
) STRICT;
