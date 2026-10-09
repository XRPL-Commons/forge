-- Preserve enabled legacy auto policy only for repositories already configured.
-- All new repository IDs default to human; explicit existing per-repo choices win.
CREATE TEMP TABLE repository_policy_migration AS
SELECT c.domain,c.subject,
       (SELECT MAX(revision)+1 FROM configuration h WHERE h.domain=c.domain AND h.subject=c.subject) AS revision,
       json_set(c.settings_json,'$.repository_merge_policies',json_patch(
         COALESCE((SELECT json_group_object(CAST(json_extract(r.value,'$.id') AS TEXT),
           CASE WHEN json_extract(c.settings_json,'$.merge_enabled')=1
                     AND json_extract(c.settings_json,'$.human_required')=0 THEN 'auto' ELSE 'human' END)
           FROM configuration f,json_each(f.settings_json,'$.repositories') r
           WHERE f.domain=CASE WHEN c.domain='setup/runtime.json' THEN 'setup/factory.json' ELSE 'factory.json' END
             AND f.subject=c.subject AND f.active=1),'{}'),
         COALESCE(json_extract(c.settings_json,'$.repository_merge_policies'),'{}'))) AS settings_json,
       c.credential_ref,CAST(strftime('%s','now') AS REAL) AS created_at
FROM configuration c
WHERE c.active=1 AND c.domain IN ('runtime.json','setup/runtime.json','host-runtime.json');
UPDATE configuration SET active=0 WHERE active=1 AND domain IN ('runtime.json','setup/runtime.json','host-runtime.json');
INSERT INTO configuration(domain,subject,revision,active,settings_json,credential_ref,created_at)
SELECT domain,subject,revision,1,settings_json,credential_ref,created_at FROM repository_policy_migration;
DROP TABLE repository_policy_migration;
