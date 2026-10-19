"""Bounded scheduler and public task projections over immutable task authority."""
import base64
import json

from .models import DeliveryPlanChanged,TransitionError
from .transitions import _charges,_id,applicable_gates
from .task_inputs import (current_plan_in,plan_current_in,plan_tasks_in,task_manifest_in,current_task_output_in,
    builder_manifest_in,scoped_task_gates_in,task_output_ref,member_contributions_in,owner_final_in,environment_conflicts_in,contribution_collection_in)


def assignments_in(db,unit,*,base_oid,policy_hash,limit,allowed_members=None):
    def result(items=(),reason=None):
        return {'unit_id':unit['id'],'version':unit['version'],'items':list(items),'pending_reason':reason}
    if unit['state'] != 'building': return result(reason='not_building')
    if _charges(db,unit)>=3: return result(reason='budget_exhausted')
    if not db.execute('SELECT 1 FROM unit_publication_targets WHERE unit_id=?',(unit['id'],)).fetchone(): return result(reason='not_approved_composition')
    members=[row[0] for row in db.execute('SELECT id FROM unit_issues WHERE unit_id=? AND left_at IS NULL',(unit['id'],))]
    if not members: return result(reason='no_current_members')
    gates=applicable_gates(db,unit,candidate_id=unit['current_candidate_id'])
    if any(g['member_id'] is None and g.get('job_id') is None for g in gates): return result(reason='unit_blocked')
    # The owning builder reservation refuses every memberless gate, including
    # an exact wait on its own previous job such as a Forge problem. Offering
    # that stage anyway made dispatch retry a rejected reservation each cycle.
    owner_blocked=next((g['code'] for g in gates if g['member_id'] is None),None)
    active=[dict(row) for row in db.execute('SELECT j.id,j.cleanup,j.outcome,t.task_id,d.executor FROM jobs j '
        'LEFT JOIN job_tasks t ON t.job_id=j.id LEFT JOIN delivery_tasks d ON d.id=t.task_id '
        'WHERE j.unit_id=? AND (j.cleanup=0 OR j.outcome IS NULL)',(unit['id'],))]
    if any(row['task_id'] is None for row in active): return result(reason='builder_cleanup_pending')
    from .recovery import pending_mandatory
    recovery_pending=pending_mandatory(db,unit['id'])
    plan=current_plan_in(db,unit['id'])
    current=plan_current_in(db,unit,plan,base_oid,policy_hash,whole_members=False)
    if current:
        tasks=plan_tasks_in(db,plan['id'])
        outputs={task['id']:current_task_output_in(db,unit,task['id'],base_oid=base_oid,policy_hash=policy_hash) for task in tasks}
        if all(outputs.values()) and plan_current_in(db,unit,plan,base_oid,policy_hash):
            if recovery_pending: return result(reason='recovery_pending')
            if active: return result(reason='child_cleanup_pending')
            if allowed_members is not None and not set(members)<=allowed_members: return result(reason='member_blocked')
            if scoped_task_gates_in(db,unit,members,candidate_id=unit['current_candidate_id']): return result(reason='member_blocked')
            if owner_blocked: return result(reason=owner_blocked)
            return result([{'stage':'integration','input_manifest':builder_manifest_in(db,unit,'integration',base_oid,policy_hash)}])
        conflicts=environment_conflicts_in(db,unit,plan_id=plan['id'])
        conflicted={row['task_id'] for row in conflicts}
        candidates=[]; needs_plan=bool(conflicts) or not plan_current_in(db,unit,plan,base_oid,policy_hash)
        for task in tasks:
            if outputs[task['id']]: continue
            if task['id'] in conflicted: continue
            from .steward_execution import task_retry_in
            retry = task_retry_in(db, unit, plan['id'], task['id'])
            if not retry and db.execute('SELECT 1 FROM job_tasks WHERE plan_id=? AND task_id=?',(plan['id'],task['id'])).fetchone():
                needs_plan=True; continue
            scope={pin['member_id'] for pin in task['members']}
            if allowed_members is not None and not scope<=allowed_members: continue
            if scoped_task_gates_in(db,unit,scope): continue
            if any(row['task_id']==task['id'] for row in active): continue
            if task['executor']=='owner' and any(row['executor']=='owner' for row in active): continue
            if any(outputs.get(predecessor) is None for predecessor in task['dependencies']): continue
            try: manifest=task_manifest_in(db,unit,plan,task['id'])
            except TransitionError:
                needs_plan=True; continue
            candidates.append({'stage':'task','plan_id':plan['id'],'task_id':task['id'],'key':task['key'],'executor':task['executor'],
                'member_ids':sorted(scope),'worktree_key':_id(retry['decision_id'] if retry else plan['id'],'worktree:'+task['id']),
                'input_manifest':manifest})
        if candidates: return result(candidates[:limit])
        if not needs_plan or active: return result(reason='children_running' if active else 'member_blocked')
    if active: return result(reason='children_running')
    if recovery_pending: return result(reason='recovery_pending')
    # Planning is read-only and may propose independent work around scoped blockers.
    if all(scoped_task_gates_in(db,unit,[member]) for member in members): return result(reason='member_blocked')
    if owner_blocked: return result(reason=owner_blocked)
    return result([{'stage':'planning','input_manifest':builder_manifest_in(db,unit,'planning',base_oid,policy_hash)}])


