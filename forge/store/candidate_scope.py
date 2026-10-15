"""The complete requirements scope of one published delivery candidate."""
import json

from .delivery_inputs import approved_in, requirements_hash_in
from .models import TransitionError


def scope_in(db, unit):
    from .review_pulls import review_only_in, source_in
    if review_only_in(db,unit['id']):
        source=source_in(db,unit['current_candidate_id'])
        candidate=db.execute('SELECT requirements_hash FROM candidates WHERE id=?',(unit['current_candidate_id'],)).fetchone()
        if source is None or candidate is None:raise TransitionError('Review-only candidate source is unavailable')
        return {'members':[],'grouped':False,'requirements_hash':candidate['requirements_hash'],
                'number':None,'requirements':{'title':source['title'],'body':source['body']},'review_source':source}
    members=[]
    for row in db.execute('SELECT m.id AS member_id,m.issue_id,m.requirements_revision_id,i.github_number AS number,'
            'r.requirements_json,r.requirements_hash FROM unit_issues m JOIN issues i ON i.id=m.issue_id '
            'JOIN issue_revisions r ON r.id=m.requirements_revision_id WHERE m.unit_id=? AND m.left_at IS NULL ORDER BY m.id', (unit['id'],)):
        members.append({**dict(row),'requirements':json.loads(row['requirements_json'])})
    grouped=approved_in(db,unit['id'])
    if not members or not grouped and len(members)!=1:
        raise TransitionError('Candidate scope has no approved delivery composition')
    requirements_hash=requirements_hash_in(db,unit) if grouped else members[0]['requirements_hash']
    return {'members':members,'grouped':grouped,'requirements_hash':requirements_hash,
            'number':None if grouped else members[0]['number'],
            'requirements':None if grouped else members[0]['requirements']}
