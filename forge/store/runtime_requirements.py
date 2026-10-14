"""Revision-bound prerequisites and environment decisions for live row adapters."""
import json
import re

from .executions import Executions
from .effects import Effects, admit_send_in, defer_effect_in, effect_due, retry_policy
from .runtime import writable
from .models import TransitionError
from .transitions import _id, _json, applicable_gates, manifest_hash


def _memory(value):
    if not isinstance(value,str) or not re.fullmatch(r'[1-9][0-9]*[mg]',value):
        raise TransitionError('memory requires a positive m/g allocation')
    return int(value[:-1]) * 1024 ** (2 if value[-1]=='m' else 3)


def dependency_fulfilled(db, member_id, revision_id):
    row=db.execute('SELECT clear FROM dependency_checks WHERE member_id=? AND requirements_revision_id=? ORDER BY generation DESC LIMIT 1',
                   (member_id,revision_id)).fetchone()
    return bool(row and row[0])


def learn_memory_in(db,unit,job,now):
    launch=db.execute('SELECT runner_config_json FROM job_launches WHERE job_id=?',(job['id'],)).fetchone()
    if launch is None:
        return
    config=json.loads(launch[0])
    if not config.get('memory_tiers') or not config.get('memory'):
        return
    used=_memory(config['memory'])
    previous=db.execute('SELECT memory_bytes,revision FROM memory_profiles WHERE repository_id=? AND role=? ORDER BY revision DESC LIMIT 1',
                        (unit['repository_id'],job['role'])).fetchone()
    if not previous or used>previous['memory_bytes']:
        db.execute('INSERT INTO memory_profiles(id,repository_id,role,revision,memory_bytes,source_job_id,observed_at,allocation) VALUES(?,?,?,?,?,?,?,?)',
                   (_id(job['id'],'memory-profile'),unit['repository_id'],job['role'],previous['revision']+1 if previous else 1,
                    used,job['id'],now,config['memory']))
    for member in db.execute('SELECT m.issue_id FROM job_members j JOIN unit_issues m ON m.id=j.member_id WHERE j.job_id=?', (job['id'],)):
        row=db.execute('SELECT memory_retry_json FROM issue_environment_observations WHERE issue_id=?',(member['issue_id'],)).fetchone()
        retry=json.loads(row[0]) if row and row[0] else {}
        if retry.get('role')==job['role'] and used>=_memory(retry['memory']):
            db.execute('UPDATE issue_environment_observations SET memory_retry_json=NULL WHERE issue_id=?',(member['issue_id'],))


def _dependency_facts(facts):
    """Prerequisite authority excludes transport ordering and remote timestamps."""
    return {fact['reference']: {key: value for key, value in fact.items() if key != 'updated_at'}
            for fact in facts}


