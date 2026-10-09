-- A partial repository scan is a collection, not a repository metadata blob.
-- Keep its original observation immutable and read its issue facts in pages.
CREATE TABLE inventory_scan_issues (
    observation_id TEXT NOT NULL REFERENCES repository_observations(id),
    issue_key TEXT NOT NULL,
    body TEXT,
    body_present INTEGER NOT NULL CHECK(body_present IN (0,1)),
    fields_json TEXT NOT NULL CHECK(json_valid(fields_json) AND length(CAST(fields_json AS BLOB))<=65536),
    PRIMARY KEY(observation_id,issue_key)
) STRICT;
