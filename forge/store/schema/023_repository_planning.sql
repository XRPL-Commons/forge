DROP TRIGGER gate_codes_no_insert;
DROP TRIGGER gate_codes_no_update;
INSERT INTO gate_codes VALUES('triage_sync','intent',2,'observation',0,1,0);
UPDATE gate_codes SET member_scope=1 WHERE code IN ('operator_pause','operator_backlog','operator_sync');
CREATE TRIGGER gate_codes_no_insert BEFORE INSERT ON gate_codes
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;
CREATE TRIGGER gate_codes_no_update BEFORE UPDATE ON gate_codes
BEGIN SELECT RAISE(ABORT,'registry changes require a migration'); END;

-- Repository planning is context and proposed intent; lifecycle rows remain authoritative.
CREATE TABLE repository_planning (
 repository_id INTEGER PRIMARY KEY REFERENCES repositories(id),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 rules TEXT NOT NULL DEFAULT '' CHECK(length(CAST(rules AS BLOB))<=16384),
 rules_revision INTEGER NOT NULL DEFAULT 1 CHECK(rules_revision>0),
 proposals_enabled INTEGER NOT NULL DEFAULT 1 CHECK(proposals_enabled IN(0,1)),
 next_triage_at REAL NOT NULL DEFAULT 0,
 updated_at REAL NOT NULL,
 actor_id TEXT NOT NULL
) STRICT;
CREATE TABLE repository_note_revisions (
 note_id TEXT NOT NULL,
 revision INTEGER NOT NULL CHECK(revision>0),
 repository_id INTEGER NOT NULL REFERENCES repositories(id),
 kind TEXT NOT NULL CHECK(kind IN('convention','decision','lesson','question','next_action')),
 content TEXT NOT NULL CHECK(length(CAST(content AS BLOB)) BETWEEN 1 AND 8192),
 sources_json TEXT NOT NULL CHECK(json_valid(sources_json) AND length(CAST(sources_json AS BLOB))<=8192),
 deleted INTEGER NOT NULL DEFAULT 0 CHECK(deleted IN(0,1)),
 created_at REAL NOT NULL,
 actor_id TEXT NOT NULL,
 PRIMARY KEY(note_id,revision)
) STRICT;
CREATE INDEX repository_notes ON repository_note_revisions(repository_id,note_id,revision DESC);
CREATE TABLE repository_planning_requests (
 request_id TEXT PRIMARY KEY,
 actor_id TEXT NOT NULL,
 operation TEXT NOT NULL,
 payload_digest TEXT NOT NULL,
 result_json TEXT NOT NULL CHECK(json_valid(result_json) AND length(CAST(result_json AS BLOB))<=65536),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE triage_proposals (
 id TEXT PRIMARY KEY,
 repository_id INTEGER NOT NULL REFERENCES repositories(id),
 version INTEGER NOT NULL DEFAULT 1 CHECK(version>0),
 rules_revision INTEGER NOT NULL CHECK(rules_revision>0),
 base_branch TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN('draft','proposed','accepted','rejected','superseded')),
 rationale TEXT NOT NULL CHECK(length(CAST(rationale AS BLOB))<=8192),
 created_at REAL NOT NULL,
 actor_id TEXT NOT NULL,
 accepted_at REAL,
 accepted_by TEXT
) STRICT;
CREATE INDEX triage_repository_page ON triage_proposals(repository_id,created_at DESC,id);
CREATE TABLE triage_proposal_inputs (
 proposal_id TEXT NOT NULL REFERENCES triage_proposals(id),
 issue_id TEXT NOT NULL REFERENCES issues(id),
 issue_version INTEGER NOT NULL CHECK(issue_version>0),
 requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
 unit_id TEXT REFERENCES units(id),
 unit_version INTEGER,
 member_id TEXT REFERENCES unit_issues(id),
 candidate_id TEXT REFERENCES candidates(id),
 PRIMARY KEY(proposal_id,issue_id)
) STRICT;
CREATE TABLE triage_proposal_rows (
 id TEXT PRIMARY KEY,
 proposal_id TEXT NOT NULL REFERENCES triage_proposals(id),
 position INTEGER NOT NULL CHECK(position>=0),
 revision INTEGER NOT NULL DEFAULT 1 CHECK(revision>0),
 action TEXT NOT NULL CHECK(action IN('close','relabel','park','group','order','question','keep')),
 reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB)) BETWEEN 1 AND 8192),
 rule_quote TEXT CHECK(rule_quote IS NULL OR length(CAST(rule_quote AS BLOB))<=16384),
 parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json) AND length(CAST(parameters_json AS BLOB))<=65536),
 status TEXT NOT NULL DEFAULT 'proposed' CHECK(status IN('proposed','accepted','rejected')),
 decision_id TEXT,
 UNIQUE(proposal_id,position)
) STRICT;
CREATE TABLE triage_row_members (
 row_id TEXT NOT NULL REFERENCES triage_proposal_rows(id),
 issue_id TEXT NOT NULL REFERENCES issues(id),
 PRIMARY KEY(row_id,issue_id)
) STRICT;
CREATE TABLE triage_effect_authority (
 effect_id TEXT PRIMARY KEY REFERENCES external_effects(id),
 proposal_id TEXT NOT NULL REFERENCES triage_proposals(id),
 row_id TEXT NOT NULL REFERENCES triage_proposal_rows(id),
 issue_id TEXT NOT NULL REFERENCES issues(id),
 issue_version INTEGER NOT NULL,
 requirements_revision_id TEXT NOT NULL REFERENCES issue_revisions(id),
 actor_id TEXT NOT NULL,
 gate_id TEXT REFERENCES unit_gates(id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id)
) STRICT;
CREATE TABLE triage_effect_members (
 effect_id TEXT NOT NULL REFERENCES external_effects(id),
 member_id TEXT NOT NULL REFERENCES unit_issues(id),
 PRIMARY KEY(effect_id,member_id)
) STRICT;
CREATE TRIGGER triage_effect_authority_fixed BEFORE UPDATE ON triage_effect_authority
BEGIN SELECT RAISE(ABORT,'triage effect authority is immutable'); END;
CREATE TRIGGER repository_note_history_fixed BEFORE UPDATE ON repository_note_revisions
BEGIN SELECT RAISE(ABORT,'repository note history is immutable'); END;
CREATE TRIGGER repository_note_history_retained BEFORE DELETE ON repository_note_revisions
BEGIN SELECT RAISE(ABORT,'repository note history is retained'); END;

