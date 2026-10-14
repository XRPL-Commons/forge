ALTER TABLE discussions ADD COLUMN current_execution_id TEXT REFERENCES discussion_executions(id);
ALTER TABLE discussions ADD COLUMN auto_retry TEXT;
ALTER TABLE discussions ADD COLUMN avoid_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(avoid_json) AND length(CAST(avoid_json AS BLOB))<=65536);
CREATE UNIQUE INDEX discussion_issue_identity ON discussions(issue_id);
CREATE TABLE discussion_runtime (
    execution_id TEXT PRIMARY KEY NOT NULL REFERENCES discussion_executions(id),
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json) AND length(CAST(manifest_json AS BLOB))<=262144),
    container_id TEXT,
    joined_at REAL,
    boot TEXT,
    completed INTEGER NOT NULL DEFAULT 0 CHECK(completed IN (0,1)),
    ended TEXT,
    cancelled INTEGER NOT NULL DEFAULT 0 CHECK(cancelled IN (0,1)),
    excerpt TEXT CHECK(excerpt IS NULL OR length(CAST(excerpt AS BLOB))<=16384)
) STRICT;
CREATE TRIGGER discussion_manifest_fixed BEFORE UPDATE ON discussion_runtime
WHEN NEW.execution_id!=OLD.execution_id OR NEW.manifest_json!=OLD.manifest_json
BEGIN SELECT RAISE(ABORT,'discussion launch identity is immutable'); END;
DROP TRIGGER discussion_artifacts_no_delete;
CREATE TRIGGER discussion_artifacts_no_delete BEFORE DELETE ON discussion_artifacts
WHEN OLD.kind!='logs' OR NOT EXISTS(SELECT 1 FROM discussion_executions WHERE id=OLD.execution_id AND cleanup=1)
BEGIN SELECT RAISE(ABORT,'discussion evidence is retained'); END;
