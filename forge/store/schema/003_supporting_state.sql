-- Fields needed by the observed legacy stores. Collections remain separate rows;
-- JSON columns contain bounded facts about one entity, never a runtime store.
ALTER TABLE issues ADD COLUMN assignee_types_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(assignee_types_json) AND length(CAST(assignee_types_json AS BLOB)) <= 65536);
ALTER TABLE repository_observations ADD COLUMN refreshing INTEGER NOT NULL DEFAULT 0 CHECK (refreshing IN (0,1));
CREATE TABLE inventory_pull_summaries (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    github_number INTEGER NOT NULL CHECK (github_number > 0),
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    author_login TEXT NOT NULL,
    branch TEXT NOT NULL,
    forge_owned INTEGER NOT NULL CHECK (forge_owned IN (0,1)),
    observation_id TEXT NOT NULL REFERENCES repository_observations(id),
    PRIMARY KEY (repository_id, github_number)
) STRICT;
CREATE TABLE inventory_collaborators (
    repository_id INTEGER NOT NULL REFERENCES repositories(id),
    login TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('collaborator','recent_author')),
    observation_id TEXT NOT NULL REFERENCES repository_observations(id),
    details_json TEXT NOT NULL CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536),
    PRIMARY KEY (repository_id, login, source)
) STRICT;
ALTER TABLE provider_cooldowns ADD COLUMN source_job_id TEXT REFERENCES jobs(id);
ALTER TABLE memory_profiles ADD COLUMN allocation TEXT;
ALTER TABLE discussions ADD COLUMN status TEXT NOT NULL DEFAULT 'idle';
ALTER TABLE discussions ADD COLUMN source_revision TEXT;
ALTER TABLE discussions ADD COLUMN requested_at REAL;
ALTER TABLE discussions ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1));
ALTER TABLE discussions ADD COLUMN reason TEXT;
ALTER TABLE discussions ADD COLUMN partial TEXT;
ALTER TABLE discussions ADD COLUMN origin TEXT;
ALTER TABLE discussions ADD COLUMN presence_json TEXT CHECK (presence_json IS NULL OR (json_valid(presence_json) AND length(CAST(presence_json AS BLOB)) <= 65536));
ALTER TABLE messages ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(metadata_json) AND length(CAST(metadata_json AS BLOB)) <= 65536);
ALTER TABLE discussion_executions ADD COLUMN completion_at REAL;
ALTER TABLE discussion_executions ADD COLUMN source_created_at REAL;
CREATE INDEX discussion_execution_history ON discussion_executions(discussion_id, created_at DESC, id);
CREATE TABLE issue_action_requests (
    id TEXT PRIMARY KEY NOT NULL,
    issue_id TEXT NOT NULL REFERENCES issues(id),
    action TEXT NOT NULL,
    expected_revision TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','applied','rejected')),
    reason TEXT NOT NULL,
    message TEXT,
    plan_json TEXT CHECK (plan_json IS NULL OR (json_valid(plan_json) AND length(CAST(plan_json AS BLOB)) <= 65536)),
    environment_json TEXT CHECK (environment_json IS NULL OR (json_valid(environment_json) AND length(CAST(environment_json AS BLOB)) <= 65536)),
    created_at REAL NOT NULL
) STRICT;
CREATE INDEX pending_issue_actions ON issue_action_requests(status, created_at, id);
ALTER TABLE notification_outbox ADD COLUMN source_event_id TEXT;
ALTER TABLE notification_outbox ADD COLUMN delivery_state TEXT NOT NULL DEFAULT 'pending' CHECK (delivery_state IN ('pending','retry','failed','accepted_by_relay','disabled'));
ALTER TABLE notification_outbox ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0);
ALTER TABLE notification_outbox ADD COLUMN last_attempt_at REAL;
CREATE INDEX notification_event_channels ON notification_outbox(source_event_id, channel);
ALTER TABLE runtime_controls ADD COLUMN source_generation TEXT;
ALTER TABLE runtime_controls ADD COLUMN details_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(details_json) AND length(CAST(details_json AS BLOB)) <= 65536);
ALTER TABLE environment_scans ADD COLUMN source_id TEXT;
ALTER TABLE environment_scans ADD COLUMN error TEXT;
ALTER TABLE environment_scans ADD COLUMN source_fields_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(source_fields_json));
CREATE TABLE operator_requests (
    id TEXT PRIMARY KEY NOT NULL,
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    result_json TEXT CHECK (result_json IS NULL OR (json_valid(result_json) AND length(CAST(result_json AS BLOB)) <= 65536))
) STRICT;
