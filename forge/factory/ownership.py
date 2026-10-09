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