class RuntimeRequirements(Executions):
    @staticmethod
    def _revision_member(db, unit, revision_id):
        row = db.execute('SELECT * FROM unit_issues WHERE unit_id=? AND requirements_revision_id=? AND left_at IS NULL',
                         (unit['id'], revision_id)).fetchone()
        if row is None:
            raise TransitionError('Requirements revision is not a current member of this unit')
        return dict(row)

    def dependencies(self,command,*,requirements_revision_id,generation,facts):
        if (type(generation) is not int or generation<1 or not isinstance(facts,list)
                or len(_json(facts).encode())>60000):
            raise TransitionError('dependency observation must be a bounded ordered collection')
        from .repository_requirements import reference
        from ..factory.prerequisites import blocker
        refs=[]
        for fact in facts:
            if not isinstance(fact,dict):
                raise TransitionError('Dependency observation must identify a prerequisite')
            ref=reference(fact.get('reference'))
            if ref!=fact['reference']:
                raise TransitionError('Dependency references must be canonical')
            if fact.get('error'):
                if fact['error'] not in {'prerequisite_inaccessible','prerequisite_identity_changed','prerequisite_unobserved'}:
                    raise TransitionError('Unknown prerequisite observation error')
            elif fact.get('kind')=='internal':
                if (set(fact) != {'reference','kind','member_id','plan_id','candidate_id','output_ids','ready','owner_final_pending'}
                        or not isinstance(fact['member_id'],str)
                        or any(fact[key] is not None and not isinstance(fact[key],str) for key in ('plan_id','candidate_id'))
                        or not isinstance(fact['output_ids'],list)
                        or any(not isinstance(identifier,str) for identifier in fact['output_ids'])
                        or fact['output_ids'] != sorted(set(fact['output_ids']))
                        or type(fact['ready']) is not bool or type(fact['owner_final_pending']) is not bool):
                    raise TransitionError('Internal prerequisite must pin its complete current contribution set')
            else:
                if (fact.get('kind') not in {'issue','pull_request'} or fact.get('state') not in {'open','closed'}
                        or any(type(fact.get(key)) is not int or fact[key]<1 for key in ('repository_id','github_id','number'))
                        or type(fact.get('merged')) is not bool):
                    raise TransitionError('Dependency needs an exact GitHub identity and completion state')
                self._remote_time(fact['updated_at'])
            refs.append(ref)
        if len(set(refs))!=len(refs):
            raise TransitionError('Dependency observations must be unique')
        payload=dict(requirements_revision_id=requirements_revision_id,generation=generation,facts=facts)
        authority_changed = False
        def change(db,unit):
            nonlocal authority_changed
            self._live(unit)
            member=self._revision_member(db,unit,requirements_revision_id)
            if member['requirements_revision_id']!=requirements_revision_id:
                raise TransitionError('dependency evidence quotes an obsolete requirements revision')
            previous=db.execute('SELECT * FROM dependency_checks WHERE member_id=? ORDER BY generation DESC LIMIT 1',(member['id'],)).fetchone()
            if previous and generation<=previous['generation']:
                return {'stale':True}
            from .repository_requirements import orders_in
            required=set(orders_in(db,member['issue_id'],requirements_revision_id))
            required.update(row[0] for row in db.execute('SELECT lower(r.full_name)||\'#\'||i.github_number '
                'FROM issue_dependencies d JOIN issues i ON i.id=d.prerequisite_issue_id JOIN repositories r ON r.id=i.repository_id '
                'WHERE d.issue_id=? AND d.requirements_revision_id=?',(member['issue_id'],requirements_revision_id)))
            if not required<=set(refs):
                raise TransitionError('Dependency observation omits persisted prerequisites')
            own=db.execute('SELECT lower(r.full_name)||\'#\'||i.github_number FROM issues i JOIN repositories r ON r.id=i.repository_id WHERE i.id=?',(member['issue_id'],)).fetchone()[0]
            own_identity=db.execute('SELECT repository_id,github_number FROM issues WHERE id=?',(member['issue_id'],)).fetchone()
            blocked=[]
            for fact in facts:
                # Internal output readiness belongs to the approved delivery graph.
                reason=None if fact.get('kind')=='internal' else blocker(fact)
                if fact['reference']==own or (fact.get('repository_id'),fact.get('number'))==tuple(own_identity):
                    reason='prerequisite_cycle'
                if not fact.get('error') and fact['kind']=='internal':
                    from .delivery_inputs import dependencies_in,member_contributions_in
                    if fact['member_id'] not in dependencies_in(db,unit,member['id']):
                        raise TransitionError('Internal prerequisite was not approved in the composition')
                    contribution=member_contributions_in(db,unit,fact['member_id'])
                    expected={key:contribution[key] for key in ('plan_id','candidate_id','ready','owner_final_pending')}
                    expected['output_ids']=sorted(output['output_id'] for output in contribution['outputs'])
                    if any(fact[key]!=value for key,value in expected.items()):
                        raise TransitionError('Internal prerequisite contributions changed before observation')
                elif not fact.get('error'):
                    binding=db.execute('SELECT * FROM prerequisite_identities WHERE reference=?',(fact['reference'],)).fetchone()
                    identity=tuple(fact[key] for key in ('repository_id','github_id','number','kind'))
                    if binding and tuple(binding[key] for key in ('repository_id','github_id','number','kind'))!=identity:
                        reason='prerequisite_identity_changed'
                    elif binding is None:
                        db.execute('INSERT INTO prerequisite_identities VALUES(?,?,?,?,?,?)',(fact['reference'],*identity,self.clock()))
                if reason:
                    blocked.append({'reference':fact['reference'],'reason':reason})
            blocked.sort(key=lambda fact: fact['reference'])
            clear=not blocked
            authority_changed = (previous is None or previous['requirements_revision_id'] != requirements_revision_id
                or bool(previous['clear']) != clear
                or _dependency_facts(json.loads(previous['facts_json'])) != _dependency_facts(facts))
            db.execute('INSERT INTO dependency_checks VALUES(?,?,?,?,?,?,?,?)',
                       (_id(command.request_id,'dependency-check'),unit['id'],member['id'],requirements_revision_id,generation,int(clear),_json(facts),self.clock()))
            gates=db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='dependency' AND member_id IS ? AND resolved_at IS NULL",
                             (unit['id'],member['id'])).fetchall()
            owned=[g for g in gates if json.loads(g['details_json']).get('source')=='runtime_dependencies']
            current_digest=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(requirements_revision_id,)).fetchone()[0])
            for gate in gates:
                details=json.loads(gate['details_json'])
                if details.get('requirements') is not None and details['requirements']!=current_digest:
                    authority_changed = True
                    self._resolve(db,command,gate['id'],{'superseded_by_requirements':requirements_revision_id})
            worker_wait=any(json.loads(gate['details_json']).get('requirements')==current_digest
                            and json.loads(gate['details_json']).get('issues') for gate in gates)
            if clear:
                for gate in owned:
                    authority_changed = True
                    self._resolve(db,command,gate['id'],{'verified':True,'requirements_revision_id':requirements_revision_id,'generation':generation})
                # Worker-declared waits bind to the same requirements digest and
                # are cleared only if every named prerequisite was observed closed.
                digest=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(requirements_revision_id,)).fetchone()[0])
                for gate in gates:
                    details=json.loads(gate['details_json'])
                    if details.get('requirements')==digest and {own.rsplit('#',1)[0]+'#'+str(number) for number in details.get('issues',[])}<=set(refs):
                        authority_changed = True
                        self._resolve(db,command,gate['id'],{'verified':True,'requirements_revision_id':requirements_revision_id,'generation':generation})
            elif (not owned or any(json.loads(g['details_json']).get('blocked')!=blocked for g in owned)) and (authority_changed or not worker_wait):
                authority_changed = True
                for gate in owned:
                    self._resolve(db,command,gate['id'],{'superseded_by_observation':generation})
                self.fence_member_in(db,command,member['id'],reason='prerequisite_changed')
                if not worker_wait:
                    self._open_gate(db,command,'dependency',{'source':'runtime_dependencies','requirements_revision_id':requirements_revision_id,
                        'blocked':blocked},member['id'])
            # The check row above renews freshness; an unchanged result is not journalled.
            if authority_changed:
                self._event(db,command,'lifecycle_changed',{'dependency_generation':generation,'clear':clear})
            return {'clear':clear,'generation':generation}
        return self._apply(command,'observe_dependencies',payload,change,version_changed=lambda: authority_changed,
                           journal=lambda: authority_changed)

    def select_environment(self,command,*,requirements_revision_id,choice,labels,config_hash):
        if (not isinstance(choice,dict) or choice.get('tier') not in {'standard','full'} or type(choice.get('automatic')) is not bool
                or not isinstance(choice.get('services'),list) or set(choice['services'])-{'mongodb','postgres','redis'}
                or choice['tier']=='standard' and choice['services'] or len(_json(choice).encode())>32000
                or not isinstance(labels,list) or any(label not in {'env:standard','env:full'} for label in labels)
                or not isinstance(config_hash,str) or not config_hash):
            raise TransitionError('environment selection needs a bounded supported choice')
        payload=dict(requirements_revision_id=requirements_revision_id,choice=choice,labels=labels,config_hash=config_hash)
        def change(db,unit):
            self._live(unit)
            member=self._revision_member(db,unit,requirements_revision_id)
            if member['requirements_revision_id']!=requirements_revision_id:
                raise TransitionError('environment selection quotes obsolete requirements')
            current=db.execute('SELECT revision FROM unit_environment WHERE unit_id=? ORDER BY revision DESC LIMIT 1',(unit['id'],)).fetchone()
            revision=current[0]+1 if current else 1
            target='env:'+choice['tier']
            for gate in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND member_id=? AND code='environment' AND resolved_at IS NULL",(unit['id'],member['id'])).fetchall():
                if json.loads(gate['details_json']).get('source')=='runtime_selection':
                    self._resolve(db,command,gate['id'],{'environment_ready':choice,'requirements_revision_id':requirements_revision_id})
            db.execute('INSERT INTO unit_environment(unit_id,revision,choice,requirements_revision_id,config_hash,created_at,member_id) VALUES(?,?,?,?,?,?,?)',
                       (unit['id'],revision,_json(choice),requirements_revision_id,config_hash,self.clock(),member['id']))
            db.execute('INSERT INTO issue_environment_observations(issue_id,choice_json,label) VALUES(?,?,?) ON CONFLICT(issue_id) DO UPDATE SET choice_json=excluded.choice_json,label=excluded.label',
                       (member['issue_id'],_json(choice),target))
            effect_id=None
            if set(labels)!={target}:
                effect_id=_id(command.request_id,'environment-label')
                db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,expected_version,payload_json,created_at,updated_at) VALUES(?,?,?,'environment_label','pending',?,?,?,?)",
                    (effect_id,command.request_id,unit['id'],unit['version']+1,_json(payload),self.clock(),self.clock()))
                db.execute('INSERT INTO environment_label_effects VALUES(?,?,?,?,?,?,?)',
                    (effect_id,unit['id'],revision,member['id'],requirements_revision_id,target,_json(sorted(set(labels)))))
            self._event(db,command,'lifecycle_changed',{'environment_revision':revision,'choice':choice,'effect_id':effect_id})
            return {'revision':revision,'effect_id':effect_id}
        return self._apply(command,'select_environment',payload,change)

    def environment_receipt(self,effect_id,*,status,observed_labels):
        if status not in {'confirmed','superseded'} or not isinstance(observed_labels,list):
            raise TransitionError('environment label receipt requires observed labels')
        with self.store.write() as db:
            writable(db)
            row=db.execute('SELECT e.*,l.target_label FROM external_effects e JOIN environment_label_effects l ON l.effect_id=e.id WHERE e.id=?',(effect_id,)).fetchone()
            if row is None:
                raise KeyError(effect_id)
            if status=='confirmed' and set(observed_labels)!={row['target_label']}:
                raise TransitionError('label projection has not been observed complete')
            if row['status'] in {'confirmed','superseded'}:
                return
            db.execute('UPDATE external_effects SET status=?,receipt_json=?,updated_at=? WHERE id=?',
                       (status,_json({'labels':observed_labels}),self.clock(),effect_id))
            Effects(self.store, clock=self.clock)._effect_event(db,effect_id)

    def claim_environment(self,effect_id,*,expected_version,reliability=None):
        with self.store.write() as db:
            writable(db)
            row=db.execute('SELECT e.*,l.revision,l.requirements_revision_id,l.member_id FROM external_effects e JOIN environment_label_effects l ON l.effect_id=e.id WHERE e.id=?',(effect_id,)).fetchone()
            if row is None or row['status'] not in {'pending','running','uncertain'}:
                raise TransitionError('environment effect is not runnable')
            reliability=self._effect_policy_in(db,reliability)
            admit_send_in(row,self.clock(),reliability=reliability)
            unit=db.execute('SELECT * FROM units WHERE id=?',(row['unit_id'],)).fetchone()
            member=db.execute('SELECT * FROM unit_issues WHERE id=?',(row['member_id'],)).fetchone()
            latest=db.execute('SELECT MAX(revision) FROM unit_environment WHERE member_id=?',(member['id'],)).fetchone()[0]
            if (unit['version']!=expected_version or unit['state'] in {'merged','closed','dissolved'}
                    or member['left_at'] is not None or member['requirements_revision_id']!=row['requirements_revision_id']
                    or latest!=row['revision']):
                raise TransitionError('environment effect authority has changed')
            policy=Effects(self.store,clock=self.clock,**retry_policy(reliability))
            db.execute("UPDATE external_effects SET status='running',attempts=attempts+1,retry_at=?,uncertain_since=COALESCE(uncertain_since,?),updated_at=? WHERE id=?",
                (self.clock()+policy.delay(row['attempts']+1),self.clock(),self.clock(),effect_id))
            Effects(self.store,clock=self.clock)._effect_event(db,effect_id)

    @staticmethod
    def _effect_policy_in(db,fallback):
        row=db.execute("SELECT settings_json FROM configuration WHERE domain='runtime.json' AND subject='instance' AND active=1").fetchone()
        return json.loads(row[0]).get('reliability',{}) if row else fallback

    def environment_unavailable(self,effect_id,*,reliability=None):
        with self.store.write() as db:
            writable(db)
            row=db.execute("SELECT * FROM external_effects WHERE id=? AND kind='environment_label'",(effect_id,)).fetchone()
            if row is None or row['status'] not in {'pending','running','uncertain'} or (row['status']!='running' and not effect_due(row,self.clock())):
                return
            defer_effect_in(db,row,self.clock(),reliability=self._effect_policy_in(db,reliability))
            Effects(self.store,clock=self.clock)._effect_event(db,effect_id)

    def environment_problem(self,command,*,requirements_revision_id,reason):
        if not isinstance(reason,str) or not reason or len(reason)>2000:
            raise TransitionError('environment issue needs a bounded reason')
        def change(db,unit):
            self._live(unit)
            member=self._revision_member(db,unit,requirements_revision_id)
            if member['requirements_revision_id']!=requirements_revision_id:
                raise TransitionError('environment issue quotes obsolete requirements')
            self.fence_member_in(db,command,member['id'],reason='environment_changed')
            gate=self._open_gate(db,command,'environment',{'source':'runtime_selection','reason':reason,'requirements_revision_id':requirements_revision_id},member['id'])
            return {'accepted':True,'gate_id':gate}
        return self._apply(command,'environment_problem',dict(requirements_revision_id=requirements_revision_id,reason=reason),change)

    def _set_override_in(self,db,command,unit,requirements_revision_id,value):
        from ..factory.environments import override as validate_override
        value=validate_override(value)
        self._live(unit)
        member=self._revision_member(db,unit,requirements_revision_id)
        if member['requirements_revision_id']!=requirements_revision_id:
            raise TransitionError('environment override quotes obsolete requirements')
        self.fence_member_in(db,command,member['id'],reason='environment_changed')
        db.execute('INSERT INTO issue_environment_observations(issue_id,override_json) VALUES(?,?) ON CONFLICT(issue_id) DO UPDATE SET override_json=excluded.override_json,retry_json=NULL',
                   (member['issue_id'],_json(value) if value['mode']!='automatic' else None))
        from .worker_claims import WorkerClaims, eligible_gate_in
        for gate in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND member_id=? AND code='environment' AND resolved_at IS NULL",(unit['id'],member['id'])).fetchall():
            if eligible_gate_in(db,gate):
                WorkerClaims(self.store,clock=self.clock).recheck_in(db,command,unit,[gate['id']],
                    member_id=member['id'],explicit=True,reason='environment_override')
            else:
                self._resolve(db,command,gate['id'],{'operator_environment_override':value})
        self._event(db,command,'lifecycle_changed',{'environment_override':value})
        return {'accepted':True}

    def set_override(self,command,*,requirements_revision_id,override):
        from ..factory.environments import override as validate_override
        value=validate_override(override)
        return self._apply(command,'environment_override',dict(requirements_revision_id=requirements_revision_id,override=value),
            lambda db,unit:self._set_override_in(db,command,unit,requirements_revision_id,value))

    def _clear_declared_dependencies_in(self,db,command,unit,requirements_revision_id,evidence):
        if not isinstance(evidence,dict) or not evidence:
            raise TransitionError('dependency override requires operator evidence')
        self._live(unit)
        member=self._revision_member(db,unit,requirements_revision_id)
        if member['requirements_revision_id']!=requirements_revision_id:
            raise TransitionError('dependency override quotes obsolete requirements')
        digest=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(requirements_revision_id,)).fetchone()[0])
        resolved=[]
        for gate in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='dependency' AND resolved_at IS NULL",(unit['id'],)).fetchall():
            details=json.loads(gate['details_json'])
            if details.get('requirements')==digest and details.get('issues') and details.get('source')!='runtime_dependencies':
                self._resolve(db,command,gate['id'],evidence)
                resolved.append(gate['id'])
        self._event(db,command,'lifecycle_changed',{'declared_dependencies_cleared':resolved})
        return {'accepted':True,'resolved':resolved}

    def clear_declared_dependencies(self,command,*,requirements_revision_id,evidence):
        return self._apply(command,'clear_declared_dependencies',dict(requirements_revision_id=requirements_revision_id,evidence=evidence),
            lambda db,unit:self._clear_declared_dependencies_in(db,command,unit,requirements_revision_id,evidence))

    def environment_retry(self,command,job_id,*,execution_fence,input_manifest,kind,selection,evidence):
        if kind not in {'memory','services'} or not isinstance(selection,dict) or not isinstance(evidence,dict):
            raise TransitionError('environment retry requires verified selection and evidence')
        payload=dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,kind=kind,selection=selection,evidence=evidence)
        def change(db,unit):
            job,current=self._job(db,unit,job_id,execution_fence,input_manifest)
            if not current:
                self._result(db,command,job,evidence,'stale_environment_retry')
                return {'accepted':False}
            if job['role'] not in {'build','integration','review','audit'} or not job['cleanup']:
                raise TransitionError('environment retry requires a cleaned pipeline assignment')
            from .task_inputs import task_execution_gates_in
            for gate in task_execution_gates_in(db,unit,job):
                if gate['code'] not in {'result_pending','cleanup_pending'} or json.loads(gate['details_json']).get('job_id')!=job_id:
                    raise TransitionError('another gate prevents environment retry')
            launch=db.execute('SELECT runner_config_json,branch FROM job_launches WHERE job_id=?',(job_id,)).fetchone()
            config=json.loads(launch['runner_config_json'])
            members=db.execute('SELECT m.issue_id FROM job_members j JOIN unit_issues m ON m.id=j.member_id WHERE j.job_id=?',(job_id,)).fetchall()
            states=[db.execute('SELECT * FROM issue_environment_observations WHERE issue_id=?',(m['issue_id'],)).fetchone() for m in members]
            if kind=='memory':
                used=_memory(config['memory'])
                tiers=selection.get('tiers',[])
                if (not isinstance(tiers,list) or not tiers or len(tiers)>8
                        or [_memory(t) for t in tiers]!=sorted(set(_memory(t) for t in tiers))):
                    raise TransitionError('memory escalation requires ordered bounded tiers')
                wanted=next((tier for tier in tiers if _memory(tier)>used),None)
                if wanted is None or selection.get('memory')!=wanted or evidence.get('confirmed_memory_exhaustion') is not True:
                    raise TransitionError('retry must select the next bounded memory tier')
                retry=dict(role=job['role'],memory=wanted,attempt=job_id,branch=launch['branch'],at=self.clock())
                field='memory_retry_json'
            else:
                wanted=selection.get('services')
                if (any(state and (state['retry_json'] or state['override_json'] or json.loads(state['choice_json'] or '{}').get('automatic') is False) for state in states)
                        or not isinstance(wanted,list) or not wanted or set(wanted)-{'mongodb','postgres','redis'}
                        or set(wanted)<=set(config.get('services',[])) or evidence.get('full_environment_available') is not True):
                    raise TransitionError('service retry must be automatic, available, new and bounded to one retry')
                retry=dict(attempt=job_id,services=sorted(set(wanted)|set(config.get('services',[]))),branch=launch['branch'])
                field='retry_json'
            self._result(db,command,job,evidence,kind+'_retry')
            for member in members:
                db.execute('INSERT INTO issue_environment_observations(issue_id,'+field+') VALUES(?,?) ON CONFLICT(issue_id) DO UPDATE SET '+field+'=excluded.'+field,
                           (member['issue_id'],_json(retry)))
            db.execute('INSERT INTO environment_job_retries VALUES(?,?,?,?)',(job_id,kind,_json(selection),self.clock()))
            self._event(db,command,'lifecycle_changed',{'environment_retry':kind,'job_id':job_id,'selection':selection})
            return {'accepted':True,'retry':kind,'selection':selection}
        return self._apply(command,'retry_environment',payload,change)
