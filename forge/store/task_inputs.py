"""Immutable task definitions and exact contribution joins for approved deliveries.

These readers derive execution readiness from the same rows used by commands.
They never turn member completion into an invented representative output.
"""
import json

from .models import TransitionError, InvalidDelegationPlan


def task_members_in(db, task_id):
    return [dict(row) for row in db.execute('SELECT member_id,requirements_revision_id FROM delivery_task_members WHERE task_id=? ORDER BY member_id',(task_id,))]


def task_dependencies_in(db, task_id):
    return [row[0] for row in db.execute('SELECT prerequisite_task_id FROM delivery_task_dependencies WHERE task_id=? ORDER BY prerequisite_task_id',(task_id,))]


def task_in(db, task_id):
    row=db.execute('SELECT t.*,b.unit_id FROM delivery_tasks t JOIN unit_builders b ON b.id=t.builder_id WHERE t.id=?',(task_id,)).fetchone()
    if row is None:
        raise TransitionError('unknown delivery task')
    return {**dict(row),'key':row['task_key'],'members':task_members_in(db,task_id),
            'dependencies':task_dependencies_in(db,task_id),'internalizes':json.loads(row['internalizes_json'])}


def task_output_ref(output):
    return {key:output[key] for key in ('task_id','attempt_id','commit_oid','input_manifest_hash')}|{'output_id':output['id']}


def current_plan_in(db, unit_id):
    # Plan acceptance and this event commit atomically, including legacy plans.
    row=db.execute("SELECT p.*,a.input_manifest_json,a.assignment_generation,a.membership_generation,a.budget_generation,j.cleanup,j.outcome "
        "FROM delegation_plans p JOIN unit_builders b ON b.id=p.builder_id JOIN jobs j ON j.id=p.parent_job_id JOIN attempts a ON a.id=j.attempt_id "
        "JOIN events e ON e.job_id=j.id AND e.kind='attempt_finished' WHERE b.unit_id=? AND j.outcome='planned' "
        "ORDER BY e.sequence DESC LIMIT 1",(unit_id,)).fetchone()
    return dict(row) if row else None


def plan_tasks_in(db, plan_id):
    return [task_in(db,row[0]) for row in db.execute('SELECT task_id FROM delegation_plan_tasks WHERE plan_id=? ORDER BY task_id',(plan_id,))]


def owner_final_in(plan):
    value=json.loads(plan['plan_json'])
    return value['owner_final'] if value.get('version')==2 else {'member_ids':[],'scope':'','internalizes':[]}


def current_task_output_in(db, unit, task_id, *, base_oid=None, policy_hash=None, visiting=(), _cache=None):
    cache={} if _cache is None else _cache
    key=(task_id,base_oid,policy_hash)
    active=set(visiting); tasks={}; stack=[(task_id,False)]
    while stack:
        identity,expanded=stack.pop(); current_key=(identity,base_oid,policy_hash)
        if current_key in cache: continue
        if expanded:
            cache[current_key]=_read_task_output_in(db,unit,identity,base_oid=base_oid,policy_hash=policy_hash,
                                                  _cache=cache,task=tasks[identity])
            active.remove(identity)
            continue
        if identity in active: raise TransitionError('retained task dependency cycle')
        task=tasks[identity]=task_in(db,identity); active.add(identity)
        stack.append((identity,True))
        for predecessor in reversed(task['dependencies']):
            stack.append((predecessor,False))
    return cache[key]


