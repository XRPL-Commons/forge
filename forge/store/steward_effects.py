"""Durable issue mutations derived only from an operator-selected steward choice."""
import json

from ..steward_contract import digest, require
from .steward import decision_in
from .steward_dossier import authority_in
from .transitions import _id, _json, _unit
from .runtime import writable


EXTERNAL_ACTIONS=frozenset({'split','drop','fold','question','escalate_forge'})


def queue_in(db,command,unit,*,decision_id,choice,dossier):
    writable(db)
    selected=db.execute('SELECT s.*,d.actor_id FROM steward_selections s JOIN decisions d ON d.id=s.command_id WHERE s.decision_id=?',(decision_id,)).fetchone()
    require(selected and selected['selected_by']=='operator' and selected['actor_id'].startswith('github:'),
            'Remote issue changes require an authenticated operator choice')
    require(selected['choice_id']==choice['id'] and selected['choice_digest']==digest(choice), 'Selected issue action changed')
    require(choice['action'] in EXTERNAL_ACTIONS, 'Unsupported steward issue action')
    require(authority_in(db,unit)==dossier['authority'], 'Steward issue scope changed')
    members=[dict(row) for row in db.execute('SELECT m.id AS member_id,m.issue_id,m.requirements_revision_id,i.repository_id,i.github_number AS number,'
        'i.labels_json,i.assignees_json,i.assignee_types_json,r.full_name AS repo FROM unit_issues m JOIN issues i ON i.id=m.issue_id '
        'JOIN repositories r ON r.id=i.repository_id WHERE m.unit_id=? AND m.left_at IS NULL ORDER BY m.id',(unit['id'],))]
    require(members and len(members)<=100, 'Steward issue action requires bounded complete scope')
    if choice['action']=='fold':
        target=choice['parameters']
        require(not any(m['repository_id']==target['repository_id'] and m['number']==target['issue_number'] for m in members),
                'Cannot fold a delivery into one of its own issues')
    require(not db.execute('SELECT 1 FROM steward_effect_groups WHERE decision_id=?',(decision_id,)).fetchone(), 'Steward issue action already queued')
    db.execute('INSERT INTO steward_effect_groups VALUES(?,?,?,NULL)',(decision_id,selected['actor_id'],_json(members)))
    params=choice['parameters'];steps=[]
    if choice['action']=='split':
        steps.extend({'operation':'create','child':child} for child in params['children'])
    elif choice['action']=='fold':
        target=db.execute('SELECT full_name FROM repositories WHERE id=?',(params['repository_id'],)).fetchone()
        require(target is not None,'Fold target repository must be configured')
        steps.append({'operation':'target_comment','item':{'repo_id':params['repository_id'],'repo':target[0],'number':params['issue_number']}})
    elif choice['action']=='escalate_forge':
        steps.append({'operation':'report_wait','signature':params['signature']})
    if choice['action']!='escalate_forge':
        steps.extend({'operation':'source_comment','member_id':member['member_id']} for member in members)
    if choice['action'] in {'split','drop','fold'}:
        steps.extend({'operation':'close','member_id':member['member_id']} for member in members)
    effects=[]
    for position,step in enumerate(steps):
        identifier=_id(decision_id,'issue-effect:'+str(position));effects.append(identifier)
        db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,payload_json,created_at,updated_at) VALUES(?,?,?,'steward_issue','pending',?,?,?)",
                   (identifier,identifier,unit['id'],_json(step),dossier['created_at'],dossier['created_at']))
        db.execute('INSERT INTO steward_issue_effects VALUES(?,?,?)',(identifier,decision_id,position))
    return {'effect_id':effects[0],'effect_ids':effects,'status':'pending'}