CREATE TABLE triage_row_history (
 row_id TEXT NOT NULL REFERENCES triage_proposal_rows(id),
 revision INTEGER NOT NULL,
 action TEXT NOT NULL,
 reason TEXT NOT NULL CHECK(length(CAST(reason AS BLOB))<=8192),
 rule_quote TEXT,
 parameters_json TEXT NOT NULL CHECK(json_valid(parameters_json) AND length(CAST(parameters_json AS BLOB))<=65536),
 actor_id TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(row_id,revision)
) STRICT;
CREATE TABLE triage_row_member_history (
 row_id TEXT NOT NULL,
 revision INTEGER NOT NULL,
 issue_id TEXT NOT NULL REFERENCES issues(id),
 PRIMARY KEY(row_id,revision,issue_id),
 FOREIGN KEY(row_id,revision) REFERENCES triage_row_history(row_id,revision)
) STRICT;
CREATE TABLE repository_rule_history (
 repository_id INTEGER NOT NULL REFERENCES repositories(id),
 revision INTEGER NOT NULL,
 rules TEXT NOT NULL CHECK(length(CAST(rules AS BLOB))<=16384),
 actor_id TEXT NOT NULL,
 created_at REAL NOT NULL,
 PRIMARY KEY(repository_id,revision)
) STRICT;