def _read_task_output_in(db, unit, task_id, *, base_oid=None, policy_hash=None, _cache, task):
    if task['unit_id']!=unit['id']:
        raise TransitionError('task belongs to another delivery')
    for pin in task['members']:
        member=db.execute('SELECT requirements_revision_id,left_at FROM unit_issues WHERE id=?',(pin['member_id'],)).fetchone()
        if member is None or member['left_at'] is not None or member['requirements_revision_id']!=pin['requirements_revision_id']:
            return None
    predecessors=[]
    for predecessor in task['dependencies']:
        output=_cache[(predecessor,base_oid,policy_hash)]
        if output is None:
            return None
        predecessors.append(output)
    for row in db.execute('SELECT p.*,a.input_manifest_json FROM part_outputs p JOIN attempts a ON a.id=p.attempt_id '
            'WHERE p.task_id=? AND NOT EXISTS(SELECT 1 FROM part_output_invalidations i WHERE i.output_id=p.id) '
            'ORDER BY p.accepted_at DESC,p.id DESC',(task_id,)):
        manifest=json.loads(row['input_manifest_json'])
        refs=manifest['prerequisite_outputs']
        if ({ref['output_id'] for ref in refs}!={item['id'] for item in predecessors}
                or base_oid is not None and row['base_oid']!=base_oid
                or policy_hash is not None and manifest['policy_hash']!=policy_hash):
            continue
        # Validate complete refs, including historical v1 shapes, without rewriting evidence.
        by_id={item['id']:item for item in predecessors}
        if all(all(by_id[ref['output_id']][key]==value for key,value in ref.items() if key!='output_id') for ref in refs):
            return dict(row)
    return None


def plan_current_in(db,unit,plan,base_oid=None,policy_hash=None,*,whole_members=True):
    from .delivery_inputs import member_pins_in,requirements_hash_in
    if not plan or not plan['cleanup'] or plan['outcome']!='planned':
        return False
    parent=json.loads(plan['input_manifest_json'])
    return (plan['assignment_generation']==unit['fencing_generation'] and plan['membership_generation']==unit['membership_generation']
        and plan['budget_generation']==unit['budget_generation']
        and (not whole_members or parent['members']==member_pins_in(db,unit) and parent['requirements_hash']==requirements_hash_in(db,unit))
        and (base_oid is None or parent['base_oid']==base_oid) and (policy_hash is None or parent['policy_hash']==policy_hash))


def integration_evidence_in(db,unit,plan,outputs,*,base_oid=None,policy_hash=None):
    if not unit['current_candidate_id']:
        return None
    row=db.execute("SELECT c.*,a.input_manifest_json FROM candidates c JOIN candidate_evidence e ON e.candidate_id=c.id "
        "AND e.kind='publication' AND e.verdict='pass' JOIN attempts a ON a.id=e.attempt_id "
        "JOIN jobs j ON j.attempt_id=a.id JOIN builder_assignments b ON b.job_id=j.id AND b.stage='integration' "
        "WHERE c.id=?",(unit['current_candidate_id'],)).fetchone()
    if row is None:
        return None
    manifest=json.loads(row['input_manifest_json'])
    from .delivery_inputs import member_pins_in,requirements_hash_in
    if (manifest.get('plan_id')!=plan['id'] or manifest['members']!=member_pins_in(db,unit)
            or manifest['requirements_hash']!=requirements_hash_in(db,unit)
            or {ref['output_id'] for ref in manifest['part_outputs']}!={output['id'] for output in outputs}
            or base_oid is not None and row['base_oid']!=base_oid
            or policy_hash is not None and row['policy_hash']!=policy_hash):
        return None
    return row['id']


