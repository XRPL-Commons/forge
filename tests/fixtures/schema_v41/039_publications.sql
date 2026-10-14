-- Publication owns captured build output before there is a candidate.
CREATE TABLE publication_intents (
 id TEXT PRIMARY KEY NOT NULL,
 job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
 unit_id TEXT NOT NULL REFERENCES units(id),
 execution_fence TEXT NOT NULL,
 input_manifest_json TEXT NOT NULL CHECK(json_valid(input_manifest_json)),
 output_digest TEXT NOT NULL CHECK(length(output_digest)=64),
 output_json TEXT NOT NULL CHECK(json_valid(output_json) AND length(CAST(output_json AS BLOB))<=2097152),
 target_json TEXT NOT NULL CHECK(json_valid(target_json)),
 report_json TEXT NOT NULL CHECK(json_valid(report_json) AND length(CAST(report_json AS BLOB))<=65536),
 status TEXT NOT NULL CHECK(status IN('pending','working','uncertain','blocked','confirmed','superseded')),
 claim_json TEXT CHECK(claim_json IS NULL OR json_valid(claim_json)),
 version INTEGER NOT NULL DEFAULT 1,
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL
) STRICT;
CREATE TABLE publication_steps (
 intent_id TEXT NOT NULL REFERENCES publication_intents(id),
 key TEXT NOT NULL,
 request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
 status TEXT NOT NULL CHECK(status IN('prepared','sending','confirmed','refused')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts>=0),
 retry_at REAL,
 uncertain_since REAL,
 escalation_at REAL,
 receipt_json TEXT CHECK(receipt_json IS NULL OR json_valid(receipt_json)),
 created_at REAL NOT NULL,
 updated_at REAL NOT NULL,
 PRIMARY KEY(intent_id,key)
) STRICT;
CREATE TABLE publication_receipts (
 intent_id TEXT PRIMARY KEY NOT NULL REFERENCES publication_intents(id),
 receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json) AND length(CAST(receipt_json AS BLOB))<=65536),
 observed_at REAL NOT NULL
) STRICT;
CREATE INDEX publication_pending ON publication_intents(status,updated_at,id);
CREATE TRIGGER publication_identity_immutable BEFORE UPDATE ON publication_intents
WHEN NEW.id!=OLD.id OR NEW.job_id!=OLD.job_id OR NEW.unit_id!=OLD.unit_id OR NEW.execution_fence!=OLD.execution_fence
 OR NEW.input_manifest_json!=OLD.input_manifest_json OR NEW.output_digest!=OLD.output_digest OR NEW.output_json!=OLD.output_json
 OR NEW.target_json!=OLD.target_json OR NEW.report_json!=OLD.report_json OR NEW.created_at!=OLD.created_at
BEGIN SELECT RAISE(ABORT,'publication identity is immutable'); END;
CREATE TRIGGER publication_step_identity_immutable BEFORE UPDATE ON publication_steps
WHEN NEW.intent_id!=OLD.intent_id OR NEW.key!=OLD.key OR NEW.request_digest!=OLD.request_digest OR NEW.created_at!=OLD.created_at
 OR OLD.status='confirmed' AND (NEW.status!='confirmed' OR NEW.receipt_json!=OLD.receipt_json)
BEGIN SELECT RAISE(ABORT,'publication step evidence is immutable'); END;
CREATE TRIGGER publication_receipt_no_update BEFORE UPDATE ON publication_receipts
BEGIN SELECT RAISE(ABORT,'publication receipt is immutable'); END;
CREATE TRIGGER publication_receipt_no_delete BEFORE DELETE ON publication_receipts
BEGIN SELECT RAISE(ABORT,'publication receipt is retained'); END;
CREATE TRIGGER publication_intent_no_delete BEFORE DELETE ON publication_intents
BEGIN SELECT RAISE(ABORT,'publication intent is retained'); END;
CREATE TRIGGER publication_step_no_delete BEFORE DELETE ON publication_steps
BEGIN SELECT RAISE(ABORT,'publication step is retained'); END;