def task_page_in(db,unit,*,task_after='',task_limit=50):
    plan=current_plan_in(db,unit['id'])
    after=''
    if task_after:
        try:
            value=json.loads(base64.urlsafe_b64decode(task_after+'='*(-len(task_after)%4)))
            if not isinstance(value,list) or len(value)!=2 or any(not isinstance(x,str) for x in value): raise ValueError()
        except (ValueError,UnicodeError):
            raise ValueError('invalid task cursor') from None
        if not plan or value[0]!=plan['id']: raise DeliveryPlanChanged('delivery plan changed')
        after=value[1]
    tasks=plan_tasks_in(db,plan['id']) if plan else []
    parent=json.loads(plan['input_manifest_json']) if plan else {}
    selected=[task for task in tasks if task['id']>after]
    page=[]
    for task in selected[:task_limit]:
        output=current_task_output_in(db,unit,task['id'],base_oid=parent['base_oid'],policy_hash=parent['policy_hash'])
        jobs=[dict(row) for row in db.execute('SELECT j.id,j.phase,j.outcome,j.cleanup,j.fenced FROM jobs j JOIN job_tasks t ON t.job_id=j.id WHERE t.task_id=? ORDER BY j.created_at,j.id',(task['id'],))]
        active=[row for row in jobs if not row['cleanup'] or row['outcome'] is None]
        reason=None; status='queued'
        if output: status='accepted'
        elif (not plan_current_in(db,unit,plan,parent['base_oid'],parent['policy_hash'],whole_members=False)
              or any(not db.execute('SELECT 1 FROM unit_issues WHERE id=? AND requirements_revision_id=? AND left_at IS NULL',
                     (pin['member_id'],pin['requirements_revision_id'])).fetchone() for pin in task['members'])):
            status,reason='pending','plan_changed'
        elif active:
            status='working'
            if any(not row['cleanup'] and row['outcome'] for row in active): reason='cleanup_pending'
            elif any(row['cleanup'] and row['outcome'] is None for row in active): reason='result_pending'
        elif any(row['task_id']==task['id'] for row in environment_conflicts_in(db,unit,plan_id=plan['id'])): status,reason='blocked','environment_conflict'
        elif jobs: status,reason='failed','failed_requires_plan'
        elif scoped_task_gates_in(db,unit,[pin['member_id'] for pin in task['members']]): status,reason='blocked','member_blocked'
        elif any(current_task_output_in(db,unit,x,base_oid=parent['base_oid'],policy_hash=parent['policy_hash']) is None for x in task['dependencies']): status,reason='pending','prerequisite_output_pending'
        page.append({key:task[key] for key in ('id','key','executor','scope','members','dependencies')}|
                    {'output':task_output_ref(output) if output else None,'status':status,'pending_reason':reason,'active_job_ids':[job['id'] for job in active]})
    complete=len(selected)<=task_limit
    cursor=None if complete else base64.urlsafe_b64encode(json.dumps([plan['id'],page[-1]['id']],separators=(',',':')).encode()).decode().rstrip('=')
    contributions=contribution_collection_in(db,unit,base_oid=parent.get('base_oid'),policy_hash=parent.get('policy_hash'))
    return {'response_version':2,'plan':{'id':plan['id'],'version':json.loads(plan['plan_json']).get('version',1),'owner_final':owner_final_in(plan)} if plan else None,
            'tasks':page,'task_next_cursor':cursor,'tasks_complete':complete,'contributions':contributions}