def row_in(db,effect_id):
    row=db.execute('SELECT e.*,s.decision_id AS steward_decision_id,s.position,g.actor_id,g.scope_json,g.completed_at FROM external_effects e '
        'JOIN steward_issue_effects s ON s.effect_id=e.id JOIN steward_effect_groups g ON g.decision_id=s.decision_id WHERE e.id=?',(effect_id,)).fetchone()
    require(row is not None,'Unknown steward issue effect')
    result=dict(row);result['decision_id']=result.pop('steward_decision_id')
    result['payload']=json.loads(row['payload_json']);result['scope']=json.loads(row['scope_json'])
    _,output,dossier=decision_in(db,result['decision_id']);result['dossier']=dossier
    selected=db.execute('SELECT choice_id FROM steward_selections WHERE decision_id=?',(result['decision_id'],)).fetchone()
    result['choice']=next(c for c in output['choices'] if c['id']==selected[0])
    return result


def _observed_closure_in(db,row,member_id):
    """A closure observation may change exactly the selected issue's state."""
    pin=next((m for m in row['dossier']['authority']['members'] if m['member_id']==member_id),None)
    scope=next((m for m in row['scope'] if m['member_id']==member_id),None)
    issue=db.execute('SELECT i.*,m.requirements_revision_id AS member_revision FROM unit_issues m '
                     'JOIN issues i ON i.id=m.issue_id WHERE m.id=? AND m.unit_id=? AND m.left_at IS NULL',
                     (member_id,row['unit_id'])).fetchone()
    return bool(pin and scope and issue and pin['github_state']=='open'
        and issue['github_state']=='closed' and issue['state_reason']=='not_planned'
        and issue['version']==pin['issue_version']+1
        and issue['current_revision_id']==pin['current_revision_id']
        and issue['member_revision']==pin['requirements_revision_id']
        and issue['labels_json']==scope['labels_json'] and issue['assignees_json']==scope['assignees_json']
        and json.loads(issue['assignee_types_json'])==json.loads(scope['assignee_types_json']))


def closure_obligation_in(db,unit_id,member_id):
    """An already-sent closure stays owned by its durable outbox reconciliation.

    Observation is not confirmation. The effect controller must still read back
    the outcome and validate every independent authority pin before completion.
    """
    rows=db.execute("SELECT e.id FROM external_effects e JOIN steward_issue_effects s ON s.effect_id=e.id "
        "JOIN steward_effect_groups g ON g.decision_id=s.decision_id WHERE e.unit_id=? AND g.completed_at IS NULL "
        "AND e.status IN ('uncertain','confirmed') AND e.attempts>0 "
        "AND json_extract(e.payload_json,'$.operation')='close' AND json_extract(e.payload_json,'$.member_id')=?",
        (unit_id,member_id)).fetchall()
    for effect in rows:
        row=row_in(db,effect['id'])
        if _observed_closure_in(db,row,member_id):
            return effect['id']
    return None


def current_in(db,row):
    """Own confirmed closures may refresh GitHub state; other authority stays pinned."""
    unit=_unit(db,row['unit_id']);current=authority_in(db,unit)
    expected=json.loads(_json(row['dossier']['authority']))
    closed={json.loads(r[0])['member_id'] for r in db.execute("SELECT e.payload_json FROM external_effects e JOIN steward_issue_effects s ON s.effect_id=e.id WHERE s.decision_id=? AND e.status='confirmed' AND json_extract(e.payload_json,'$.operation')='close'",(row['decision_id'],))}
    for member in current['members']:
        if member['member_id'] in closed and _observed_closure_in(db,row,member['member_id']):
            old=next(m for m in expected['members'] if m['member_id']==member['member_id'])
            member['github_state']=old['github_state'];member['issue_version']=old['issue_version']
    # The shared bounded-effect gate belongs to this effect, not a new approval.
    current['gates']=[gate for gate in current['gates'] if not db.execute(
        "SELECT 1 FROM unit_gates WHERE id=? AND code='forge_problem' AND json_extract(details_json,'$.source')='scheduling' AND json_extract(details_json,'$.effect_id')=?",(gate['id'],row['id'])).fetchone()]
    require(unit['state'] not in {'merged','closed','dissolved'} and current==expected,'Accepted steward issue authority changed')
    require(not db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0',(unit['id'],)).fetchone(),'Worker cleanup is pending')
    require(not db.execute("SELECT 1 FROM external_effects WHERE unit_id=? AND status IN ('pending','running','uncertain') AND kind!='steward_issue'",(unit['id'],)).fetchone(),'Another effect is unsettled')
    return unit
