-- Raw worker diagnostics expire independently of immutable structured evidence.
CREATE TABLE auxiliary_logs (
 execution_id TEXT PRIMARY KEY REFERENCES auxiliary_executions(id),
 digest TEXT NOT NULL,
 content BLOB CHECK(content IS NULL OR length(content)<=65536),
 created_at REAL NOT NULL,
 expires_at REAL NOT NULL CHECK(expires_at>=created_at),
 expired_at REAL,
 CHECK((content IS NOT NULL AND expired_at IS NULL) OR (content IS NULL AND expired_at IS NOT NULL AND expired_at>=expires_at))
) STRICT;
CREATE INDEX auxiliary_logs_expiry ON auxiliary_logs(expires_at) WHERE expired_at IS NULL;
CREATE TRIGGER auxiliary_logs_expiry_only BEFORE UPDATE ON auxiliary_logs
WHEN NEW.execution_id!=OLD.execution_id OR NEW.digest!=OLD.digest
 OR NEW.created_at!=OLD.created_at OR NEW.expires_at!=OLD.expires_at
 OR OLD.expired_at IS NOT NULL OR NEW.content IS NOT NULL OR NEW.expired_at IS NULL
 OR NEW.expired_at<OLD.expires_at
 OR NOT EXISTS(SELECT 1 FROM auxiliary_executions e WHERE e.id=OLD.execution_id AND e.cleanup=1)
 OR EXISTS(SELECT 1 FROM auxiliary_leases l WHERE l.execution_id=OLD.execution_id)
BEGIN SELECT RAISE(ABORT,'auxiliary logs may only expire after retention and cleanup'); END;
CREATE TRIGGER auxiliary_logs_no_delete BEFORE DELETE ON auxiliary_logs
BEGIN SELECT RAISE(ABORT,'auxiliary log provenance must be retained'); END;