def contribution_collection_in(db,unit,*,base_oid=None,policy_hash=None):
    plan=current_plan_in(db,unit['id'])
    member_ids=[row[0] for row in db.execute('SELECT id FROM unit_issues WHERE unit_id=? AND left_at IS NULL ORDER BY id',(unit['id'],))]
    values={member:{'plan_id':plan['id'] if plan else None,'member_id':member,'task_ids':[],'outputs':[],
                    'ready':False,'owner_final_pending':False,'candidate_id':None} for member in member_ids}
    if not plan: return values
    parent=json.loads(plan['input_manifest_json'])
    base_oid=parent['base_oid'] if base_oid is None else base_oid
    policy_hash=parent['policy_hash'] if policy_hash is None else policy_hash
    if not plan_current_in(db,unit,plan,base_oid,policy_hash,whole_members=False): return values
    tasks=plan_tasks_in(db,plan['id']); cache={}
    outputs={task['id']:current_task_output_in(db,unit,task['id'],base_oid=base_oid,policy_hash=policy_hash,_cache=cache) for task in tasks}
    candidate=(integration_evidence_in(db,unit,plan,list(outputs.values()),base_oid=base_oid,policy_hash=policy_hash)
               if all(output is not None for output in outputs.values()) else None)
    final_scope=set(owner_final_in(plan)['member_ids'])
    for member in member_ids:
        selected=[task for task in tasks if any(pin['member_id']==member for pin in task['members'])]
        final=member in final_scope; pending=bool(final and candidate is None)
        values[member].update(task_ids=[task['id'] for task in selected],
            outputs=[task_output_ref(outputs[task['id']]) for task in selected if outputs[task['id']] is not None],
            ready=bool((selected or final) and all(outputs[task['id']] is not None for task in selected) and not pending),
            owner_final_pending=pending,candidate_id=candidate)
    return values


def member_contributions_in(db,unit,member_id,*,base_oid=None,policy_hash=None):
    values=contribution_collection_in(db,unit,base_oid=base_oid,policy_hash=policy_hash)
    if member_id not in values: raise TransitionError('contribution member is not in this delivery')
    return values[member_id]


def task_manifest_in(db,unit,plan,task_id):
    from .transitions import manifest_hash
    if plan is None:
        raise TransitionError('task has no current owner plan')
    if not db.execute('SELECT 1 FROM delegation_plan_tasks WHERE plan_id=? AND task_id=?',(plan['id'],task_id)).fetchone():
        raise TransitionError('task is not in the current owner plan')
    task=task_in(db,task_id)
    parent=json.loads(plan['input_manifest_json'])
    if not plan_current_in(db,unit,plan,parent['base_oid'],parent['policy_hash'],whole_members=False):
        raise TransitionError('task plan is no longer current')
    for pin in task['members']:
        member=db.execute('SELECT requirements_revision_id,left_at FROM unit_issues WHERE id=?',(pin['member_id'],)).fetchone()
        if member is None or member['left_at'] is not None or member['requirements_revision_id']!=pin['requirements_revision_id']:
            raise TransitionError('task member requirements changed')
    outputs=[]
    for predecessor in task['dependencies']:
        output=current_task_output_in(db,unit,predecessor,base_oid=parent['base_oid'],policy_hash=parent['policy_hash'])
        if output is None:
            raise TransitionError('task prerequisite output is not current')
        outputs.append(task_output_ref(output))
    return {'manifest_version':2,'task_id':task_id,'plan_id':plan['id'],'membership_generation':unit['membership_generation'],
            'members':task['members'],'requirements_hash':manifest_hash(task['members']),'base_oid':parent['base_oid'],
            'policy_hash':parent['policy_hash'],'scope':task['scope'],'executor':task['executor'],'existing_candidates':parent['existing_candidates'],
            'prerequisite_outputs':sorted(outputs,key=lambda row:row['output_id'])}


