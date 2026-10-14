-- Explicit, atomic upgrade: exact UTC microseconds, including offsets and Z.
-- The migration runner registers forge_remote_time; it fails the transaction
-- for invalid stored timestamps. Historical source archives remain unchanged.
DROP TRIGGER issues_observed_in_order;
UPDATE issues SET remote_updated_at=forge_remote_time(remote_updated_at)
 WHERE remote_updated_at IS NOT NULL;
UPDATE pull_requests SET remote_updated_at=forge_remote_time(remote_updated_at);

-- Canonical values have one byte ordering, so the database fence is exact even
-- for two observations less than one millisecond apart. No UDF in schema code.
CREATE TRIGGER issues_timestamp_insert BEFORE INSERT ON issues
WHEN NEW.remote_updated_at IS NOT NULL AND
 (NEW.remote_updated_at NOT GLOB '????-??-??T??:??:??.??????+00:00' OR julianday(NEW.remote_updated_at) IS NULL)
BEGIN SELECT RAISE(ABORT,'GitHub timestamp must use canonical UTC microseconds'); END;
CREATE TRIGGER issues_observed_in_order BEFORE UPDATE ON issues
WHEN (NEW.remote_updated_at IS NOT NULL AND
       (NEW.remote_updated_at NOT GLOB '????-??-??T??:??:??.??????+00:00' OR julianday(NEW.remote_updated_at) IS NULL))
    OR (OLD.remote_updated_at IS NOT NULL AND NEW.remote_updated_at IS NULL)
    OR NEW.remote_updated_at < OLD.remote_updated_at
    OR (NEW.remote_updated_at IS OLD.remote_updated_at AND NEW.collection_generation < OLD.collection_generation)
BEGIN SELECT RAISE(ABORT,'an older or noncanonical observation cannot replace a newer one'); END;
CREATE TRIGGER pull_requests_timestamp_insert BEFORE INSERT ON pull_requests
WHEN NEW.remote_updated_at NOT GLOB '????-??-??T??:??:??.??????+00:00' OR julianday(NEW.remote_updated_at) IS NULL
BEGIN SELECT RAISE(ABORT,'GitHub timestamp must use canonical UTC microseconds'); END;
CREATE TRIGGER pull_requests_timestamp_update BEFORE UPDATE OF remote_updated_at ON pull_requests
WHEN NEW.remote_updated_at NOT GLOB '????-??-??T??:??:??.??????+00:00' OR julianday(NEW.remote_updated_at) IS NULL
BEGIN SELECT RAISE(ABORT,'GitHub timestamp must use canonical UTC microseconds'); END;

-- A discovery failure is an observation on one issue, not a global controller
-- failure and not a lifecycle unit fabricated before admission is safe.
CREATE TABLE issue_discovery_observations (
 issue_id TEXT PRIMARY KEY NOT NULL REFERENCES issues(id),
 revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
 reason_code TEXT NOT NULL CHECK(reason_code IN ('requirements_stale','remote_timestamp_invalid','admission_conflict')),
 observed_at REAL NOT NULL,
 retry_after REAL NOT NULL CHECK(retry_after>=observed_at)
) STRICT;

DROP VIEW public_issue_index;
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
 SELECT f.*,COALESCE(b.code,CASE WHEN f.current=0 AND f.github_state='open' THEN d.reason_code END) AS reason_code,
 CASE WHEN f.github_state='closed' AND f.state_reason='completed' THEN 'done'
      WHEN f.github_state='closed' AND f.state_reason IN ('not_planned','duplicate') THEN 'dropped'
      WHEN f.github_state='closed' THEN 'blocked'
      WHEN f.unit_state='merged' AND ((f.completion_issue_version IS NOT NULL AND f.reopened_version<=f.completion_issue_version)
        OR (f.completion_issue_version IS NULL AND f.reopened_version=0 AND COALESCE(f.state_reason,'')!='reopened')) THEN 'done'
      WHEN f.current=0 THEN 'untracked' ELSE 'current' END AS issue_state,
 EXISTS(SELECT 1 FROM jobs j WHERE j.unit_id=f.unit_id AND j.cleanup=0 AND j.fenced=0 AND j.outcome IS NULL) AS active
 FROM facts f LEFT JOIN ranked b ON b.issue_id=f.id AND b.rank=1
 LEFT JOIN issues observed ON observed.id=f.id
 LEFT JOIN issue_discovery_observations d ON d.issue_id=f.id AND d.revision_id=observed.current_revision_id
)
SELECT *,CASE WHEN issue_state='done' THEN 'completed' WHEN issue_state='dropped' THEN 'dropped'
 WHEN reason_code IN ('requirements_stale','remote_timestamp_invalid','admission_conflict') THEN 'attention'
 WHEN issue_state='untracked' OR reason_code IN ('operator_pause','operator_backlog') THEN 'backlog'
 WHEN reason_code IN ('dependency','label_hold','ownership_conflict') THEN 'blocked'
 WHEN reason_code IN ('budget_exhausted','approach_required','decision_required','repository_setup','environment','legacy_reconciliation',
   'membership_decision','external_change','verification_human','approval_required','observation_unavailable')
   OR (github_state='closed' AND state_reason IS NULL) THEN 'attention'
 WHEN reason_code IS NOT NULL THEN 'waiting' WHEN active THEN 'working' ELSE 'queued' END AS public_status
FROM classified;