-- Add the planning role to the existing auxiliary ownership/cleanup boundary.
-- Child references retain this table name and are checked after the replacement.
PRAGMA defer_foreign_keys=ON;
DROP TRIGGER auxiliary_release_verified;
DROP TRIGGER auxiliary_lease_requires_live_execution;
DROP TRIGGER auxiliary_cleanup_monotonic;
CREATE TABLE auxiliary_executions_next (
 id TEXT PRIMARY KEY NOT NULL,
 domain TEXT NOT NULL CHECK(domain IN('setup','deployment','qualification','triage')),
 owner_id TEXT NOT NULL,
 position INTEGER NOT NULL CHECK(position>=0),
 phase TEXT NOT NULL REFERENCES job_phases(code),
 fenced INTEGER NOT NULL CHECK(fenced IN(0,1)),
 cleanup INTEGER NOT NULL CHECK(cleanup IN(0,1)),
 created_at REAL,
 CHECK(cleanup=0 OR (fenced=1 AND phase='cleaned')),
 UNIQUE(domain,owner_id,position)
) STRICT;
INSERT INTO auxiliary_executions_next SELECT * FROM auxiliary_executions;
DROP TABLE auxiliary_executions;
ALTER TABLE auxiliary_executions_next RENAME TO auxiliary_executions;
CREATE TRIGGER auxiliary_release_verified BEFORE DELETE ON auxiliary_leases
WHEN NOT EXISTS(SELECT 1 FROM auxiliary_executions WHERE id=OLD.execution_id AND fenced=1 AND cleanup=1)
BEGIN SELECT RAISE(ABORT,'cleanup must be verified before release'); END;
CREATE TRIGGER auxiliary_lease_requires_live_execution BEFORE INSERT ON auxiliary_leases
WHEN NOT EXISTS(SELECT 1 FROM auxiliary_executions WHERE id=NEW.execution_id AND cleanup=0 AND fenced=0)
BEGIN SELECT RAISE(ABORT,'lease requires an unfenced execution awaiting cleanup'); END;
CREATE TRIGGER auxiliary_cleanup_monotonic BEFORE UPDATE ON auxiliary_executions
WHEN NEW.fenced<OLD.fenced OR NEW.cleanup<OLD.cleanup
BEGIN SELECT RAISE(ABORT,'execution fencing and cleanup cannot be reversed'); END;
-- DROP TABLE records deferred violations against the old table identity even
-- after all references resolve to its replacement. Check the complete restored
-- graph, then end this migration's deferral before commit. Foreign keys stay ON.
CREATE TABLE planning_foreign_key_assertion (violations INTEGER CHECK(violations=0)) STRICT;
INSERT INTO planning_foreign_key_assertion SELECT count(*) FROM pragma_foreign_key_check;
DROP TABLE planning_foreign_key_assertion;
PRAGMA defer_foreign_keys=OFF;
CREATE TABLE repository_triage_runs (
 id TEXT PRIMARY KEY REFERENCES triage_proposals(id),
 repository_id INTEGER NOT NULL REFERENCES repositories(id),
 status TEXT NOT NULL CHECK(status IN('queued','running','captured','complete','failed','cancelled')),
 execution_id TEXT UNIQUE REFERENCES auxiliary_executions(id),
 requested_at REAL NOT NULL,
 requested_by TEXT NOT NULL,
 error TEXT,
 retry_at REAL
) STRICT;
CREATE UNIQUE INDEX one_repository_triage_run ON repository_triage_runs(repository_id)
WHERE status IN('queued','running','captured');
CREATE TABLE triage_candidate_context (
 proposal_id TEXT NOT NULL REFERENCES triage_proposals(id),
 candidate_id TEXT NOT NULL REFERENCES candidates(id),
 author_json TEXT NOT NULL CHECK(json_valid(author_json) AND length(CAST(author_json AS BLOB))<=1024),
 PRIMARY KEY(proposal_id,candidate_id)
) STRICT;
