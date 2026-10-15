-- One durable outbox. Old pending operator rows may already have been sent.
ALTER TABLE external_effects ADD COLUMN operator_request_id TEXT REFERENCES issue_action_requests(id);
ALTER TABLE external_effects ADD COLUMN operator_step TEXT;
CREATE UNIQUE INDEX external_operator_step ON external_effects(operator_request_id,operator_step)
WHERE operator_request_id IS NOT NULL;

INSERT INTO external_effects(id,request_id,unit_id,kind,status,payload_json,receipt_json,
 attempts,created_at,updated_at,uncertain_since,operator_request_id,operator_step)
SELECT 'operator:'||e.request_id||':'||e.step,'operator:'||e.request_id||':'||e.step,
 a.unit_id,'operator_action',CASE WHEN e.status='confirmed' THEN 'confirmed' ELSE 'uncertain' END,
 e.payload_json,e.receipt_json,1,e.created_at,e.updated_at,
 CASE WHEN e.status!='confirmed' THEN e.created_at END,e.request_id,e.step
FROM operator_action_effects e LEFT JOIN operator_action_authority a ON a.request_id=e.request_id;

CREATE TRIGGER operator_effect_identity BEFORE UPDATE ON external_effects
WHEN OLD.operator_request_id IS NOT NULL AND
 (NEW.id IS NOT OLD.id OR NEW.request_id IS NOT OLD.request_id OR NEW.unit_id IS NOT OLD.unit_id
  OR NEW.kind IS NOT OLD.kind OR NEW.operator_request_id IS NOT OLD.operator_request_id
  OR NEW.operator_step IS NOT OLD.operator_step OR NEW.payload_json IS NOT OLD.payload_json
  OR NEW.created_at IS NOT OLD.created_at
  OR OLD.status='confirmed' AND (NEW.status IS NOT OLD.status OR NEW.receipt_json IS NOT OLD.receipt_json))
BEGIN SELECT RAISE(ABORT,'operator effect authority is immutable'); END;
CREATE TRIGGER operator_effect_retained BEFORE DELETE ON external_effects
WHEN OLD.operator_request_id IS NOT NULL
BEGIN SELECT RAISE(ABORT,'operator effect history is retained'); END;
DROP TABLE operator_action_effects;

-- Preserve legacy registry facts while making the four classes authoritative.
DROP TRIGGER gate_codes_no_update;
ALTER TABLE gate_codes RENAME COLUMN kind TO legacy_kind;
ALTER TABLE gate_codes ADD COLUMN kind TEXT NOT NULL DEFAULT 'transitional'
 CHECK(kind IN ('transitional','supervisor-remedy','operator-choice','external'));
ALTER TABLE gate_codes ADD COLUMN owner TEXT NOT NULL DEFAULT 'core'
 CHECK(owner IN ('core','supervisor','operator','external'));
ALTER TABLE gate_codes ADD COLUMN public_status TEXT NOT NULL DEFAULT 'waiting'
 CHECK(public_status IN ('backlog','blocked','attention','working','waiting'));
