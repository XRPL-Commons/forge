-- A rejected request is a receipt, not an independent external-change hold.
-- Genuine candidate-change gates (without request_id) retain their authority.
CREATE TEMP TABLE rejected_request_gates AS
SELECT id,unit_id FROM unit_gates WHERE code='external_change' AND resolved_at IS NULL
 AND json_type(details_json,'$.request_id')='text'
 AND length(json_extract(details_json,'$.request_id'))>0;
UPDATE unit_gates SET resolved_at=MAX(created_at,CAST(strftime('%s','now') AS REAL)),resolution='superseded'
 WHERE id IN (SELECT id FROM rejected_request_gates);
UPDATE units SET primary_gate_id=NULL,version=version+1
 WHERE id IN (SELECT unit_id FROM rejected_request_gates);
INSERT INTO events(transaction_id,unit_id,kind,at,actor_id,payload_json)
 SELECT 'migration:035:'||id,unit_id,'gate_resolved',CAST(strftime('%s','now') AS REAL),'migration:035',
 json_object('gate_id',id,'resolution','superseded','evidence',json_object('reason','rejected_operator_request'))
 FROM rejected_request_gates;
DROP TABLE rejected_request_gates;

-- Observation may release a genuine hold only after validating current facts.
DROP TRIGGER gate_codes_no_update;
UPDATE gate_codes SET release='observation' WHERE code='external_change';
CREATE TRIGGER gate_codes_no_update BEFORE UPDATE ON gate_codes
 BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