def task_execution_gates_in(db,unit,job):
    """Every physical execution checks its own immutable scope and current inputs."""
    from .transitions import applicable_gates
    pins=[row[0] for row in db.execute('SELECT member_id FROM job_members WHERE job_id=?',(job['id'],))]
    task=db.execute('SELECT task_id,plan_id FROM job_tasks WHERE job_id=?',(job['id'],)).fetchone()
    owner=db.execute('SELECT stage FROM builder_assignments WHERE job_id=?',(job['id'],)).fetchone()
    if not task and not owner:
        found={}
        for member in pins:
            for gate in applicable_gates(db,unit,member,job['candidate_id'],job_id=job['id']):
                found[(gate['id'],gate['code'],gate['subject_key'])]=gate
        return list(found.values())
    gates=scoped_task_gates_in(db,unit,pins,job_id=job['id'],candidate_id=job['candidate_id'])
    if owner and owner['stage']=='planning':
        return [gate for gate in gates if gate['member_id'] is None]
    row=db.execute('SELECT input_manifest_json FROM attempts WHERE id=?',(job['attempt_id'],)).fetchone()
    manifest=json.loads(row[0])
    if manifest.get('manifest_version')==2:
        try:
            expected=(task_manifest_in(db,unit,current_plan_in(db,unit['id']),task['task_id']) if task else
                      builder_manifest_in(db,unit,'integration',manifest['base_oid'],manifest['policy_hash']))
            valid=manifest==expected
        except TransitionError:
            valid=False
        if not valid:
            gates.append({'id':None,'unit_id':unit['id'],'member_id':None,'candidate_id':None,'job_id':job['id'],
                          'subject_key':'job:'+job['id'],'code':'dependency','details_json':json.dumps({'reason':'task_inputs_changed'})})
    return gates


def scoped_task_gates_in(db,unit,member_ids,*,job_id=None,candidate_id=None):
    from .transitions import _collect_gates
    gates=[]
    for gate in _collect_gates(db,unit):
        if gate.get('job_id') is not None:
            if gate['job_id']==job_id:
                gates.append(gate)
            continue
        if gate['candidate_id'] is not None and gate['candidate_id']!=candidate_id:
            continue
        if gate['member_id'] is not None and gate['member_id'] not in member_ids:
            continue
        details=json.loads(gate['details_json'])
        if gate['id'] is None and gate['code']=='dependency' and details.get('reason')=='prerequisite_output_pending':
            continue # Exact effective task DAG is checked separately inside reservation.
        gates.append(gate)
    return gates


def affected_tasks_in(db,unit_id,member_ids):
    tasks={row[0] for member in member_ids for row in db.execute('SELECT m.task_id FROM delivery_task_members m '
          'JOIN delivery_tasks t ON t.id=m.task_id JOIN unit_builders b ON b.id=t.builder_id WHERE b.unit_id=? AND m.member_id=?',(unit_id,member))}
    while True:
        following={row[0] for task in tasks for row in db.execute('SELECT task_id FROM delivery_task_dependencies WHERE prerequisite_task_id=?',(task,))}
        if following<=tasks:
            return tasks
        tasks.update(following)


def builder_manifest_in(db,unit,stage,base_oid,policy_hash):
    from .delivery_inputs import member_pins_in,requirements_hash_in,dependencies_in,existing_candidates_in,previous_publication_in
    if stage not in {'planning','integration'} or not all(isinstance(x,str) and x for x in (base_oid,policy_hash)):
        raise TransitionError('builder input requires stage and exact base/policy identities')
    pins=member_pins_in(db,unit)
    manifest={'manifest_version':2,'membership_generation':unit['membership_generation'],'members':pins,
              'requirements_hash':requirements_hash_in(db,unit),'base_oid':base_oid,'policy_hash':policy_hash,
              'dependencies':{pin['member_id']:dependencies_in(db,unit,pin['member_id']) for pin in pins},
              'publication_target':json.loads(db.execute('SELECT target_json FROM unit_publication_targets WHERE unit_id=?',(unit['id'],)).fetchone()[0]),
              'existing_candidates':existing_candidates_in(db,unit['id']),'previous_publication':previous_publication_in(db,unit['id'])}
    plan=current_plan_in(db,unit['id'])
    if stage=='planning':
        available=[]
        if plan:
            for task in plan_tasks_in(db,plan['id']):
                if any(pin not in pins for pin in task['members']):
                    continue
                output=current_task_output_in(db,unit,task['id'],base_oid=base_oid,policy_hash=policy_hash)
                available.append({key:task[key] for key in ('id','key','executor','scope','members','dependencies','internalizes')}|
                                 {'output':task_output_ref(output) if output else None})
        # Retention is explicit and closed: do not advertise a dangling old prerequisite.
        while True:
            allowed={task['id'] for task in available}
            following=[task for task in available if set(task['dependencies'])<=allowed]
            if len(following)==len(available): break
            available=following
        manifest['available_tasks']=available
        manifest['environment_conflicts']=environment_conflicts_in(db,unit)
        return manifest
    if not plan_current_in(db,unit,plan,base_oid,policy_hash):
        raise TransitionError('integration requires the current owner plan')
    tasks=plan_tasks_in(db,plan['id'])
    outputs=[current_task_output_in(db,unit,task['id'],base_oid=base_oid,policy_hash=policy_hash) for task in tasks]
    if any(output is None for output in outputs):
        raise TransitionError('integration requires every exact task output')
    return manifest|{'plan_id':plan['id'],'owner_final':owner_final_in(plan),'verification_requests':verification_requests_in(db,outputs),
                     'part_outputs':sorted((task_output_ref(output) for output in outputs),key=lambda row:row['output_id'])}