-- BEGIN GENERATED GATE REGISTRY
UPDATE gate_codes SET kind='operator-choice',priority=2,release='operator',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='operator',public_status='backlog' WHERE code='operator_pause';
UPDATE gate_codes SET kind='operator-choice',priority=2,release='operator',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='operator',public_status='backlog' WHERE code='operator_backlog';
UPDATE gate_codes SET kind='external',priority=2,release='observation',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='external',public_status='blocked' WHERE code='label_hold';
UPDATE gate_codes SET kind='operator-choice',priority=2,release='operator',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='operator',public_status='attention' WHERE code='membership_decision';
UPDATE gate_codes SET kind='transitional',priority=2,release='observation',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='core',public_status='working' WHERE code='operator_sync';
UPDATE gate_codes SET kind='transitional',priority=2,release='observation',unit_scope=0,member_scope=1,candidate_scope=0,job_scope=0,owner='core',public_status='working' WHERE code='triage_sync';
UPDATE gate_codes SET kind='supervisor-remedy',priority=2,release='observation',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='supervisor',public_status='attention' WHERE code='external_change';
UPDATE gate_codes SET kind='external',priority=2,release='observation',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='external',public_status='blocked' WHERE code='ownership_conflict';
UPDATE gate_codes SET kind='operator-choice',priority=3,release='operator',unit_scope=1,member_scope=0,candidate_scope=0,job_scope=0,owner='operator',public_status='attention' WHERE code='budget_exhausted';
UPDATE gate_codes SET kind='supervisor-remedy',priority=3,release='operator',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='supervisor',public_status='attention' WHERE code='approach_required';
UPDATE gate_codes SET kind='operator-choice',priority=3,release='operator',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=1,owner='operator',public_status='attention' WHERE code='decision_required';
UPDATE gate_codes SET kind='supervisor-remedy',priority=4,release='observation',unit_scope=1,member_scope=1,candidate_scope=1,job_scope=1,owner='supervisor',public_status='attention' WHERE code='forge_problem';
UPDATE gate_codes SET kind='external',priority=4,release='observation',unit_scope=1,member_scope=0,candidate_scope=0,job_scope=0,owner='external',public_status='attention' WHERE code='repository_setup';
UPDATE gate_codes SET kind='external',priority=4,release='recheck',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=1,owner='external',public_status='attention' WHERE code='environment';
UPDATE gate_codes SET kind='supervisor-remedy',priority=4,release='observation',unit_scope=1,member_scope=1,candidate_scope=1,job_scope=0,owner='supervisor',public_status='attention' WHERE code='observation_unavailable';
UPDATE gate_codes SET kind='operator-choice',priority=4,release='operator',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='operator',public_status='attention' WHERE code='legacy_reconciliation';
UPDATE gate_codes SET kind='external',priority=5,release='observation',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=1,owner='external',public_status='blocked' WHERE code='dependency';
UPDATE gate_codes SET kind='transitional',priority=6,release='transition',unit_scope=1,member_scope=1,candidate_scope=1,job_scope=1,owner='core',public_status='working' WHERE code='recovery_pending';
UPDATE gate_codes SET kind='transitional',priority=6,release='transition',unit_scope=1,member_scope=1,candidate_scope=1,job_scope=1,owner='core',public_status='working' WHERE code='cleanup_pending';
UPDATE gate_codes SET kind='transitional',priority=6,release='transition',unit_scope=1,member_scope=1,candidate_scope=1,job_scope=1,owner='core',public_status='working' WHERE code='result_pending';
UPDATE gate_codes SET kind='external',priority=6,release='time',unit_scope=1,member_scope=0,candidate_scope=0,job_scope=0,owner='external',public_status='waiting' WHERE code='provider_unavailable';
UPDATE gate_codes SET kind='supervisor-remedy',priority=6,release='time',unit_scope=1,member_scope=1,candidate_scope=0,job_scope=0,owner='supervisor',public_status='waiting' WHERE code='retry_delay';
UPDATE gate_codes SET kind='external',priority=7,release='evidence',unit_scope=0,member_scope=0,candidate_scope=1,job_scope=0,owner='external',public_status='attention' WHERE code='verification_human';
UPDATE gate_codes SET kind='external',priority=7,release='evidence',unit_scope=0,member_scope=0,candidate_scope=1,job_scope=0,owner='external',public_status='waiting' WHERE code='verification_ci';
UPDATE gate_codes SET kind='external',priority=7,release='evidence',unit_scope=0,member_scope=0,candidate_scope=1,job_scope=0,owner='external',public_status='attention' WHERE code='approval_required';
UPDATE gate_codes SET kind='external',priority=7,release='observation',unit_scope=0,member_scope=0,candidate_scope=1,job_scope=0,owner='external',public_status='waiting' WHERE code='merge_checks';
-- END GENERATED GATE REGISTRY
CREATE TRIGGER gate_codes_no_update BEFORE UPDATE ON gate_codes
 BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;

-- Replace unit phases, preserving histories.
DROP TRIGGER close_codes_no_insert;
DROP TRIGGER unit_states_no_insert;
DROP TRIGGER unit_states_no_delete;
DROP TRIGGER unit_transitions_no_insert;
DROP TRIGGER unit_transitions_no_delete;
DROP TRIGGER unit_transition_legal;
DROP TRIGGER unit_terminal_facts_fixed;
INSERT INTO unit_states VALUES('publishing',0);
INSERT INTO close_codes VALUES('dissolved',NULL);
CREATE TRIGGER close_codes_no_insert BEFORE INSERT ON close_codes BEGIN
 SELECT RAISE(ABORT,'registry changes require a migration');
