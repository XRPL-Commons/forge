"""Recorded issue scope for current and imported public job metadata."""
# Public history can use an imported job's explicit member identity even when
# its original requirements revision was never recorded. This does not create
# an execution pin or select a member from the unit's current membership.
JOB_MEMBER_SCOPE = "(m.id IN (SELECT p.member_id FROM job_members p WHERE p.job_id=j.id) OR " \
    "(m.id=j.member_id AND NOT EXISTS(SELECT 1 FROM job_members p WHERE p.job_id=j.id)))"
ISSUE_JOB_SCOPE = 'EXISTS(SELECT 1 FROM unit_issues m WHERE m.unit_id=j.unit_id AND m.issue_id=? AND '+JOB_MEMBER_SCOPE+')'



def issue_jobs_in(db, unit_id, issue_id):
    if unit_id is None:
        return []
    return db.execute("SELECT j.id,j.role,j.phase,j.outcome,j.cleanup,j.fenced,j.created_at FROM jobs j WHERE j.unit_id=? AND " + ISSUE_JOB_SCOPE +
                      ' ORDER BY j.created_at DESC,j.id DESC LIMIT 6', (unit_id, issue_id)).fetchall()
