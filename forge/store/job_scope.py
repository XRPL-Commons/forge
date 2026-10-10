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


def affected_jobs_in(db, unit, member_ids, *, include_descendants=True):
    """One scope for member fencing and the cleanup required by that decision.

    Task scopes come from immutable pins and the effective task DAG. Shared
    phases already pin every member; a nullable jobs.member_id is not authority.
    """
    from .delivery_inputs import dependencies_in
    from .task_inputs import affected_tasks_in
    from .models import TransitionError

    members = [row[0] for row in db.execute(
        'SELECT id FROM unit_issues WHERE unit_id=? AND left_at IS NULL', (unit['id'],))]
    affected = set(member_ids)
    if not affected <= set(members):
        raise TransitionError('scoped fencing requires current members')
    if include_descendants:
        while True:
            following = {member for member in members if set(dependencies_in(db, unit, member)) & affected}
            if following <= affected:
                break
            affected.update(following)
    tasks = affected_tasks_in(db, unit['id'], affected)
    jobs = []
    for row in db.execute('SELECT j.id,j.member_id,j.cleanup,j.outcome,t.task_id FROM jobs j '
            'LEFT JOIN job_tasks t ON t.job_id=j.id WHERE j.unit_id=?', (unit['id'],)):
        if row['cleanup'] and row['outcome'] is not None:
            continue
        scope = {pin[0] for pin in db.execute('SELECT member_id FROM job_members WHERE job_id=?', (row['id'],))}
        if (row['task_id'] in tasks if row['task_id'] is not None else bool(scope & affected)):
            jobs.append(dict(row))
    return {'member_ids': sorted(affected), 'jobs': jobs}
