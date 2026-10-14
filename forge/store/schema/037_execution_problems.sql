-- Deduplicated local drafts. Nothing in this table authorizes GitHub publication.
CREATE TABLE forge_problem_reports (
 signature TEXT PRIMARY KEY NOT NULL CHECK(length(signature)=64),
 details_json TEXT NOT NULL CHECK(json_valid(details_json) AND length(details_json)<=8192),
 created_at REAL NOT NULL
) STRICT;
CREATE TABLE forge_problem_report_jobs (
 signature TEXT NOT NULL REFERENCES forge_problem_reports(signature),
 job_id TEXT NOT NULL REFERENCES jobs(id),
 PRIMARY KEY(signature,job_id)
) STRICT;
CREATE TRIGGER forge_problem_reports_no_update BEFORE UPDATE ON forge_problem_reports
 BEGIN SELECT RAISE(ABORT,'Forge report drafts retain their original safe signature'); END;
CREATE TRIGGER forge_problem_reports_no_delete BEFORE DELETE ON forge_problem_reports
 BEGIN SELECT RAISE(ABORT,'Forge report drafts are durable'); END;
CREATE TRIGGER forge_problem_report_jobs_no_update BEFORE UPDATE ON forge_problem_report_jobs
 BEGIN SELECT RAISE(ABORT,'Forge report associations are immutable'); END;
CREATE TRIGGER forge_problem_report_jobs_no_delete BEFORE DELETE ON forge_problem_report_jobs
 BEGIN SELECT RAISE(ABORT,'Forge report associations are immutable'); END;
