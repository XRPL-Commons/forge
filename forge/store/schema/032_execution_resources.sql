-- Host-observed counters belong to a physical execution, not its issue scope.
-- Latest observation failures retain the last successful sample's own timestamp.
CREATE TABLE job_resource_observations (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    container_id TEXT NOT NULL,
    checked_at REAL NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('available','partial','unavailable')),
    reason TEXT,
    sampled_at REAL,
    cpu_seconds REAL CHECK(cpu_seconds >= 0),
    memory_bytes INTEGER CHECK(memory_bytes >= 0),
    CHECK((sampled_at IS NULL) = (cpu_seconds IS NULL AND memory_bytes IS NULL))
) STRICT;
-- Compact immutable receipt committed alongside worker_capture. Reads never
-- need to decompress results or logs to display numeric resource evidence.
CREATE TABLE job_resource_receipts (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    usage_json TEXT NOT NULL CHECK(json_valid(usage_json) AND length(CAST(usage_json AS BLOB)) <= 2048)
) STRICT;
CREATE TRIGGER resource_receipts_no_update BEFORE UPDATE ON job_resource_receipts
BEGIN SELECT RAISE(ABORT,'resource capture is immutable'); END;
CREATE TRIGGER resource_receipts_no_delete BEFORE DELETE ON job_resource_receipts
BEGIN SELECT RAISE(ABORT,'resource capture is immutable'); END;
