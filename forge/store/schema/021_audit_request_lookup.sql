-- Retained audit history grows indefinitely. Request replay/finalization uses
-- this key so admission cost does not grow with unrelated history.
CREATE INDEX operator_audit_request_kind
ON operator_audit(request_id, json_extract(details_json,'$.kind'), sequence DESC);
