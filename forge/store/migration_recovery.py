"""Upgrade imported investigation obligations without replaying legacy launches."""
import json

from .artifacts import Artifacts
from .models import TransitionError
from .transitions import _json, manifest_hash

SOURCE='state/factory/factory-ledger.json'


def pending_in(db,unit_id):
    rows=db.execute("SELECT f.*,j.id AS job_id,j.role AS job_role,j.outcome,j.cleanup,a.input_manifest_json "
        "FROM attempt_failures f JOIN jobs j ON j.attempt_id=f.attempt_id JOIN attempts a ON a.id=f.attempt_id "
        "WHERE f.unit_id=? AND (f.investigation IS NULL OR f.investigation!='complete') "
        "AND NOT EXISTS(SELECT 1 FROM migration_recovery_adoptions m WHERE m.failed_job_id=j.id) ORDER BY f.position LIMIT 101",(unit_id,)).fetchall()
    if len(rows)>100:raise TransitionError('imported investigation history exceeds reconciliation bound')
    answer=[]
    for row in rows:
        value=dict(row);manifest=json.loads(value['input_manifest_json'])
        if (value['recovery_required'] or not value['cleanup'] or value['job_role'] not in {'build','review','audit'}
                or value['outcome'] not in {'failed','failure','rejected'} or manifest.get('legacy_job_id')!=value['job_id']
                or not db.execute('SELECT 1 FROM import_runs WHERE id=?',(manifest.get('source_manifest'),)).fetchone()
                or db.execute('SELECT 1 FROM job_launches WHERE job_id=?',(value['job_id'],)).fetchone()):
            raise TransitionError('legacy failure cannot be adopted without exact cleaned import provenance')
        value['source_manifest']=manifest['source_manifest']
        value['disposition']='investigate' if value['investigation']=='pending' else 'operator_required'
        answer.append(value)
    return answer


def unresolved_results_in(db,unit_id,records):
    interrupted={row['investigation'] for row in records if row['disposition']=='operator_required'}
    interrupted.update(json.loads(row[0]).get('investigation') for row in db.execute(
        "SELECT m.original_json FROM migration_recovery_adoptions m JOIN jobs j ON j.id=m.failed_job_id WHERE j.unit_id=? AND m.disposition='operator_required'",(unit_id,)))
    rows=db.execute('SELECT id,role FROM jobs WHERE unit_id=? AND outcome IS NULL LIMIT 101',(unit_id,)).fetchall()
    return len(rows)>100 or any(row['role']!='recovery' or row['id'] not in interrupted for row in rows)


def retry_phase(records,candidate):
    if not records:return None
    stage=records[-1]['retry_stage']
    phase={'ready':'building','built':'reviewing','reviewed':'auditing','audited':'merging',
           'build':'building','review':'reviewing','audit':'auditing'}.get(stage)
    if phase is None or (phase!='building' and not candidate):
        raise TransitionError('legacy retry phase requires an explicit operator decision')
    return phase


def phase_in(db,unit,records):
    if records:return retry_phase(records,bool(unit['current_candidate_id']))
    if unit['current_candidate_id'] and unit['state'] in {'queued','building'}:
        row=db.execute("SELECT details_json FROM unit_gates WHERE unit_id=? AND code='legacy_reconciliation' AND resolved_at IS NULL",(unit['id'],)).fetchone()
        original=json.loads(row[0]).get('legacy_state') if row else None
        if original=='ready':return 'building'
        if original in {'hold','failed','investigating'}:
            failure=db.execute('SELECT retry_stage FROM attempt_failures WHERE unit_id=? ORDER BY position DESC LIMIT 1',(unit['id'],)).fetchone()
            if failure:return retry_phase([dict(failure)],True)
    return None


def advance_in(commands,db,command,unit):
    if unit['state'] in {'closed','merged','dissolved'}:return
    if db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='recovery_pending' AND resolved_at IS NULL",(unit['id'],)).fetchone():return
    row=db.execute("SELECT j.id FROM migration_recovery_adoptions m JOIN jobs j ON j.id=m.failed_job_id "
        "JOIN attempt_failures f ON f.attempt_id=j.attempt_id WHERE j.unit_id=? AND m.disposition='investigate' "
        "AND f.investigation='pending' ORDER BY f.position LIMIT 1",(unit['id'],)).fetchone()
    if row:
        commands._open_gate(db,command,'recovery_pending',{'job_id':row['id'],'source':'migration_recovery'},commands._singleton(db,unit)['id'])


