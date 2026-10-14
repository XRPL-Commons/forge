-- Preserve the legacy opaque authorization epoch as well as the new monotonic
-- epoch. A migration must never make a stale grant current by renumbering it.
CREATE TABLE operator_auth_configuration (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    origin TEXT NOT NULL,
    client_id TEXT NOT NULL,
    credential_ref TEXT NOT NULL,
    credential_digest TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    source_epoch TEXT NOT NULL,
    authorization_epoch INTEGER NOT NULL CHECK (authorization_epoch > 0)
) STRICT;
ALTER TABLE operator_sessions ADD COLUMN csrf TEXT;
ALTER TABLE operator_sessions ADD COLUMN operator_login TEXT;
ALTER TABLE operator_flows ADD COLUMN verifier TEXT;
ALTER TABLE operator_flows ADD COLUMN source_epoch TEXT;
ALTER TABLE client_connections ADD COLUMN source_epoch TEXT;
ALTER TABLE client_connections ADD COLUMN instance_id TEXT;
ALTER TABLE client_connections ADD COLUMN setup_generation TEXT;
ALTER TABLE client_connections ADD COLUMN origin TEXT;
ALTER TABLE client_connections ADD COLUMN scope_json TEXT CHECK (scope_json IS NULL OR (json_valid(scope_json) AND length(CAST(scope_json AS BLOB)) <= 65536));
ALTER TABLE client_connections ADD COLUMN access_hash TEXT;
ALTER TABLE client_connections ADD COLUMN access_expires REAL;
ALTER TABLE client_connections ADD COLUMN refresh_hash TEXT;
ALTER TABLE client_connections ADD COLUMN idle_expires REAL;
ALTER TABLE client_connections ADD COLUMN last_used_at REAL;
ALTER TABLE client_connections ADD COLUMN revocation_reason TEXT;
ALTER TABLE client_connections ADD COLUMN next_refresh_at REAL;
ALTER TABLE client_connections ADD COLUMN operator_login TEXT;
CREATE UNIQUE INDEX client_access_hash ON client_connections(access_hash) WHERE access_hash IS NOT NULL AND access_hash != '';
CREATE UNIQUE INDEX client_refresh_hash ON client_connections(refresh_hash) WHERE refresh_hash IS NOT NULL AND refresh_hash != '';
CREATE TABLE consumed_refresh_tokens (
    token_hash TEXT PRIMARY KEY NOT NULL,
    connection_id TEXT NOT NULL REFERENCES client_connections(id),
    sequence INTEGER NOT NULL CHECK (sequence >= 0),
    UNIQUE (connection_id, sequence)
) STRICT;
CREATE TABLE client_pairings (
    device_hash TEXT PRIMARY KEY NOT NULL,
    user_code TEXT NOT NULL UNIQUE,
    instance_id TEXT NOT NULL,
    setup_generation TEXT NOT NULL,
    source_epoch TEXT NOT NULL,
    origin TEXT NOT NULL,
    client_name TEXT NOT NULL,
    capabilities_json TEXT NOT NULL CHECK (json_valid(capabilities_json) AND length(CAST(capabilities_json AS BLOB)) <= 65536),
    scope_json TEXT NOT NULL CHECK (json_valid(scope_json) AND length(CAST(scope_json AS BLOB)) <= 65536),
    expires_at REAL NOT NULL,
    poll_interval REAL NOT NULL CHECK (poll_interval > 0),
    next_poll_at REAL NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending','approved','denied','consumed')),
    connection_id TEXT REFERENCES client_connections(id)
) STRICT;
CREATE TABLE auth_rate_limits (
    kind TEXT NOT NULL CHECK (kind IN ('device','lookup')),
    subject TEXT NOT NULL,
    window_start REAL NOT NULL,
    request_count INTEGER NOT NULL CHECK (request_count >= 0),
    PRIMARY KEY (kind,subject)
) STRICT;
ALTER TABLE client_operations ADD COLUMN finished_at REAL;
ALTER TABLE client_operations ADD COLUMN error_code TEXT;
ALTER TABLE client_operations ADD COLUMN expected_revision TEXT;
