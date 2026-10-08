"""Small display projections recorded with immutable issue revisions.

These rows are never prerequisites or dispatch authority.
"""
from ..factory.contracts import dependencies, digest


def record_revision_display(db, revision_id, body, requirements, *, source_content_hash=None):
    truncated=isinstance(requirements,dict) and bool(requirements.get('body_truncated'))
    db.execute('INSERT INTO revision_presentation VALUES(?,?,?)',
               (revision_id,int(truncated),source_content_hash or digest(body or '')))
    db.executemany('INSERT INTO revision_display_dependencies VALUES(?,?)',
                   ((revision_id,number) for number in dependencies(body)))


def record_pull_display(db, repository_id, repository_name, pull):
    import re
    from ..factory.contracts import claims_issue, submission
    from ..factory.ownership import forge_branch
    body=pull.get('body') or ''
    numbers={int(number) for pair in re.findall(r'(?<![\w/])#([1-9]\d*)|https://github\.com/'+re.escape(repository_name)+r'/issues/([1-9]\d*)',body) for number in pair if number}
    branch=pull.get('branch') or (pull.get('head') or {}).get('ref') or ''
    match=re.match(r'forge/issue-([1-9]\d*)-',branch)
    if match:numbers.add(int(match[1]))
    try:numbers.add(submission(body)['issue'])
    except (ValueError,TypeError,KeyError):pass
    db.executemany('INSERT INTO inventory_pull_issue_links VALUES(?,?,?,?)',
        ((repository_id,pull['number'],number,int(forge_branch(pull,number))) for number in sorted(numbers)
         if forge_branch(pull,number) or claims_issue(body,repository_name,number)))


def record_label_display(db, issue_id, labels, events):
    db.execute('DELETE FROM issue_label_attributions WHERE issue_id=?',(issue_id,))
    labels_hash=digest(sorted(labels))
    for index,event in enumerate(events[:100]):
        if isinstance(event,dict) and isinstance(event.get('label'),str):
            db.execute('INSERT OR IGNORE INTO issue_label_attributions VALUES(?,?,?,?,?,?)',
                       (issue_id,event['label'],event.get('login'),event.get('at'),index,labels_hash))


def record_approval_display(db, candidate_id, reviews, at, *, source='github'):
    import json
    import uuid
    generation=db.execute("SELECT COALESCE(MAX(collection_generation),0)+1 FROM candidate_observations WHERE candidate_id=? AND kind='approval'",(candidate_id,)).fetchone()[0]
    rows=[{key:value.get(key) for key in ('login','state','at','counted','why')} for value in reviews[:100]]
    db.execute("INSERT INTO candidate_observations(id,candidate_id,kind,collection_generation,fetched_at,complete,details_json) VALUES(?,?,'approval',?,?,1,?)",
               (str(uuid.uuid4()),candidate_id,generation,at,json.dumps({'approvals':rows,'source':source})))