def environment_choices_in(db,unit,member_ids):
    choices={}
    for member in sorted(member_ids):
        row=db.execute('SELECT e.choice FROM unit_environment e JOIN unit_issues m ON m.id=e.member_id '
            'WHERE e.unit_id=? AND e.member_id=? AND e.requirements_revision_id=m.requirements_revision_id ORDER BY e.revision DESC LIMIT 1',(unit['id'],member)).fetchone()
        if row is None: raise TransitionError('task environment has not been observed for every scoped member')
        choices[member]=json.loads(row[0])
    return choices


def environment_conflicts_in(db,unit,*,plan_id=None):
    result=[]
    for row in db.execute("SELECT payload_json,result_json FROM decisions WHERE unit_id=? AND command='task_environment_conflict' AND status='applied' ORDER BY rowid",(unit['id'],)):
        value=json.loads(row['payload_json'])
        if plan_id is not None and value['plan_id']!=plan_id: continue
        try: current=environment_choices_in(db,unit,value['choices'])
        except TransitionError: continue
        if value['choices']==current:
            result.append(value|{'decision':json.loads(row['result_json'])})
    return result


def verification_requests_in(db,outputs):
    requests=[]
    for output in outputs:
        row=db.execute("SELECT a.content,a.codec,a.digest,COALESCE(a.raw_size,a.size) AS raw_size FROM job_artifacts a JOIN jobs j ON j.id=a.job_id WHERE j.attempt_id=? AND a.kind='result' AND a.sequence=0",(output['attempt_id'],)).fetchone()
        if row is None: raise TransitionError('accepted task output has no retained result evidence')
        import hashlib
        # Lifecycle._result writes immutable identity-coded structured evidence.
        if row['codec']!='identity' or len(row['content'])!=row['raw_size'] or hashlib.sha256(row['content']).hexdigest()!=row['digest']:
            raise TransitionError('accepted task result evidence is corrupt')
        evidence=json.loads(row['content'])
        report=evidence.get('report')
        if isinstance(report,dict) and report.get('outcome')=='needs_human_verification':
            verification=report['verification']
            if (not isinstance(verification,dict) or set(verification) not in ({'what','why'},{'what','why','checks'})
                    or not all(isinstance(verification[key],str) and verification[key].strip() for key in ('what','why'))
                    or not isinstance(verification.get('checks',[]),list)
                    or any(not isinstance(value,str) or not value.strip() for value in verification.get('checks',[]))):
                raise TransitionError('accepted task verification evidence is malformed')
            requests.append({'output_id':output['id'],'verification':{'checks':[],**verification}})
    return sorted(requests,key=lambda row:row['output_id'])
