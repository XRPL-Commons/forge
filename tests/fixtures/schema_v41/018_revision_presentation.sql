-- Display metadata only; admission and dependency authority use typed commands.
CREATE TABLE revision_presentation (
    revision_id TEXT PRIMARY KEY NOT NULL REFERENCES issue_revisions(id),
    body_truncated INTEGER NOT NULL CHECK(body_truncated IN (0,1)),
    source_content_hash TEXT NOT NULL
) STRICT;
CREATE TABLE revision_display_dependencies (
    revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
    issue_number INTEGER NOT NULL CHECK(issue_number>0),
    PRIMARY KEY(revision_id,issue_number)
) STRICT;
CREATE TABLE imported_operator_requests (
    request_id TEXT PRIMARY KEY NOT NULL REFERENCES issue_action_requests(id),
    source_manifest TEXT NOT NULL,
    source_status TEXT NOT NULL,
    source_message TEXT,
    interrupted_at REAL,
    evidence_json TEXT CHECK(evidence_json IS NULL OR json_valid(evidence_json))
) STRICT;
CREATE TABLE inventory_pull_issue_links (
    repository_id INTEGER NOT NULL,
    pull_number INTEGER NOT NULL,
    issue_number INTEGER NOT NULL CHECK(issue_number>0),
    forge_attempt INTEGER NOT NULL CHECK(forge_attempt IN (0,1)),
    PRIMARY KEY(repository_id,pull_number,issue_number),
    FOREIGN KEY(repository_id,pull_number) REFERENCES inventory_pull_summaries(repository_id,github_number) ON DELETE CASCADE
) STRICT;
CREATE INDEX issue_related_pulls ON inventory_pull_issue_links(repository_id,issue_number,pull_number);
CREATE TABLE issue_label_attributions (
    issue_id TEXT NOT NULL REFERENCES issues(id),
    label TEXT NOT NULL,
    login TEXT,
    applied_at TEXT,
    position INTEGER NOT NULL,
    labels_hash TEXT NOT NULL,
    PRIMARY KEY(issue_id,label)
) STRICT;
ALTER TABLE unit_environment_observations ADD COLUMN environment_hold_json TEXT CHECK(environment_hold_json IS NULL OR json_valid(environment_hold_json));