END;
CREATE TEMP TABLE mapped_unit_transitions AS SELECT DISTINCT CASE from_state WHEN 'queued' THEN 'building' WHEN 'integrating' THEN 'building' WHEN 'dissolved' THEN 'closed' ELSE from_state END AS from_state, CASE to_state WHEN 'queued' THEN 'building' WHEN 'integrating' THEN 'building' WHEN 'dissolved' THEN 'closed' ELSE to_state END AS to_state FROM unit_transitions;
DELETE FROM unit_transitions;
INSERT INTO unit_transitions SELECT from_state,to_state FROM mapped_unit_transitions WHERE from_state!=to_state;
DROP TABLE mapped_unit_transitions;
INSERT INTO unit_transitions VALUES('building','publishing'),('publishing','building'),('publishing','reviewing'),('publishing','closed'),('publishing','merged');
UPDATE units SET state='building' WHERE state IN('queued','integrating');
UPDATE units SET state='closed',close_code='dissolved' WHERE state='dissolved';
UPDATE units SET state='publishing' WHERE state='building' AND EXISTS(SELECT 1 FROM publication_intents p JOIN jobs j ON j.id=p.job_id WHERE p.unit_id=units.id AND p.status!='superseded' AND j.outcome IS NULL);
DELETE FROM unit_states WHERE code IN('queued','integrating','dissolved');
CREATE TRIGGER unit_states_no_insert BEFORE INSERT ON unit_states BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_states_no_delete BEFORE DELETE ON unit_states BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_transitions_no_insert BEFORE INSERT ON unit_transitions BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_transitions_no_delete BEFORE DELETE ON unit_transitions BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;
CREATE TRIGGER unit_transition_legal BEFORE UPDATE OF state ON units
WHEN NEW.state != OLD.state AND NOT EXISTS (
    SELECT 1 FROM unit_transitions WHERE from_state=OLD.state AND to_state=NEW.state)
BEGIN SELECT RAISE(ABORT, 'illegal unit transition'); END;
CREATE TRIGGER unit_terminal_facts_fixed BEFORE UPDATE ON units
WHEN OLD.state IN ('merged','closed','dissolved') AND
    (NEW.close_code IS NOT OLD.close_code OR NEW.merge_sha IS NOT OLD.merge_sha OR NEW.current_candidate_id IS NOT OLD.current_candidate_id)
BEGIN SELECT RAISE(ABORT, 'terminal delivery facts are immutable'); END;

