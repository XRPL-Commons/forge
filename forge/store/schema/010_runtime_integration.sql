-- Runtime activation is a one-way maintenance operation. Its journal remains
-- separate from the imported source archive and ordinary delivery commands.
CREATE TABLE runtime_operations (
    id TEXT PRIMARY KEY NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('activate','upgrade')),
    stage TEXT NOT NULL,
    source_revision TEXT NOT NULL,
    manifest_digest TEXT NOT NULL,
    started_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    evidence_json TEXT NOT NULL CHECK (json_valid(evidence_json) AND length(CAST(evidence_json AS BLOB)) <= 65536)
) STRICT;
