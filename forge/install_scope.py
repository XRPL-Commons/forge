"""Admission fence for one installation's real smoke issue."""
import json


def pending_in(db):
    row = db.execute("SELECT c.settings_json FROM configuration c JOIN setup_instances s ON s.scope='live' "
                     "AND c.subject=s.generation WHERE c.domain='installation_smoke' AND c.active=1").fetchone()
    value = json.loads(row[0]) if row else None
    if value:
        return value if value['phase'] != 'complete' else None
    row = db.execute("SELECT c.settings_json FROM configuration c JOIN setup_instances s ON s.scope='live' "
                     "AND c.subject=s.generation WHERE c.domain='installation_prepare' AND c.active=1").fetchone()
    if row:
        prepared = json.loads(row[0])
        if prepared['status'] != 'complete':
            plan = prepared['binding']['plan']
            return {'phase': 'preparing', 'binding': {'repository': plan['smoke_repository'],
                    'number': None, 'started_at': plan['started_at']}}
    return None


def pending(store):
    with store.read_snapshot() as db:
        return pending_in(db)


def issue_allowed_in(db, repository_id, number):
    value = pending_in(db)
    return value is None or (value['phase'] != 'preparing' and value['binding']['repository']['id'] == repository_id
                             and value['binding']['number'] == number)


def issue_allowed(store, repository_id, number):
    with store.read_snapshot() as db:
        return issue_allowed_in(db, repository_id, number)


def execution_allowed(store, item):
    with store.read_snapshot() as db:
        value = pending_in(db)
        if value is None:
            return True
        if value['phase'] == 'preparing':
            return False
        if triage_in(db, value['binding']) is None:
            return False
        target = value['binding']['repository']
        issue = item.get('issue') or {}
        repo = item.get('repo') or item.get('repository') or issue.get('repository')
        if item.get('review_only') or repo != target['name']:
            return False
        unit_id = item.get('unit_id') or (item.get('unit') or {}).get('id')
        if unit_id:
            members = list(db.execute('SELECT i.repository_id,i.github_number FROM unit_issues m JOIN issues i ON i.id=m.issue_id '
                                     'WHERE m.unit_id=? AND m.left_at IS NULL', (unit_id,)))
            return len(members) == 1 and tuple(members[0]) == (target['id'], value['binding']['number'])
        return item.get('issue_number', item.get('number', issue.get('github_number'))) == value['binding']['number']


def triage_run_in(db, binding):
    row = db.execute("SELECT r.id,r.execution_id FROM repository_triage_runs r JOIN auxiliary_executions e ON e.id=r.execution_id "
        "JOIN triage_proposal_inputs i ON i.proposal_id=r.id JOIN issues t ON t.id=i.issue_id "
        "WHERE r.repository_id=? AND r.status='complete' AND e.cleanup=1 AND e.created_at>=? "
        "AND t.github_number=? AND NOT EXISTS(SELECT 1 FROM triage_proposal_inputs other WHERE other.proposal_id=r.id AND other.issue_id!=i.issue_id) "
        'ORDER BY r.requested_at DESC LIMIT 1',
        (binding['repository']['id'], binding['started_at'], binding['number'])).fetchone()
    return dict(row) if row else None


def accepted_triage_in(db, binding):
    """An accepted runnable disposition, before external effects finish."""
    # A newer suggestion does not revoke accepted work. Select accepted evidence
    # directly, retaining its requirements and exact delivery membership pins.
    row = db.execute("SELECT run.id,run.execution_id,r.id AS row_id,r.action,i.unit_id "
        "FROM repository_triage_runs run JOIN auxiliary_executions e ON e.id=run.execution_id "
        "JOIN triage_proposals p ON p.id=run.id JOIN triage_proposal_rows r ON p.id=r.proposal_id "
        "JOIN triage_row_members m ON m.row_id=r.id JOIN triage_proposal_inputs i ON i.proposal_id=p.id AND i.issue_id=m.issue_id "
        "JOIN issues t ON t.id=i.issue_id JOIN unit_issues member ON member.id=i.member_id "
        "AND member.issue_id=t.id AND member.unit_id=i.unit_id JOIN units delivery ON delivery.id=member.unit_id "
        "WHERE run.repository_id=? AND run.status='complete' AND e.cleanup=1 AND e.created_at>=? "
        "AND p.status='accepted' AND r.status='accepted' "
        "AND r.action IN ('keep','order','relabel') AND t.repository_id=? AND t.github_number=? "
        "AND t.current_revision_id=i.requirements_revision_id "
        "AND (member.left_at IS NULL OR (member.exit_reason='merged' AND delivery.state='merged')) "
        "AND NOT EXISTS(SELECT 1 FROM triage_proposal_inputs other WHERE other.proposal_id=p.id AND other.issue_id!=i.issue_id) "
        "AND NOT EXISTS(SELECT 1 FROM triage_proposal_rows other WHERE other.proposal_id=p.id AND other.id!=r.id) "
        "AND NOT EXISTS(SELECT 1 FROM unit_issues other WHERE other.unit_id=i.unit_id "
        "AND (other.left_at IS NULL OR other.exit_reason='merged') AND other.id!=member.id) "
        "ORDER BY run.requested_at DESC,run.rowid DESC LIMIT 1",
        (binding['repository']['id'], binding['started_at'], binding['repository']['id'], binding['number'])).fetchone()
    return dict(row) if row else None


def triage_in(db, binding):
    """Only accepted runnable work with confirmed effects opens execution."""
    accepted = accepted_triage_in(db, binding)
    if accepted is None:
        return None
    effects = db.execute("SELECT 1 FROM external_effects e LEFT JOIN triage_effect_authority a ON a.effect_id=e.id "
        "WHERE (a.proposal_id=? AND e.status!='confirmed') OR "
        "(e.unit_id=? AND e.kind='environment_label' AND e.status NOT IN ('confirmed','superseded')) LIMIT 1",
        (accepted['id'], accepted['unit_id'])).fetchone()
    return None if effects else accepted


def check_repositories_in(db, repositories):
    value = pending_in(db)
    if value:
        if value['phase'] == 'preparing':
            if any(row['enabled'] for row in repositories):
                raise ValueError('Complete installation preparation and the smoke merge before enabling repositories')
            return
        target = value['binding']['repository']
        if any(row['enabled'] and (row['id'] != target['id'] or row['name'] != target['name']) for row in repositories):
            raise ValueError('Complete the installation smoke merge before enabling other repositories')
