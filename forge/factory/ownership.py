"""Shared ownership facts for scheduling and issue presentation."""
from .contracts import claims_issue


def forge_branch(pr, number):
    branch = pr.get('branch') or (pr.get('head') or {}).get('ref') or ''
    return str(branch).startswith(f'forge/issue-{number}-')


def ownership(issue, item, repo, prs=()):
    """Return the current handoff needed, or None. Accept API and cached facts."""
    number = issue.get('number') or item['number']
    owned_pr = (item.get('candidate') or {}).get('pr')
    for pr in prs:
        if (pr.get('state', 'open') != 'open' or pr.get('merged')
                or publication_transfer_matches(pr, item)
                or pr['number'] == owned_pr or (pr.get('forge_owned') and forge_branch(pr, number))
                or not claims_issue(pr.get('body'), repo, number)):
            continue
        author = pr.get('author') or (pr.get('user') or {}).get('login')
        who = '@' + author if author else 'Someone'
        return {'headline': f"{who} has pull request #{pr['number']} open",
                'plain': 'Forge stays away so nothing is done twice. Merge or close that pull request, '
                         'or remove its link to this issue, to let Forge work on it.'}
    # The operator can assign a human to review Forge's existing candidate.
    # That recorded request is not a handoff of implementation ownership.
    request = item.get('reviewers_requested') or {}
    if 'candidate_id' in request:
        # Only a preserved Forge issue assignment on this exact current
        # candidate exempts a human assignee. A review request alone never does.
        identity = (item.get('candidate') or {}).get('key') or item.get('candidate_id')
        allowed = (owned_pr and request.get('issue_assignment') is True and request['candidate_id']==identity
                   and (item.get('unit_state') or item.get('state'))=='merging')
        reviewers = set(request.get('logins', [])) if allowed else set()
    else:
        reviewers = set(request.get('logins', [])) if owned_pr and item.get('state') in ('audited', 'approval_needed') else set()
    people = []
    for assignee in issue.get('assignees') or []:
        login = assignee.get('login', '') if isinstance(assignee, dict) else assignee
        kind = assignee.get('type') if isinstance(assignee, dict) else (issue.get('assignee_types') or {}).get(login)
        if login and login not in reviewers and kind != 'Bot' and not login.lower().endswith('[bot]'):
            people.append('@' + login)
    if people:
        return {'headline': 'Assigned to ' + ', '.join(people),
                'plain': 'Forge leaves assigned issues to their assignees. Unassign it on GitHub to let Forge work on it.'}
    labels = {label['name'] if isinstance(label, dict) else label for label in issue.get('labels', [])}
    if 'wip' in labels:
        return {'headline': 'Marked work in progress',
                'plain': 'The issue has the wip label, so Forge stays away. Remove the label on GitHub to let Forge work on it.'}
    return None


def publication_context(store, unit_id, item):
    """Attach only the operator-approved target identity, never a broad exemption."""
    import json
    with store.read_snapshot() as db:
        row = db.execute('SELECT target_json FROM unit_publication_targets WHERE unit_id=?', (unit_id,)).fetchone()
        target = json.loads(row[0]) if row else None
        if not target or target.get('kind') != 'existing' or target.get('ownership_transfer') is not True:
            return item
        unit = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
        source = db.execute('SELECT branch FROM candidates WHERE id=?', (target['candidate_id'],)).fetchone()
        selected = db.execute('SELECT * FROM candidates WHERE id=?', (unit['current_candidate_id'],)).fetchone()
        current = selected['head_oid'] if selected and selected['pull_number'] == target['pull_number'] else target['head_oid']
        jobs = [row[0] for row in db.execute("SELECT j.id FROM jobs j JOIN builder_assignments b ON b.job_id=j.id "
            'JOIN attempts a ON a.id=j.attempt_id LEFT JOIN job_execution_state e ON e.job_id=j.id '
            "WHERE j.unit_id=? AND b.stage='integration' AND j.outcome IS NULL "
            'AND a.assignment_generation=? AND a.membership_generation=? AND a.budget_generation=? '
            'AND (j.fenced=0 OR e.capture_authorized=1 AND e.result_revoked=0)',
            (unit_id, unit['fencing_generation'], unit['membership_generation'], unit['budget_generation']))]
        numbers = sorted(row[0] for row in db.execute('SELECT i.github_number FROM unit_issues m JOIN issues i ON i.id=m.issue_id '
                                                     'WHERE m.unit_id=? AND m.left_at IS NULL', (unit_id,)))
    return {**item, 'publication_transfer': {**target, 'expected_head': current, 'branch': source['branch'],
             'base_branch': unit['base_branch'], 'repository_id': unit['repository_id'],
             'delivery_id': unit_id, 'members': numbers, 'active_integrations': jobs}}


def publication_transfer_matches(pr, context, *, allow_publication=True):
    transfer = context.get('publication_transfer')
    if not transfer or transfer.get('ownership_transfer') is not True:
        return False
    user = pr.get('user') or {}
    if (pr.get('number') != transfer['pull_number'] or pr.get('state') != 'open' or pr.get('merged')
            or {key: user.get(key) for key in ('id', 'login', 'type')} != transfer.get('author')
            or pr.get('head', {}).get('ref') != transfer['branch']
            or pr.get('head', {}).get('repo', {}).get('id') != transfer['repository_id']
            or pr.get('base', {}).get('ref') != transfer['base_branch']
            or pr.get('base', {}).get('repo', {}).get('id') != transfer['repository_id']):
        return False
    head = pr.get('head', {}).get('sha')
    if head == transfer['expected_head']:
        return True
    if allow_publication:
        from .contracts import submission
        try:
            marker = submission(pr.get('body') or '')
        except (ValueError, TypeError):
            return False
        return (marker.get('unit') == transfer['delivery_id'] and marker.get('members') == transfer['members']
                and marker.get('attempt') in transfer['active_integrations'] and marker.get('head') == head)
    return False
