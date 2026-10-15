"""Explicit operator acknowledgments preserve every other captured member claim."""
import json


def typed_claim(gate, details):
    return (gate['job_id'] is not None and details.get('source')=='build_claim'
            and gate['code'] in {'dependency','decision_required'}
            and isinstance(details.get('requirements_by_member'),dict))


def matching_pin_in(db,unit_id,member_id,revision_id):
    return db.execute('SELECT 1 FROM unit_issues WHERE unit_id=? AND id=? AND requirements_revision_id=? AND left_at IS NULL',
        (unit_id,member_id,revision_id)).fetchone() is not None


def fresh_pin_in(db,unit_id,member_id,revision_id):
    return db.execute('SELECT 1 FROM unit_issues m JOIN issues i ON i.id=m.issue_id '
        'JOIN issue_revisions current ON current.id=i.current_revision_id JOIN issue_revisions pinned ON pinned.id=m.requirements_revision_id '
        'WHERE m.unit_id=? AND m.id=? AND m.requirements_revision_id=? AND m.left_at IS NULL '
        'AND current.requirements_hash=pinned.requirements_hash',(unit_id,member_id,revision_id)).fetchone() is not None


def dependencies_in(db,unit_id,member_id,revision_id=None):
    if revision_id is None:
        row=db.execute('SELECT requirements_revision_id FROM unit_issues WHERE unit_id=? AND id=? AND left_at IS NULL',(unit_id,member_id)).fetchone()
        revision_id=row[0] if row else None
    if not fresh_pin_in(db,unit_id,member_id,revision_id):
        return []
    requirement=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(revision_id,)).fetchone()[0])
    result=[]
    for gate in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='dependency' AND resolved_at IS NULL",(unit_id,)):
        details=json.loads(gate['details_json'])
        if typed_claim(gate,details):
            if details['requirements_by_member'].get(member_id)==revision_id and details.get('references'):
                result.append(gate)
        elif (details.get('source')!='runtime_dependencies' and details.get('requirements')==requirement
              and details.get('issues') and gate['job_id'] is None
              and (gate['member_id']==member_id or gate['member_id'] is None and db.execute(
                  'SELECT COUNT(*) FROM unit_issues WHERE unit_id=? AND left_at IS NULL',(unit_id,)).fetchone()[0]==1)):
            result.append(gate)
    return result


def acknowledge_in(lifecycle,db,command,unit,gate,member_id,revision_id,evidence):
    """Return False for an unrelated typed gate; never widen its selected scope."""
    details=json.loads(gate['details_json'])
    if not typed_claim(gate,details):
        return False
    if details['requirements_by_member'].get(member_id)!=revision_id or not fresh_pin_in(db,unit['id'],member_id,revision_id):
        return False
    remaining={identifier:revision for identifier,revision in details['requirements_by_member'].items()
        if identifier!=member_id and matching_pin_in(db,unit['id'],identifier,revision)}
    receipt={**evidence,'original_gate_id':gate['id'],'acknowledged_member_id':member_id,
        'requirements_revision_id':revision_id,'captured_job_id':gate['job_id'],
        'claim_types':[claim['type'] for claim in details.get('claims',[])] or ['wait_on_issue'],
        'acknowledged_claims':details.get('claims',[]),'acknowledged_references':details.get('references',[])}
    lifecycle._resolve(db,command,gate['id'],receipt,**({'resolution':'superseded'} if remaining else {}))
    if remaining:
        lifecycle._open_gate(db,command,gate['code'],{**details,'requirements_by_member':remaining,
            'previous_gate_id':gate['id']},job_id=gate['job_id'])
    return True
