"""Shared ownership facts for scheduling and issue presentation."""



COLLABORATOR_ASSOCIATIONS=frozenset({'OWNER','MEMBER','COLLABORATOR'})


def collaborator(pr):
    from .github import GitHubUnavailable
    user=pr.get('user') or {}
    if user.get('type')=='Bot':
        return False
    if user.get('type')!='User' or pr.get('author_association') not in {
            'OWNER','MEMBER','COLLABORATOR','CONTRIBUTOR','FIRST_TIMER','FIRST_TIME_CONTRIBUTOR','MANNEQUIN','NONE'}:
        raise GitHubUnavailable('Pull request collaborator identity has not been observed')
    return (pr.get('author_association') in COLLABORATOR_ASSOCIATIONS
            and user.get('type')=='User' and isinstance(user.get('login'),str))


def linked_issue(pr, repository_id, number):
    """A supplied list is a complete typed observation, never a prose fallback."""
    from .github import GitHubUnavailable
    references=pr.get('closing_issues')
    if not isinstance(references,list):
        raise GitHubUnavailable('Collaborator pull request links have not been observed')
    return any(row['repository_id']==repository_id and row['number']==number for row in references)


def linked_pulls(github, item, pulls):
    """Enrich REST pull identities with complete GitHub closingIssuesReferences."""
    from .github import GitHubUnavailable
    query = '''query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
      repository(owner:$owner,name:$name) { databaseId
        pullRequest(number:$number) { databaseId number authorAssociation
          closingIssuesReferences(first:100,after:$cursor) {
            nodes { number repository { databaseId } }
            pageInfo { hasNextPage endCursor }
          }
        }
      }
    }'''
    owner,name=item['repo'].split('/')
    result=[]
    for original in pulls:
        pr=dict(original)
        if not collaborator(pr):
            pr['closing_issues']=[];result.append(pr);continue
        references=[];cursor=None;seen=set()
        for _ in range(5):
            data=github.graphql('controller',item,query,dict(owner=owner,name=name,number=pr['number'],cursor=cursor))
            repository=data.get('repository') or {};remote=repository.get('pullRequest') or {}
            links=remote.get('closingIssuesReferences') or {};page=links.get('pageInfo') or {};nodes=links.get('nodes')
            if (repository.get('databaseId')!=item['repo_id'] or remote.get('databaseId')!=pr.get('id')
                    or remote.get('number')!=pr['number'] or remote.get('authorAssociation')!=pr['author_association']
                    or not isinstance(nodes,list) or type(page.get('hasNextPage')) is not bool):
                raise GitHubUnavailable('Collaborator pull request link identity is unconfirmed')
            for node in nodes:
                repo_id=(node.get('repository') or {}).get('databaseId') if isinstance(node,dict) else None
                number=node.get('number') if isinstance(node,dict) else None
                if type(repo_id) is not int or repo_id<=0 or type(number) is not int or number<=0:
                    raise GitHubUnavailable('Linked issue identity is unconfirmed')
                references.append({'repository_id':repo_id,'number':number})
            if not page['hasNextPage']:
                break
            cursor=page.get('endCursor')
            if not isinstance(cursor,str) or not cursor or cursor in seen:
                raise GitHubUnavailable('Pull request link pagination is unconfirmed')
            seen.add(cursor)
        else:
            raise GitHubUnavailable('Pull request links exceed the complete observation bound')
        pr['closing_issues']=references;result.append(pr)
    return result


def forge_branch(pr, number):
    branch = pr.get('branch') or (pr.get('head') or {}).get('ref') or ''
    return str(branch).startswith(f'forge/issue-{number}-')


def ownership(issue, item, repo, prs=()):
    """Return the current handoff needed, or None. Accept API and cached facts."""
    number = issue.get('number') or item['number']
    owned_pr = (item.get('candidate') or {}).get('pr')
    for pr in prs:
        if (pr.get('state', 'open') != 'open' or pr.get('merged')
                or publication_transfer_matches(pr, item) or approved_source_matches(pr, item) or owned_publication_matches(pr, item)
                or pr['number'] == owned_pr or (pr.get('forge_owned') and forge_branch(pr, number))
                or not collaborator(pr) or not linked_issue(pr, item['repo_id'], number)):
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
        if target:
            from ..store.delivery_inputs import existing_candidates_in
            unit = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
            sources = existing_candidates_in(db, unit_id)
            item = {**item, 'approved_sources': [{**source, 'repository_id': unit['repository_id'],
                     'base_branch': unit['base_branch']} for source in sources
                     if target.get('kind') != 'existing' or source['candidate_id'] != target['candidate_id']]}
        if not target or target.get('kind') == 'new':
            from ..store.delivery_inputs import previous_publication_in
            previous = previous_publication_in(db, unit_id)
            if previous:
                unit = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
                if 'source_job_id' in previous:
                    owner = db.execute('SELECT job_id AS id,publisher FROM job_launches WHERE job_id=?',
                                       (previous['source_job_id'],)).fetchone()
                else:
                    owner = db.execute("SELECT j.id,l.publisher FROM candidate_evidence c JOIN jobs j ON j.attempt_id=c.attempt_id "
                                       "JOIN job_launches l ON l.job_id=j.id WHERE c.candidate_id=? AND c.kind='publication'",
                                       (previous['id'],)).fetchone()
                if owner:
                    item = {**item, 'owned_publication': {**previous, 'repository_id': unit['repository_id'],
                            'base_branch': unit['base_branch'], 'publisher': (previous.get('author') or {}).get('login', owner['publisher']), 'attempt': owner['id']}}
        if not target or target.get('kind') != 'existing' or target.get('ownership_transfer') is not True:
            return item
        unit = db.execute('SELECT * FROM units WHERE id=?', (unit_id,)).fetchone()
        source = db.execute('SELECT branch FROM candidates WHERE id=?', (target['candidate_id'],)).fetchone()
        from ..store.delivery_inputs import previous_publication_in
        selected = previous_publication_in(db, unit_id)
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


