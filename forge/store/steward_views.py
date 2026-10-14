"""Lazy steward decisions and lightweight card markers from retained receipts."""
import json

from .queries import Queries
from .steward import decision_in, selection_in
from .steward_dossier import authority_in
from .transitions import _unit


def markers_in(db, unit_ids):
    # One indexed latest-decision lookup per requested unit; no artifact bodies.
    rows = db.execute("""
        SELECT d.unit_id,d.decision_id,d.output_digest AS digest,
          CASE WHEN a.decision_id IS NOT NULL AND (g.decision_id IS NULL OR g.completed_at IS NOT NULL) THEN 'applied'
               WHEN u.state IN ('merged','closed','dissolved') THEN 'stale'
               WHEN s.decision_id IS NOT NULL THEN 'selected' ELSE 'proposed' END AS status
        FROM json_each(?) p JOIN steward_decisions d ON d.decision_id=(
          SELECT decision_id FROM steward_decisions WHERE unit_id=p.value
          AND trigger_key NOT LIKE 'brief:%' ORDER BY accepted_at DESC,decision_id DESC LIMIT 1)
        JOIN units u ON u.id=d.unit_id
        LEFT JOIN steward_selections s ON s.decision_id=d.decision_id
        LEFT JOIN steward_applications a ON a.decision_id=d.decision_id
        LEFT JOIN steward_effect_groups g ON g.decision_id=d.decision_id
    """, (json.dumps(list(unit_ids)),))
    return {row['unit_id']: {key: row[key] for key in ('decision_id','digest','status')} for row in rows}


def marker_in(db, unit_id):
    return markers_in(db, [unit_id]).get(unit_id) if unit_id else None


def view_in(db, key):
    issue = Queries._issue(db, key)
    unit_id = issue['lifecycle']['unit_id']
    marker = marker_in(db, unit_id)
    if marker is None:
        return {'decision': None, 'repositories': [], 'scope': None}
    row, output, dossier = decision_in(db, marker['decision_id'])
    selected = selection_in(db, row['decision_id'])
    unit = _unit(db, unit_id)
    status = marker['status']
    if not selected['application'] and (unit['state'] in {'merged','closed','dissolved'}
                                or authority_in(db, unit) != dossier['authority']):
        status = 'stale'
    application = None
    if selected['selection']:
        request = db.execute('SELECT request_id FROM decisions WHERE id=?',
                             (selected['selection']['command_id'],)).fetchone()
        application = {'request_id': request[0], 'choice_id': selected['selection']['choice_id'],
                       'status': 'applied' if status == 'applied' else 'pending', 'message': None}
        failed = db.execute("SELECT 1 FROM steward_issue_effects s JOIN external_effects e ON e.id=s.effect_id "
                            "WHERE s.decision_id=? AND e.status IN ('failed','superseded')", (row['decision_id'],)).fetchone()
        if failed:
            application.update(status='failed', message='The selected action needs operator attention.')
    references = {issue['repository_id']} | {choice['parameters']['repository_id']
        for choice in output['choices'] if 'repository_id' in choice['parameters']}
    repositories = [dict(row) for row in db.execute(
        'SELECT id,full_name AS name FROM repositories WHERE id IN (SELECT value FROM json_each(?)) ORDER BY id',
        (json.dumps(sorted(references)),))]
    scope = {'unit_id': unit_id, 'issues': [dict(row) for row in db.execute(
        'SELECT i.id AS key,i.repository_id,i.github_number AS number,r.title FROM json_each(?) p '
        "JOIN unit_issues m ON m.id=p.value->>'member_id' JOIN issues i ON i.id=m.issue_id "
        "JOIN issue_revisions r ON r.id=p.value->>'requirements_revision_id' ORDER BY i.github_number",
        (json.dumps(dossier['authority']['members']),))]}
    return {'scope': scope, 'decision': {**marker, 'status': status,
        **{field: output[field] for field in ('version','choices','recommended_choice_id','confidence')},
        'brief': selected['brief'], 'application': application}, 'repositories': repositories}