def adopt_in(commands,db,command,unit,records):
    current=pending_in(db,unit['id'])
    if records!=current:raise TransitionError('legacy failure changed during migration observation')
    for value in current:
        db.execute('INSERT INTO migration_recovery_adoptions VALUES(?,?,?,?,?,?)',
            (value['job_id'],value['source_manifest'],value['disposition'],command.actor_id,_json(value),commands.clock()))
        if value['disposition']=='investigate':
            db.execute('UPDATE attempt_failures SET recovery_required=1 WHERE attempt_id=?',(value['attempt_id'],))
        elif not db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='decision_required' AND resolved_at IS NULL",(unit['id'],)).fetchone():
            commands._open_gate(db,command,'decision_required',{'source':'migration_recovery','failed_job_id':value['job_id'],
                'reason':'An earlier investigation was incomplete or interrupted. Review its preserved evidence and record a retry decision.'},commands._singleton(db,unit)['id'])
        commands._event(db,command,'lifecycle_changed',{'migration_investigation_adopted':value['job_id'],
            'disposition':value['disposition'],'source_manifest':value['source_manifest']},job_id=value['job_id'])
    for gate in db.execute("SELECT id,details_json FROM unit_gates WHERE unit_id=? AND code='recovery_pending' AND resolved_at IS NULL",(unit['id'],)).fetchall():
        if json.loads(gate['details_json']).get('source')==SOURCE:
            if not current:raise TransitionError('imported recovery wait has no accountable failure')
            commands._resolve(db,command,gate['id'],{'migration_adoptions':[r['job_id'] for r in current]})
    context=db.execute('SELECT reason FROM unit_context WHERE unit_id=?',(unit['id'],)).fetchone()
    if context and context[0]=='Repeated failure requires a supported change of approach' and not db.execute(
            "SELECT 1 FROM unit_gates WHERE unit_id=? AND code='approach_required' AND resolved_at IS NULL",(unit['id'],)).fetchone():
        commands._open_gate(db,command,'approach_required',{'source':'migration_recovery','reason':context[0]},commands._singleton(db,unit)['id'])
    advance_in(commands,db,command,unit)


def original_job(store,job_id):
    """A diagnostic projection, never an executable copy of the old assignment."""
    with store.read_snapshot() as db:
        row=db.execute("SELECT j.*,u.version AS unit_version,u.state AS unit_state,u.current_candidate_id,u.base_branch AS target_branch,"
            "r.id AS repo_id,r.full_name AS repo,m.issue_id AS issue_key,i.github_number AS issue_number,a.input_manifest_json,"
            "m.requirements_revision_id,v.requirements_json,x.original_json FROM migration_recovery_adoptions x "
            "JOIN jobs j ON j.id=x.failed_job_id JOIN units u ON u.id=j.unit_id JOIN repositories r ON r.id=u.repository_id "
            "JOIN attempts a ON a.id=j.attempt_id JOIN unit_issues m ON m.id=j.member_id JOIN issues i ON i.id=m.issue_id "
            "JOIN issue_revisions v ON v.id=m.requirements_revision_id WHERE j.id=? AND x.disposition='investigate'",(job_id,)).fetchone()
        if row is None:raise KeyError(job_id)
        value=dict(row);value['input_manifest']=json.loads(value.pop('input_manifest_json'));value['requirements']=json.loads(value.pop('requirements_json'))
        value.pop('original_json');value['candidate']=None;value['reviewers_requested']=None
        if value['current_candidate_id']:
            candidate=db.execute('SELECT * FROM candidates WHERE id=?',(value['current_candidate_id'],)).fetchone()
            value['candidate']=dict(key=candidate['id'],pr=candidate['pull_number'],branch=candidate['branch'],head=candidate['head_oid'],base=candidate['base_oid'])
            from .operator_views import reviewer_request_in
            value['reviewers_requested']=reviewer_request_in(db,candidate['id'])
    assignment=json.loads(Artifacts(store).read(job_id,'launch_input').content)
    if manifest_hash(assignment)!=value['input_manifest']['assignment_digest']:
        raise TransitionError('imported assignment diagnostic identity changed')
    branch=assignment.get('branch')
    if not isinstance(branch,str) or not branch:raise TransitionError('imported recovery lacks an identified branch')
    value.update(branch=branch,migrated_recovery=True)
    return value