def approved_source_matches(pr, context):
    """Read admission for preserved input; never grants publication authority."""
    for source in context.get('approved_sources', []):
        if (pr.get('number') == source['pull_number'] and pr.get('state') == 'open' and not pr.get('merged')
                and pr.get('head', {}).get('sha') == source['head_oid']
                and pr.get('head', {}).get('ref') == source['branch']
                and pr.get('head', {}).get('repo', {}).get('id') == source['repository_id']
                and pr.get('base', {}).get('ref') == source['base_branch']
                and pr.get('base', {}).get('repo', {}).get('id') == source['repository_id']
                and {key: pr.get('user', {}).get(key) for key in ('id','login','type')} == source['author']):
            return True
    return False


def active_publication_matches(store, unit_id, pr):
    """A current integration may update only its exact prechecked existing PR."""
    import json
    with store.read_snapshot() as db:
        assignments = db.execute(
            'SELECT j.id,l.branch,l.context_json,a.input_manifest_json,u.repository_id,u.base_branch '
            'FROM jobs j JOIN builder_assignments b ON b.job_id=j.id JOIN units u ON u.id=j.unit_id '
            'JOIN attempts a ON a.id=j.attempt_id JOIN job_launches l ON l.job_id=j.id '
            'LEFT JOIN job_execution_state e ON e.job_id=j.id '
            "WHERE j.unit_id=? AND b.stage='integration' AND j.outcome IS NULL "
            'AND a.assignment_generation=u.fencing_generation AND a.membership_generation=u.membership_generation '
            'AND a.budget_generation=u.budget_generation AND (j.fenced=0 OR e.capture_authorized=1 AND e.result_revoked=0)',
            (unit_id,)).fetchall()
        for assignment in assignments:
            context = json.loads(assignment['context_json'])
            manifest = json.loads(assignment['input_manifest_json'])
            previous = manifest.get('previous_publication')
            target = manifest['publication_target']
            expected = previous if previous else (target if target['kind'] == 'existing' else None)
            if (expected is None or context.get('resume_pr') != expected['pull_number']
                    or context.get('resume_head') != expected['head_oid']):
                continue
            if (not context.get('resume_pr') or not context.get('resume_head') or not context.get('publication_author')
                    or pr.get('base', {}).get('sha') != manifest['base_oid']):
                continue
            members = sorted(row[0] for row in db.execute(
                'SELECT i.github_number FROM job_members p JOIN unit_issues m ON m.id=p.member_id '
                'JOIN issues i ON i.id=m.issue_id WHERE p.job_id=?', (assignment['id'],)))
            transfer = {'ownership_transfer': True, 'pull_number': context['resume_pr'],
                        'expected_head': context['resume_head'], 'author': context['publication_author'],
                        'branch': assignment['branch'], 'base_branch': assignment['base_branch'],
                        'repository_id': assignment['repository_id'], 'delivery_id': unit_id,
                        'members': members, 'active_integrations': [assignment['id']]}
            if publication_transfer_matches(pr, {'publication_transfer': transfer}):
                return True
    return False


def owned_publication_matches(pr, context):
    """An invalidated review candidate remains the same builder's publication."""
    previous = context.get('owned_publication')
    if not previous:
        return False
    if 'author' in previous and {key: (pr.get('user') or {}).get(key) for key in ('id', 'login', 'type')} != previous['author']:
        return False
    return (pr.get('number') == previous['pull_number'] and pr.get('state') == 'open' and not pr.get('merged')
            and pr.get('head', {}).get('sha') == previous['head_oid']
            and pr.get('head', {}).get('ref') == previous['branch']
            and pr.get('head', {}).get('repo', {}).get('id') == previous['repository_id']
            and pr.get('base', {}).get('ref') == previous['base_branch']
            and pr.get('base', {}).get('repo', {}).get('id') == previous['repository_id']
            and pr.get('user', {}).get('type') == 'Bot' and pr.get('user', {}).get('login') == previous['publisher'])
