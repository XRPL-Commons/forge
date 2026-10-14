-- Retire the state imported from the JSON runtime (#268, cleanup 3e).
--
-- The code that reconciled imported deliveries is removed in the same release.
-- A live delivery still waiting on its open legacy_reconciliation gate is
-- closed as import_superseded: its gates resolve as superseded and its member
-- leaves, exactly as a terminal transition records them. GitHub is not touched,
-- so the issue and its labels stay as they are, and discovery admits the open
-- issue again as a new delivery. A delivery with an uncleaned job is left alone.
--
-- Every statement is a no-op when nothing imported remains, so applying this
-- script to an already repaired or a fresh store changes nothing.

DROP TRIGGER close_codes_no_insert;
INSERT OR IGNORE INTO close_codes(code,issue_outcome) VALUES('import_superseded',NULL);
CREATE TRIGGER close_codes_no_insert BEFORE INSERT ON close_codes BEGIN
    SELECT RAISE(ABORT, 'registry changes require a migration');
END;

CREATE TEMP TABLE import_superseded_041 AS
SELECT u.id AS unit_id,'migration-041:'||u.id AS transaction_id,(julianday('now')-2440587.5)*86400.0 AS at
FROM units u
WHERE u.state NOT IN ('merged','closed','dissolved')
  AND EXISTS(SELECT 1 FROM unit_gates g WHERE g.unit_id=u.id AND g.code='legacy_reconciliation' AND g.resolved_at IS NULL)
  AND NOT EXISTS(SELECT 1 FROM jobs j WHERE j.unit_id=u.id AND j.cleanup=0)
  AND EXISTS(SELECT 1 FROM unit_transitions t WHERE t.from_state=u.state AND t.to_state='closed');

INSERT INTO events(transaction_id,unit_id,issue_id,job_id,kind,at,actor_id,payload_json)
SELECT s.transaction_id,g.unit_id,NULL,NULL,'gate_resolved',s.at,'forge:migration-041',
       json_object('gate_id',g.id,'resolution','superseded','evidence',json_object('import_superseded',json('true'),
       'reason','Imported delivery retired with the import reconciliation code; the issue is admitted again as a new delivery.'))
FROM import_superseded_041 s JOIN unit_gates g ON g.unit_id=s.unit_id AND g.resolved_at IS NULL
ORDER BY g.unit_id,g.created_at,g.id;
UPDATE unit_gates SET resolved_at=(SELECT at FROM import_superseded_041 s WHERE s.unit_id=unit_gates.unit_id),resolution='superseded'
WHERE resolved_at IS NULL AND unit_id IN (SELECT unit_id FROM import_superseded_041);

INSERT INTO events(transaction_id,unit_id,issue_id,job_id,kind,at,actor_id,payload_json)
SELECT s.transaction_id,m.unit_id,m.issue_id,NULL,'membership_changed',s.at,'forge:migration-041',
       json_object('member_id',m.id,'exit_reason','closed')
FROM import_superseded_041 s JOIN unit_issues m ON m.unit_id=s.unit_id AND m.left_at IS NULL
ORDER BY m.unit_id,m.id;
UPDATE unit_issues SET state='removed',left_at=(SELECT at FROM import_superseded_041 s WHERE s.unit_id=unit_issues.unit_id),
       exit_reason='closed',completion_issue_version=(SELECT version FROM issues WHERE id=unit_issues.issue_id)
WHERE left_at IS NULL AND unit_id IN (SELECT unit_id FROM import_superseded_041);

UPDATE units SET state='closed',close_code='import_superseded',primary_gate_id=NULL,
       fencing_generation=fencing_generation+1,version=version+1,
       updated_at=(SELECT at FROM import_superseded_041 s WHERE s.unit_id=units.id)
WHERE id IN (SELECT unit_id FROM import_superseded_041);
INSERT INTO events(transaction_id,unit_id,issue_id,job_id,kind,at,actor_id,payload_json)
SELECT transaction_id,unit_id,NULL,NULL,'closed',at,'forge:migration-041',json_object('close_code','import_superseded','merge_sha',NULL)
FROM import_superseded_041 ORDER BY unit_id;

DROP TABLE import_superseded_041;

-- Imported failed attempts whose investigation never finished belong to ended
-- deliveries; nothing can investigate them any more.
UPDATE attempt_failures SET investigation='complete'
WHERE (investigation IS NULL OR investigation!='complete')
  AND EXISTS(SELECT 1 FROM units u WHERE u.id=attempt_failures.unit_id AND u.state IN ('merged','closed','dissolved'))
  AND EXISTS(SELECT 1 FROM attempts a WHERE a.id=attempt_failures.attempt_id
             AND json_extract(a.input_manifest_json,'$.legacy_job_id') IS NOT NULL);

-- Imported operator requests never gained runtime authority; nothing applies them.
UPDATE issue_action_requests SET status='rejected',message='Retired with the imported state; review the current state and ask again.'
WHERE status='pending'
  AND EXISTS(SELECT 1 FROM imported_operator_requests p WHERE p.request_id=issue_action_requests.id)
  AND NOT EXISTS(SELECT 1 FROM operator_action_authority a WHERE a.request_id=issue_action_requests.id);
