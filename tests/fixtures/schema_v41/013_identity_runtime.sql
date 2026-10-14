-- Bounded live identity lookup and immutable admitted operation bindings.
CREATE INDEX live_operator_flows ON operator_flows(expires_at) WHERE consumed_at IS NULL;
CREATE INDEX live_operator_sessions ON operator_sessions(expires_at) WHERE revoked_at IS NULL;
CREATE INDEX live_client_pairings ON client_pairings(expires_at);
CREATE INDEX live_client_connections ON client_connections(expires_at);
CREATE INDEX client_operation_history ON client_operations(connection_id,created_at DESC,id);
CREATE TRIGGER client_operation_request_fixed BEFORE UPDATE ON client_operations
WHEN NEW.id IS NOT OLD.id OR NEW.request_id IS NOT OLD.request_id
 OR NEW.connection_id IS NOT OLD.connection_id OR NEW.kind IS NOT OLD.kind
 OR NEW.payload_json IS NOT OLD.payload_json OR NEW.created_at IS NOT OLD.created_at
BEGIN SELECT RAISE(ABORT,'admitted client operation identity is immutable'); END;
CREATE TRIGGER client_grant_scope_fixed BEFORE UPDATE ON client_connections
WHEN NEW.id IS NOT OLD.id OR NEW.operator_id IS NOT OLD.operator_id
 OR NEW.authorization_epoch IS NOT OLD.authorization_epoch OR NEW.source_epoch IS NOT OLD.source_epoch
 OR NEW.instance_id IS NOT OLD.instance_id OR NEW.setup_generation IS NOT OLD.setup_generation
 OR NEW.scopes_json IS NOT OLD.scopes_json OR NEW.scope_json IS NOT OLD.scope_json
 OR NEW.created_at IS NOT OLD.created_at OR NEW.expires_at IS NOT OLD.expires_at
BEGIN SELECT RAISE(ABORT,'approved client grant authority is immutable'); END;
