-- Mirror authority belongs to a physical execution, including auxiliary roles.
-- Job identities are namespaced by the runtime adapter, not forced into jobs.
CREATE TABLE mirror_policies (
 digest TEXT PRIMARY KEY CHECK(length(digest)=64),
 policy_json TEXT NOT NULL CHECK(json_valid(policy_json)),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE mirror_sources (
 digest TEXT PRIMARY KEY CHECK(length(digest)=64),
 policy_digest TEXT NOT NULL REFERENCES mirror_policies(digest),
 source_json TEXT NOT NULL CHECK(json_valid(source_json)),
 created_at REAL NOT NULL,
 UNIQUE(digest,policy_digest)
) STRICT;
CREATE TRIGGER mirror_sources_immutable BEFORE UPDATE ON mirror_sources
 BEGIN SELECT RAISE(ABORT,'mirror source is immutable'); END;
CREATE TRIGGER mirror_sources_retained BEFORE DELETE ON mirror_sources
 BEGIN SELECT RAISE(ABORT,'mirror source is retained'); END;
CREATE TABLE mirror_sessions (
 id TEXT PRIMARY KEY,
 job_id TEXT NOT NULL,
 execution_fence TEXT NOT NULL,
 network_generation TEXT NOT NULL,
 source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
 policy_digest TEXT NOT NULL REFERENCES mirror_policies(digest),
 deadline REAL NOT NULL,
 created_at REAL NOT NULL,
 revoked_at REAL,
 FOREIGN KEY(source_digest,policy_digest) REFERENCES mirror_sources(digest,policy_digest),
 UNIQUE(id,job_id,execution_fence,source_digest,policy_digest),
 UNIQUE(job_id,execution_fence,network_generation)
) STRICT;
CREATE TABLE mirror_receipts (
 id TEXT PRIMARY KEY,
 session_id TEXT REFERENCES mirror_sessions(id),
 request_id TEXT REFERENCES mirror_receipts(id),
 job_id TEXT NOT NULL,
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=65536),
 created_at REAL NOT NULL
) STRICT;
CREATE UNIQUE INDEX mirror_request_terminal ON mirror_receipts(request_id) WHERE request_id IS NOT NULL;
CREATE INDEX mirror_job_receipts ON mirror_receipts(job_id,created_at);
CREATE INDEX mirror_session_receipts ON mirror_receipts(session_id,created_at);
CREATE TABLE mirror_claims (
 receipt_id TEXT PRIMARY KEY REFERENCES mirror_receipts(id),
 job_id TEXT NOT NULL,
 reason TEXT NOT NULL,
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=65536),
 created_at REAL NOT NULL
) STRICT;
CREATE TRIGGER mirror_policies_immutable BEFORE UPDATE ON mirror_policies
 BEGIN SELECT RAISE(ABORT,'mirror input is immutable'); END;
CREATE TRIGGER mirror_policies_retained BEFORE DELETE ON mirror_policies
 BEGIN SELECT RAISE(ABORT,'mirror input is retained'); END;
CREATE TRIGGER mirror_sessions_revoke_only BEFORE UPDATE ON mirror_sessions
 WHEN OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL OR
 NEW.id IS NOT OLD.id OR NEW.job_id IS NOT OLD.job_id OR
 NEW.execution_fence IS NOT OLD.execution_fence OR NEW.network_generation IS NOT OLD.network_generation OR
 NEW.source_digest IS NOT OLD.source_digest OR NEW.policy_digest IS NOT OLD.policy_digest OR
 NEW.deadline IS NOT OLD.deadline OR NEW.created_at IS NOT OLD.created_at
 BEGIN SELECT RAISE(ABORT,'mirror sessions only revoke'); END;
CREATE TRIGGER mirror_sessions_retained BEFORE DELETE ON mirror_sessions
 BEGIN SELECT RAISE(ABORT,'mirror sessions are retained'); END;
CREATE TRIGGER mirror_receipts_immutable BEFORE UPDATE ON mirror_receipts
 BEGIN SELECT RAISE(ABORT,'mirror receipt is immutable'); END;
CREATE TRIGGER mirror_receipts_retained BEFORE DELETE ON mirror_receipts
 BEGIN SELECT RAISE(ABORT,'mirror receipt is retained'); END;
CREATE TRIGGER mirror_claims_immutable BEFORE UPDATE ON mirror_claims
 BEGIN SELECT RAISE(ABORT,'mirror claim is immutable'); END;
CREATE TRIGGER mirror_claims_retained BEFORE DELETE ON mirror_claims
 BEGIN SELECT RAISE(ABORT,'mirror claim is retained'); END;
CREATE TABLE mirror_job_inputs (
 job_id TEXT PRIMARY KEY,
 execution_fence TEXT NOT NULL,
 source_digest TEXT NOT NULL CHECK(length(source_digest)=64),
 policy_digest TEXT NOT NULL REFERENCES mirror_policies(digest),
 session_id TEXT NOT NULL UNIQUE,
 notes_json TEXT NOT NULL CHECK(json_valid(notes_json) AND length(notes_json)<=1048576),
 publication_authorization_json TEXT NOT NULL CHECK(json_valid(publication_authorization_json) AND length(publication_authorization_json)<=65536),
 created_at REAL NOT NULL,
 FOREIGN KEY(source_digest,policy_digest) REFERENCES mirror_sources(digest,policy_digest),
 FOREIGN KEY(session_id,job_id,execution_fence,source_digest,policy_digest) REFERENCES mirror_sessions(id,job_id,execution_fence,source_digest,policy_digest)
) STRICT;
CREATE TRIGGER mirror_job_inputs_immutable BEFORE UPDATE ON mirror_job_inputs
 BEGIN SELECT RAISE(ABORT,'prepared job inputs are immutable'); END;
CREATE TRIGGER mirror_job_inputs_retained BEFORE DELETE ON mirror_job_inputs
 BEGIN SELECT RAISE(ABORT,'prepared job inputs are retained'); END;
