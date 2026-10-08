-- Immutable, chunked import evidence. Never a runtime backend.
CREATE TABLE import_documents (
    run_id TEXT NOT NULL REFERENCES import_runs(id),
    source_name TEXT NOT NULL,
    format TEXT NOT NULL CHECK(format IN ('json','jsonl','bytes')),
    canonical_digest TEXT NOT NULL,
    record_count INTEGER NOT NULL CHECK(record_count >= 0),
    PRIMARY KEY(run_id,source_name),
    FOREIGN KEY(run_id,source_name) REFERENCES import_sources(run_id,source_name)
) STRICT;
CREATE TABLE import_records (
    run_id TEXT NOT NULL,
    source_name TEXT NOT NULL,
    record_key TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK(sequence >= 0),
    digest TEXT NOT NULL,
    content BLOB NOT NULL CHECK(length(content) <= 1048576),
    PRIMARY KEY(run_id,source_name,record_key,sequence),
    FOREIGN KEY(run_id,source_name) REFERENCES import_documents(run_id,source_name)
) STRICT;
CREATE TABLE import_findings (
    run_id TEXT NOT NULL REFERENCES import_runs(id),
    subject TEXT NOT NULL,
    code TEXT NOT NULL,
    blocking INTEGER NOT NULL CHECK(blocking IN (0,1)),
    PRIMARY KEY(run_id,subject,code)
) STRICT;
CREATE TRIGGER import_documents_no_update BEFORE UPDATE ON import_documents
BEGIN SELECT RAISE(ABORT,'import evidence is immutable'); END;
CREATE TRIGGER import_documents_no_delete BEFORE DELETE ON import_documents
BEGIN SELECT RAISE(ABORT,'import evidence is retained'); END;
CREATE TRIGGER import_records_no_update BEFORE UPDATE ON import_records
BEGIN SELECT RAISE(ABORT,'import evidence is immutable'); END;
CREATE TRIGGER import_records_no_delete BEFORE DELETE ON import_records
BEGIN SELECT RAISE(ABORT,'import evidence is retained'); END;
DROP TRIGGER event_kinds_no_insert;
INSERT INTO event_kinds(code) VALUES('legacy_event');
CREATE TRIGGER event_kinds_no_insert BEFORE INSERT ON event_kinds
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
-- Import-only databases may add already-cleaned historical executions. Runtime
-- stores retain the terminal-unit launch prohibition without an exception.
DROP TRIGGER jobs_open_unit;
CREATE TRIGGER jobs_open_unit BEFORE INSERT ON jobs
WHEN EXISTS (SELECT 1 FROM units WHERE id=NEW.unit_id AND state IN ('merged','closed','dissolved'))
AND NOT (NEW.phase='cleaned' AND NEW.cleanup=1 AND NEW.fenced=1
    AND EXISTS (SELECT 1 FROM store_metadata WHERE key='import_only' AND value='true'))
BEGIN SELECT RAISE(ABORT,'a terminal unit cannot start a job'); END;
