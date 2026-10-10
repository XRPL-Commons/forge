"""Task-plan commands mixed into the existing delivery lifecycle authority."""
import json
import re

from .models import InvalidDelegationPlan, TransitionError
from .transitions import _id,_json,_charges,manifest_hash
from .task_inputs import (task_in,task_members_in,task_dependencies_in,current_task_output_in,current_plan_in,
                         plan_tasks_in,plan_current_in,task_manifest_in,scoped_task_gates_in,task_output_ref)


class TaskCommands:
    def save_task_plan(self,command,job_id,*,execution_fence,input_manifest,plan,evidence):
        payload=dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,plan=plan,evidence=evidence)
        def change(db,unit):
            job,current=self._job(db,unit,job_id,execution_fence,input_manifest)
            owner=db.execute("SELECT * FROM builder_assignments WHERE job_id=? AND stage='planning'",(job_id,)).fetchone()
            if owner is None:
                raise TransitionError('only the owning planning execution can choose task topology')
            if not current:
                self._result(db,command,job,evidence,'stale_plan'); return {'accepted':False}
            if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND id!=? AND (cleanup=0 OR outcome IS NULL)',(unit['id'],job_id)).fetchone():
                raise TransitionError('replanning requires quiescent previous executions')
            if (not isinstance(plan,dict) or set(plan)!={'version','retained_task_ids','tasks','owner_final'} or plan['version']!=2
                    or len(_json(plan).encode())>65536 or not isinstance(plan['tasks'],list) or not isinstance(plan['retained_task_ids'],list)):
                raise InvalidDelegationPlan('task plan must use the bounded version 2 contract')
            plan_id=_id(command.request_id,'plan')
            pins={row['member_id']:row['requirements_revision_id'] for row in input_manifest['members']}
            available={task['id']:task for task in input_manifest['available_tasks']}
            retained=plan['retained_task_ids']
            if any(not isinstance(value,str) or value not in available for value in retained) or len(set(retained))!=len(retained):
                raise InvalidDelegationPlan('retained tasks must be exact owner assignment inputs')
            definitions={identity:task_in(db,identity) for identity in retained}
            keys={}
            for entry in plan['tasks']:
                if (not isinstance(entry,dict) or set(entry)!={'key','executor','member_ids','scope','depends_on','internalizes'}
                        or not isinstance(entry['key'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',entry['key']) or entry['key'] in keys
                        or entry['executor'] not in {'owner','child'} or not isinstance(entry['scope'],str) or not entry['scope'].strip()
                        or not isinstance(entry['member_ids'],list) or not entry['member_ids'] or any(not isinstance(x,str) or x not in pins for x in entry['member_ids'])
                        or len(set(entry['member_ids']))!=len(entry['member_ids']) or not isinstance(entry['depends_on'],list)
                        or any(not isinstance(value,str) for value in entry['depends_on'])):
                    raise InvalidDelegationPlan('task scope or identity is invalid')
                identity=_id(plan_id,'task:'+entry['key']); keys[entry['key']]=identity
                definitions[identity]={'id':identity,'key':entry['key'],'executor':entry['executor'],'scope':entry['scope'],
                    'members':[{'member_id':member,'requirements_revision_id':pins[member]} for member in sorted(entry['member_ids'])],
                    'dependencies':entry['depends_on'],'internalizes':entry['internalizes']}
            final=plan['owner_final']
            if (not isinstance(final,dict) or set(final)!={'member_ids','scope','internalizes'} or not isinstance(final['member_ids'],list)
                    or any(not isinstance(x,str) or x not in pins for x in final['member_ids']) or len(set(final['member_ids']))!=len(final['member_ids'])
                    or not isinstance(final['scope'],str) or (final['member_ids'] and not final['scope'].strip())):
                raise InvalidDelegationPlan('owner-final scope is invalid')
            scopes={identity:{pin['member_id'] for pin in task['members']} for identity,task in definitions.items()}
            scopes['@integration']=set(final['member_ids'])
            if set().union(*scopes.values())!=set(pins):
                raise InvalidDelegationPlan('task and owner-final contributions must cover approved membership')
            approved={(p,m) for m,parents in input_manifest['dependencies'].items() for p in parents}
            declarations={}
            for identity,scope in scopes.items():
                entries=final['internalizes'] if identity=='@integration' else definitions[identity]['internalizes']
                if not isinstance(entries,list):
                    raise InvalidDelegationPlan('internalized dependencies must be explicit edges')
                edges=set()
                for edge in entries:
                    if (not isinstance(edge,dict) or set(edge)!={'prerequisite_member_id','member_id'}
                            or not isinstance(edge['prerequisite_member_id'],str) or not isinstance(edge['member_id'],str)):
                        raise InvalidDelegationPlan('internalized dependency shape is invalid')
                    pair=(edge['prerequisite_member_id'],edge['member_id'])
                    if pair not in approved or not set(pair)<=scope or pair in edges:
                        raise InvalidDelegationPlan('only scoped approved dependencies may be internalized')
                    edges.add(pair)
                declarations[identity]=edges
            dependencies={identity:set() for identity in scopes}
            dependencies['@integration']=set(definitions)
            for identity,task in definitions.items():
                for reference in task['dependencies']:
                    target=reference if identity in retained else keys.get(reference,reference)
                    if not isinstance(target,str) or target not in definitions:
                        raise InvalidDelegationPlan('task dependency must name a retained task or new task key')
                    dependencies[identity].add(target)
            for predecessor,member in approved:
                for target,scope in scopes.items():
                    if member not in scope: continue
                    for source,producer_scope in scopes.items():
                        if predecessor not in producer_scope: continue
                        if source==target:
                            if (predecessor,member) not in declarations[target]:
                                raise InvalidDelegationPlan('joint prerequisite work requires explicit internalization')
                        else:
                            dependencies[target].add(source)
            visited,visiting=set(),set()
            def visit(identity):
                if identity in visiting: raise InvalidDelegationPlan('task and member dependencies form a cycle')
                if identity in visited: return
                visiting.add(identity)
                for predecessor in dependencies[identity]: visit(predecessor)
                visiting.remove(identity); visited.add(identity)
            for identity in scopes: visit(identity)
            for identity in retained:
                if dependencies[identity]!=set(task_dependencies_in(db,identity)):
                    raise InvalidDelegationPlan('retained task would change immutable prerequisite authority')
                if any(pin['requirements_revision_id']!=pins[pin['member_id']] for pin in definitions[identity]['members']):
                    raise InvalidDelegationPlan('retained task requirements changed')
            db.execute('INSERT INTO delegation_plans VALUES(?,?,?,?,?,?)',(plan_id,owner['builder_id'],job_id,manifest_hash(input_manifest),_json(plan),self.clock()))
            for identity,task in definitions.items():
                if identity in retained: continue
                db.execute('INSERT INTO delivery_tasks VALUES(?,?,?,?,?,?,?,?)',(identity,owner['builder_id'],plan_id,task['key'],task['executor'],task['scope'],_json(task['internalizes']),self.clock()))
                db.executemany('INSERT INTO delivery_task_members VALUES(?,?,?)',[(identity,pin['member_id'],pin['requirements_revision_id']) for pin in task['members']])
            for identity in definitions:
                db.execute('INSERT INTO delegation_plan_tasks VALUES(?,?)',(plan_id,identity))
                if identity not in retained:
                    db.executemany('INSERT INTO delivery_task_dependencies VALUES(?,?)',[(identity,predecessor) for predecessor in sorted(dependencies[identity])])
            from .task_inputs import environment_conflicts_in
            conflicts=environment_conflicts_in(db,unit)
            repeated=next((conflict for conflict in conflicts if any(set(conflict['choices'])==scope for identity,scope in scopes.items() if identity!='@integration')),None)
            if repeated and not db.execute("SELECT 1 FROM unit_gates WHERE unit_id=? AND code='decision_required' AND subject_key='unit' AND resolved_at IS NULL",(unit['id'],)).fetchone():
                self._open_gate(db,command,'decision_required',{'source':'task_environment_conflict','reason':'incompatible_task_environments',
                    'plan_id':plan_id,'task_id':None,'choices':repeated['choices'],'member_ids':sorted(repeated['choices'])})
            self._result(db,command,job,{'plan_id':plan_id,'plan':plan,'evidence':evidence},'planned')
            return {'accepted':True,'plan_id':plan_id,'builder_id':owner['builder_id'],'task_ids':keys}
        return self._apply(command,'save_plan',payload,change)

    def reserve_task(self,command,plan_id,task_id,*,slot,execution_fence,input_manifest,worktree_key,launch=None):
        payload=dict(plan_id=plan_id,task_id=task_id,slot=slot,execution_fence=execution_fence,input_manifest=input_manifest,
                     worktree_key=worktree_key,launch=launch.payload() if launch else None)
        def change(db,unit):
            self._live(unit)
            if unit['state']!='building': raise TransitionError('tasks require the delivery build phase')
            plan=current_plan_in(db,unit['id'])
            if not plan or plan['id']!=plan_id: raise TransitionError('task plan has been superseded')
            expected=task_manifest_in(db,unit,plan,task_id)
            if expected!=input_manifest or not isinstance(worktree_key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',worktree_key):
                raise TransitionError('task assignment differs from its exact plan inputs')
            task=task_in(db,task_id)
            if scoped_task_gates_in(db,unit,[pin['member_id'] for pin in task['members']]):
                raise TransitionError('a scoped authority gate blocks this task')
            if current_task_output_in(db,unit,task_id,base_oid=expected['base_oid'],policy_hash=expected['policy_hash']):
                raise TransitionError('task already has a current accepted output')
            if db.execute('SELECT 1 FROM jobs j LEFT JOIN job_tasks t ON t.job_id=j.id WHERE j.unit_id=? AND (j.cleanup=0 OR j.outcome IS NULL) '
                          'AND (t.task_id=? OR t.task_id IS NULL)',(unit['id'],task_id)).fetchone():
                raise TransitionError('task or whole owner execution still requires completion and cleanup')
            if db.execute('SELECT 1 FROM job_tasks WHERE plan_id=? AND task_id=?',(plan_id,task_id)).fetchone():
                raise TransitionError('a failed task requires a new quiescent owner plan before retry')
            if task['executor']=='owner' and db.execute("SELECT 1 FROM jobs j JOIN job_tasks x ON x.job_id=j.id JOIN delivery_tasks t ON t.id=x.task_id "
                "WHERE j.unit_id=? AND t.executor='owner' AND (j.cleanup=0 OR j.outcome IS NULL)",(unit['id'],)).fetchone():
                raise TransitionError('another owning builder execution is active')
            result=self._reserve_in(db,command,unit,slot=slot,execution_fence=execution_fence,input_manifest=input_manifest,execution_kind='task')
            db.execute('INSERT INTO job_tasks VALUES(?,?,?,?)',(result['job_id'],plan_id,task_id,worktree_key))
            if launch is not None:
                from .executions import insert_launch
                insert_launch(db,result['job_id'],slot,execution_fence,launch.payload(),self.clock())
            db.executemany("UPDATE unit_issues SET state='building' WHERE id=?",[(pin['member_id'],) for pin in task['members']])
            return result|{'builder_id':task['builder_id'],'parent_job_id':plan['parent_job_id'],'task_id':task_id,'member_ids':[pin['member_id'] for pin in task['members']]}
        return self._apply(command,'reserve_task',payload,change)

    def accept_task_output(self,command,job_id,*,execution_fence,input_manifest,commit_oid,evidence):
        payload=dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,commit_oid=commit_oid,evidence=evidence)
        def change(db,unit):
            job,current=self._job(db,unit,job_id,execution_fence,input_manifest)
            assignment=db.execute('SELECT * FROM job_tasks WHERE job_id=?',(job_id,)).fetchone()
            if assignment is None: raise TransitionError('task output requires a tracked task execution')
            if not current:
                self._result(db,command,job,evidence,'stale_success'); return {'accepted':False}
            plan=current_plan_in(db,unit['id'])
            if not plan or plan['id']!=assignment['plan_id'] or input_manifest!=task_manifest_in(db,unit,plan,assignment['task_id']):
                raise TransitionError('task inputs changed before acceptance')
            captured=db.execute('SELECT capture_authorized,result_revoked FROM job_execution_state WHERE job_id=? AND captured_at IS NOT NULL',(job_id,)).fetchone()
            if not job['cleanup'] or not captured or not captured['capture_authorized'] or captured['result_revoked']:
                raise TransitionError('task acceptance requires verified cleanup and an authorized captured result')
            self._verify_part_evidence(db,job,input_manifest,commit_oid,evidence)
            members=[pin['member_id'] for pin in task_members_in(db,assignment['task_id'])]
            gates=scoped_task_gates_in(db,unit,members,job_id=job_id)
            if any(g['code'] not in {'cleanup_pending','result_pending'} or g.get('job_id')!=job_id for g in gates):
                raise TransitionError('a scoped authority gate blocks task acceptance')
            identity=_id(command.request_id,'output')
            db.execute('INSERT INTO part_outputs(id,unit_id,attempt_id,membership_generation,commit_oid,base_oid,input_manifest_hash,accepted_at,task_id) VALUES(?,?,?,?,?,?,?,?,?)',
                (identity,unit['id'],job['attempt_id'],unit['membership_generation'],commit_oid,input_manifest['base_oid'],manifest_hash(input_manifest),self.clock(),assignment['task_id']))
            self._result(db,command,job,evidence,'success')
            from .task_inputs import member_contributions_in
            for member in members:
                if member_contributions_in(db,unit,member,base_oid=input_manifest['base_oid'],policy_hash=input_manifest['policy_hash'])['ready']:
                    db.execute("UPDATE unit_issues SET state='built' WHERE id=?",(member,))
            self._event(db,command,'part_accepted',{'output_id':identity,'task_id':assignment['task_id'],'member_ids':members,'commit_oid':commit_oid},job_id=job_id)
            return {'accepted':True,'output_id':identity,'task_id':assignment['task_id']}
        return self._apply(command,'accept_task_output',payload,change)

    def _verify_part_evidence(self,db,job,input_manifest,commit_oid,evidence):
        if (not isinstance(commit_oid,str) or not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}',commit_oid)
                or not isinstance(evidence,dict) or evidence.get('verified') is not True
                or evidence.get('commit_oid')!=commit_oid or evidence.get('base_oid')!=input_manifest['base_oid']
                or evidence.get('input_manifest_hash')!=manifest_hash(input_manifest)):
            raise TransitionError('task output needs independently verified commit and input evidence')
        from .artifacts import Artifacts
        capture=json.loads(Artifacts(self.store).read(job['id'],'worker_capture').content)
        bundle=capture.get('bundle')
        chunks=db.execute("SELECT sequence,COALESCE(raw_size,size) AS raw_size FROM job_artifacts WHERE job_id=? AND kind='worktree_bundle' AND expired_at IS NULL ORDER BY sequence",(job['id'],)).fetchall()
        if (not isinstance(bundle,dict) or evidence.get('bundle_digest')!=bundle.get('digest') or not chunks
                or len(chunks)!=bundle.get('chunks') or [row['sequence'] for row in chunks]!=list(range(len(chunks)))
                or sum(row['raw_size'] for row in chunks)!=bundle.get('size')):
            raise TransitionError('accepted task output must retain its exact captured bundle')

    def environment_conflict(self,command,*,plan_id,task_id,choices):
        payload=dict(plan_id=plan_id,task_id=task_id,choices=choices)
        def change(db,unit):
            from .task_inputs import environment_choices_in,environment_conflicts_in
            plan=current_plan_in(db,unit['id'])
            if not plan or plan['id']!=plan_id or not plan_current_in(db,unit,plan):
                raise TransitionError('environment conflict must identify the current task plan')
            if task_id is None:
                scope={row[0] for row in db.execute('SELECT id FROM unit_issues WHERE unit_id=? AND left_at IS NULL',(unit['id'],))}
            else:
                if not db.execute('SELECT 1 FROM delegation_plan_tasks WHERE plan_id=? AND task_id=?',(plan_id,task_id)).fetchone():
                    raise TransitionError('environment conflict task is outside this plan')
                scope={pin['member_id'] for pin in task_members_in(db,task_id)}
            if not isinstance(choices,dict) or set(choices)!=scope or choices!=environment_choices_in(db,unit,scope):
                raise TransitionError('environment conflict must pin every exact current scoped choice')
            if not (any(choice['tier']=='full' for choice in choices.values()) and any(choice['tier']=='standard' and choice['automatic'] is False for choice in choices.values())):
                raise TransitionError('combined environment choices do not conflict')
            previous=environment_conflicts_in(db,unit)
            repeated=any(row['choices']==choices for row in previous)
            gate_id=None
            if task_id is None or repeated:
                existing=db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='decision_required' AND subject_key='unit' AND resolved_at IS NULL",(unit['id'],)).fetchone()
                if existing:
                    gate_id=existing[0]
                else:
                    gate_id=self._open_gate(db,command,'decision_required',{'source':'task_environment_conflict','reason':'incompatible_task_environments',
                        'plan_id':plan_id,'task_id':task_id,'choices':choices,'member_ids':sorted(scope)})
            self._event(db,command,'lifecycle_changed',{'reason':'task_environment_conflict','plan_id':plan_id,'task_id':task_id,'member_ids':sorted(scope)})
            return {'recorded':True,'pending_reason':'environment_conflict','replan_allowed':gate_id is None,'gate_id':gate_id}
        return self._apply(command,'task_environment_conflict',payload,change)
