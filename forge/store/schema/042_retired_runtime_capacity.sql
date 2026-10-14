-- Capacity is governed by role ceilings and memory admission. Retain every old
-- revision, but stop passing the retired global ceiling to runtime validation.
CREATE TEMP TABLE retired_capacity_configs AS
SELECT c.domain, c.subject, c.revision AS previous_revision,
       (SELECT MAX(h.revision) + 1 FROM configuration h
        WHERE h.domain = c.domain AND h.subject = c.subject) AS revision,
       json_remove(c.settings_json, '$.max_running') AS settings_json,
       c.credential_ref
FROM configuration c
WHERE c.active = 1 AND c.subject = 'instance'
  AND c.domain IN ('runtime.json', 'setup/runtime.json', 'host-runtime.json')
  AND json_type(c.settings_json, '$.max_running') IS NOT NULL;

UPDATE configuration SET active = 0
WHERE (domain, subject, revision) IN
      (SELECT domain, subject, previous_revision FROM retired_capacity_configs);

INSERT INTO configuration(domain, subject, revision, active, settings_json, credential_ref, created_at)
SELECT domain, subject, revision, 1, settings_json, credential_ref, unixepoch('now')
FROM retired_capacity_configs;

DROP TABLE retired_capacity_configs;
