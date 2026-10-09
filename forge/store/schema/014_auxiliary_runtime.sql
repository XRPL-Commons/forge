CREATE TABLE auxiliary_runtime (
    execution_id TEXT PRIMARY KEY NOT NULL REFERENCES auxiliary_executions(id),
    manifest_json TEXT NOT NULL CHECK (json_valid(manifest_json) AND length(CAST(manifest_json AS BLOB)) <= 65536),
    deadline REAL NOT NULL,
    container_id TEXT,
    capacity_wait_since REAL,
    capture_digest TEXT
) STRICT;
CREATE TRIGGER auxiliary_manifest_immutable BEFORE UPDATE ON auxiliary_runtime
WHEN NEW.manifest_json != OLD.manifest_json OR NEW.execution_id != OLD.execution_id
BEGIN SELECT RAISE(ABORT,'auxiliary launch identity is immutable'); END;
