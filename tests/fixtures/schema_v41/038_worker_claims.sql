-- Worker statements are bounded re-test obligations, not permanent readiness facts.
CREATE TABLE worker_claim_attempts (
 job_id TEXT PRIMARY KEY REFERENCES jobs(id),
 claim_key TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(reason IN ('toolchain','sandbox','dependency_access','resources','ci_evidence','services','github_permission')),
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=65536),
 revisions_json TEXT NOT NULL CHECK(json_valid(revisions_json) AND length(revisions_json)<=8192),
 disposition TEXT NOT NULL CHECK(disposition IN ('deferred','repeated')),
 retry_at REAL NOT NULL,
 created_at REAL NOT NULL
) STRICT;
CREATE INDEX worker_claim_identity ON worker_claim_attempts(claim_key,created_at);
CREATE TABLE worker_claim_obligations (
 gate_id TEXT NOT NULL REFERENCES unit_gates(id),
 job_id TEXT NOT NULL REFERENCES worker_claim_attempts(job_id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id),
 released_at REAL,
 release_reason TEXT CHECK(release_reason IN ('operator','relevant_change','timer','environment_override')),
 CHECK((released_at IS NULL)=(release_reason IS NULL)),
 PRIMARY KEY(gate_id,job_id,member_id)
) STRICT;
CREATE INDEX worker_claim_pending ON worker_claim_obligations(gate_id,released_at);
CREATE TRIGGER worker_claim_attempts_no_update BEFORE UPDATE ON worker_claim_attempts
 BEGIN SELECT RAISE(ABORT,'worker claim evidence is immutable'); END;
CREATE TRIGGER worker_claim_attempts_no_delete BEFORE DELETE ON worker_claim_attempts
 BEGIN SELECT RAISE(ABORT,'worker claim evidence is retained'); END;
CREATE TRIGGER worker_claim_obligations_release_only BEFORE UPDATE ON worker_claim_obligations
 WHEN OLD.released_at IS NOT NULL OR NEW.released_at IS NULL OR NEW.release_reason IS NULL
 OR NEW.gate_id IS NOT OLD.gate_id OR NEW.job_id IS NOT OLD.job_id OR NEW.member_id IS NOT OLD.member_id
 BEGIN SELECT RAISE(ABORT,'worker claim obligations only release once'); END;
CREATE TRIGGER worker_claim_obligations_no_delete BEFORE DELETE ON worker_claim_obligations
 BEGIN SELECT RAISE(ABORT,'worker claim obligations are retained'); END;