-- The SQL issue index uses the same gate registry as the Python and browser views.
DROP VIEW issue_selection_facts;
CREATE VIEW issue_selection_facts AS
WITH latest AS (
 SELECT i.id AS issue_id,(SELECT m.id FROM unit_issues m WHERE m.issue_id=i.id
   ORDER BY (m.left_at IS NULL) DESC,m.joined_at DESC,m.id DESC LIMIT 1) AS member_id FROM issues i
), facts AS (
 SELECT i.id,i.repository_id,i.github_number,i.github_state,i.state_reason,i.reopened_version,
   m.id AS member_id,m.left_at,m.completion_issue_version,m.requirements_revision_id,
   u.id AS unit_id,u.state AS unit_state,u.current_candidate_id,u.membership_generation,
   CASE WHEN m.left_at IS NULL AND u.state NOT IN ('merged','closed') THEN 1 ELSE 0 END AS current,
   r.enabled,ir.requirements_hash AS current_requirements,pinned.requirements_hash AS pinned_requirements
 FROM issues i JOIN repositories r ON r.id=i.repository_id JOIN latest l ON l.issue_id=i.id
 JOIN issue_revisions ir ON ir.id=i.current_revision_id LEFT JOIN unit_issues m ON m.id=l.member_id
 LEFT JOIN units u ON u.id=m.unit_id LEFT JOIN issue_revisions pinned ON pinned.id=m.requirements_revision_id
), classified AS (
 SELECT f.*,COALESCE((SELECT b.code FROM (
 SELECT f.id AS issue_id,g.code,g.created_at,g.id AS gate_id FROM unit_gates g
 WHERE g.unit_id=f.unit_id AND f.current=1 AND g.resolved_at IS NULL AND (g.candidate_id IS NULL OR g.candidate_id=f.current_candidate_id)
 AND (g.job_id IS NULL OR CASE WHEN
   (json_extract(g.details_json,'$.source')='build_claim' AND g.code IN ('dependency','decision_required')) OR (json_extract(g.details_json,'$.source')='worker_claim' AND g.code='environment')
 THEN EXISTS(SELECT 1 FROM json_each(g.details_json,'$.requirements_by_member') pin
   WHERE pin.key=f.member_id AND pin.value=f.requirements_revision_id)
 ELSE EXISTS(SELECT 1 FROM job_members scope WHERE scope.job_id=g.job_id AND scope.member_id=f.member_id) END)
 AND (g.member_id IS NULL OR g.member_id=f.member_id OR f.unit_state!='building')
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
 WHEN issue_state='untracked' THEN 'backlog'
 WHEN indexed_reason_code IS NOT NULL THEN COALESCE((SELECT public_status FROM gate_codes WHERE code=indexed_reason_code),'waiting')
 WHEN github_state='closed' AND state_reason IS NULL THEN 'attention'
 WHEN active THEN 'working' WHEN EXISTS(SELECT 1 FROM next_actions n JOIN units u ON u.id=n.unit_id WHERE n.unit_id=classified.unit_id
 AND n.unit_version=u.version AND n.fencing_generation=u.fencing_generation AND n.membership_generation=u.membership_generation
 AND (n.candidate_id IS NULL OR n.candidate_id=u.current_candidate_id) AND (n.member_id IS NULL OR n.member_id=classified.member_id)
 AND (n.job_id IS NULL OR EXISTS(SELECT 1 FROM jobs j JOIN attempts assignment ON assignment.id=j.attempt_id JOIN units assigned_unit ON assigned_unit.id=j.unit_id JOIN job_members s ON s.job_id=j.id
 WHERE assignment.assignment_generation=assigned_unit.fencing_generation AND assignment.membership_generation=assigned_unit.membership_generation AND assignment.budget_generation=assigned_unit.budget_generation AND (j.candidate_id IS NULL OR j.candidate_id=assigned_unit.current_candidate_id) AND j.id=n.job_id AND j.outcome IS NULL AND (j.fenced=0 OR EXISTS(SELECT 1 FROM job_execution_state capture
 WHERE capture.job_id=j.id AND capture.capture_authorized=1 AND capture.result_revoked=0))
 AND NOT EXISTS(SELECT 1 FROM job_execution_state revoked WHERE revoked.job_id=j.id AND revoked.result_revoked=1)
 AND (n.reason NOT IN ('cleanup','cleanup_pending') OR j.cleanup=0) AND s.member_id=classified.member_id))) THEN 'queued'
 ELSE 'waiting' END AS indexed_status
FROM classified;

-- One retained decision contract; domain stores retain selection/application authority.
CREATE TABLE steward_subject_decisions (
 subject_kind TEXT NOT NULL CHECK(subject_kind IN ('proposal_row','conversation_turn')),
 subject_id TEXT NOT NULL,
 subject_version INTEGER NOT NULL CHECK(subject_version>0),
 execution_id TEXT NOT NULL,
 dossier_json TEXT NOT NULL CHECK(json_valid(dossier_json) AND length(CAST(dossier_json AS BLOB))<=524288),
 decision_json TEXT NOT NULL CHECK(json_valid(decision_json) AND length(CAST(decision_json AS BLOB))<=65536),
 digest TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(subject_kind,subject_id,subject_version,execution_id)
) STRICT;
CREATE TRIGGER steward_subject_decisions_no_update BEFORE UPDATE ON steward_subject_decisions
 BEGIN SELECT RAISE(ABORT,'steward subject decision is immutable'); END;
CREATE TRIGGER steward_subject_decisions_no_delete BEFORE DELETE ON steward_subject_decisions
 BEGIN SELECT RAISE(ABORT,'steward subject decision is retained'); END;

ALTER TABLE triage_proposal_rows ADD COLUMN steward_choice_id TEXT;
