"""Revision-bound prerequisites and environment decisions for live row adapters."""
import json
import re

from .executions import Executions
from .effects import Effects
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
    row=db.execute('SELECT memory_retry_json FROM unit_environment_observations WHERE unit_id=?',(unit['id'],)).fetchone()
    retry=json.loads(row[0]) if row and row[0] else {}
    if retry.get('role')==job['role'] and used>=_memory(retry['memory']):
        db.execute('UPDATE unit_environment_observations SET memory_retry_json=NULL WHERE unit_id=?',(unit['id'],))


class RuntimeRequirements(Executions):
    def dependencies(self,command,*,requirements_revision_id,generation,facts):
        if (type(generation) is not int or generation<1 or not isinstance(facts,list) or len(facts)>100
                or len(_json(facts).encode())>60000):
            raise TransitionError('dependency observation must be a bounded ordered collection')
        numbers=[]
        for fact in facts:
            if (not isinstance(fact,dict) or type(fact.get('number')) is not int or fact['number']<1
                    or fact.get('state') not in {'open','closed'} or not isinstance(fact.get('updated_at'),str)):
                raise TransitionError('dependency needs an exact observed issue identity')
            self._remote_time(fact['updated_at'])
            numbers.append(fact['number'])
        if len(set(numbers))!=len(numbers):
            raise TransitionError('dependency observations must be unique')
        payload=dict(requirements_revision_id=requirements_revision_id,generation=generation,facts=facts)
        def change(db,unit):
            self._live(unit)
            member=self._singleton(db,unit)
            if member['requirements_revision_id']!=requirements_revision_id:
                raise TransitionError('dependency evidence quotes an obsolete requirements revision')
            previous=db.execute('SELECT generation FROM dependency_checks WHERE member_id=? ORDER BY generation DESC LIMIT 1',(member['id'],)).fetchone()
            if previous and generation<=previous[0]:
                return {'stale':True}
            required={row[0] for row in db.execute('SELECT i.github_number FROM issue_dependencies d JOIN issues i ON i.id=d.prerequisite_issue_id '
                'WHERE d.issue_id=? AND d.requirements_revision_id=?',(member['issue_id'],requirements_revision_id))}
            if not required<=set(numbers):
                raise TransitionError('dependency observation omits persisted prerequisites')
            own=db.execute('SELECT github_number FROM issues WHERE id=?',(member['issue_id'],)).fetchone()[0]
            clear=all(fact['state']=='closed' and fact['number']!=own for fact in facts)
            db.execute('INSERT INTO dependency_checks VALUES(?,?,?,?,?,?,?,?)',
                       (_id(command.request_id,'dependency-check'),unit['id'],member['id'],requirements_revision_id,generation,int(clear),_json(facts),self.clock()))
            gates=db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='dependency' AND member_id IS ? AND resolved_at IS NULL",
                             (unit['id'],member['id'])).fetchall()
            owned=[g for g in gates if json.loads(g['details_json']).get('source')=='runtime_dependencies']
            current_digest=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(requirements_revision_id,)).fetchone()[0])
            for gate in gates:
                details=json.loads(gate['details_json'])
                if details.get('requirements') is not None and details['requirements']!=current_digest:
                    self._resolve(db,command,gate['id'],{'superseded_by_requirements':requirements_revision_id})
            if clear:
                for gate in owned:
                    self._resolve(db,command,gate['id'],{'verified':True,'requirements_revision_id':requirements_revision_id,'generation':generation})
                # Worker-declared waits bind to the same requirements digest and
                # are cleared only if every named prerequisite was observed closed.
                digest=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(requirements_revision_id,)).fetchone()[0])
                for gate in gates:
                    details=json.loads(gate['details_json'])
                    if details.get('requirements')==digest and set(details.get('issues',[]))<=set(numbers):
                        self._resolve(db,command,gate['id'],{'verified':True,'requirements_revision_id':requirements_revision_id,'generation':generation})
            elif not owned:
                self._fence(db,command)
                self._open_gate(db,command,'dependency',{'source':'runtime_dependencies','requirements_revision_id':requirements_revision_id,
                    'blocked':[f['number'] for f in facts if f['state']!='closed' or f['number']==own]},member['id'])
            self._event(db,command,'lifecycle_changed',{'dependency_generation':generation,'clear':clear})
            return {'clear':clear,'generation':generation}
        return self._apply(command,'observe_dependencies',payload,change)

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
            member=self._singleton(db,unit)
            if member['requirements_revision_id']!=requirements_revision_id:
                raise TransitionError('environment selection quotes obsolete requirements')
            current=db.execute('SELECT revision FROM unit_environment WHERE unit_id=? ORDER BY revision DESC LIMIT 1',(unit['id'],)).fetchone()
            revision=current[0]+1 if current else 1
            target='env:'+choice['tier']
            for gate in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='environment' AND resolved_at IS NULL",(unit['id'],)).fetchall():
                if json.loads(gate['details_json']).get('source')=='runtime_selection':
                    self._resolve(db,command,gate['id'],{'environment_ready':choice,'requirements_revision_id':requirements_revision_id})
            db.execute('INSERT INTO unit_environment(unit_id,revision,choice,requirements_revision_id,config_hash,created_at) VALUES(?,?,?,?,?,?)',
                       (unit['id'],revision,_json(choice),requirements_revision_id,config_hash,self.clock()))
            db.execute('INSERT INTO unit_environment_observations(unit_id,choice_json,label) VALUES(?,?,?) ON CONFLICT(unit_id) DO UPDATE SET choice_json=excluded.choice_json,label=excluded.label',
                       (unit['id'],_json(choice),target))
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

    def claim_environment(self,effect_id,*,expected_version):
        with self.store.write() as db:
            writable(db)
            row=db.execute('SELECT e.*,l.revision,l.requirements_revision_id,l.member_id FROM external_effects e JOIN environment_label_effects l ON l.effect_id=e.id WHERE e.id=?',(effect_id,)).fetchone()
            if row is None or row['status'] not in {'pending','running','uncertain'}:
                raise TransitionError('environment effect is not runnable')
            unit=db.execute('SELECT * FROM units WHERE id=?',(row['unit_id'],)).fetchone()
            member=db.execute('SELECT * FROM unit_issues WHERE id=?',(row['member_id'],)).fetchone()
            latest=db.execute('SELECT MAX(revision) FROM unit_environment WHERE unit_id=?',(unit['id'],)).fetchone()[0]
            if (unit['version']!=expected_version or unit['state'] in {'merged','closed','dissolved'}
                    or member['left_at'] is not None or member['requirements_revision_id']!=row['requirements_revision_id']
                    or latest!=row['revision']):
                raise TransitionError('environment effect authority has changed')
            db.execute("UPDATE external_effects SET status='running',attempts=attempts+1,updated_at=? WHERE id=?",(self.clock(),effect_id))
            Effects(self.store,clock=self.clock)._effect_event(db,effect_id)

    def environment_problem(self,command,*,requirements_revision_id,reason):
        if not isinstance(reason,str) or not reason or len(reason)>2000:
            raise TransitionError('environment issue needs a bounded reason')
        def change(db,unit):
            self._live(unit)
            member=self._singleton(db,unit)
            if member['requirements_revision_id']!=requirements_revision_id:
                raise TransitionError('environment issue quotes obsolete requirements')
            self._fence(db,command)
            gate=self._open_gate(db,command,'environment',{'source':'runtime_selection','reason':reason,'requirements_revision_id':requirements_revision_id})
            return {'accepted':True,'gate_id':gate}
        return self._apply(command,'environment_problem',dict(requirements_revision_id=requirements_revision_id,reason=reason),change)

    def _set_override_in(self,db,command,unit,requirements_revision_id,value):
        from ..factory.environments import override as validate_override
        value=validate_override(value)
        self._live(unit)
        member=self._singleton(db,unit)
        if member['requirements_revision_id']!=requirements_revision_id:
            raise TransitionError('environment override quotes obsolete requirements')
        self._fence(db,command)
        db.execute('INSERT INTO unit_environment_observations(unit_id,override_json) VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET override_json=excluded.override_json,retry_json=NULL',
                   (unit['id'],_json(value) if value['mode']!='automatic' else None))
        for gate in db.execute("SELECT * FROM unit_gates WHERE unit_id=? AND code='environment' AND resolved_at IS NULL",(unit['id'],)).fetchall():
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
        member=self._singleton(db,unit)
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
            if job['role'] not in {'build','review','audit'} or not job['cleanup']:
                raise TransitionError('environment retry requires a cleaned pipeline assignment')
            for gate in applicable_gates(db,unit,job['member_id'],job['candidate_id']):
                if gate['code'] not in {'result_pending','cleanup_pending'} or json.loads(gate['details_json']).get('job_id')!=job_id:
                    raise TransitionError('another gate prevents environment retry')
            launch=db.execute('SELECT runner_config_json,branch FROM job_launches WHERE job_id=?',(job_id,)).fetchone()
            config=json.loads(launch['runner_config_json'])
            state=db.execute('SELECT * FROM unit_environment_observations WHERE unit_id=?',(unit['id'],)).fetchone()
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
                choice=json.loads(state['choice_json']) if state and state['choice_json'] else {}
                if (state and (state['retry_json'] or state['override_json']) or choice.get('automatic') is False
                        or not isinstance(wanted,list) or not wanted or set(wanted)-{'mongodb','postgres','redis'}
                        or set(wanted)<=set(config.get('services',[])) or evidence.get('full_environment_available') is not True):
                    raise TransitionError('service retry must be automatic, available, new and bounded to one retry')
                retry=dict(attempt=job_id,services=sorted(set(wanted)|set(config.get('services',[]))),branch=launch['branch'])
                field='retry_json'
            self._result(db,command,job,evidence,kind+'_retry')
            db.execute('INSERT INTO unit_environment_observations(unit_id,'+field+') VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET '+field+'=excluded.'+field,
                       (unit['id'],_json(retry)))
            db.execute('INSERT INTO environment_job_retries VALUES(?,?,?,?)',(job_id,kind,_json(selection),self.clock()))
            self._event(db,command,'lifecycle_changed',{'environment_retry':kind,'job_id':job_id,'selection':selection})
            return {'accepted':True,'retry':kind,'selection':selection}
        return self._apply(command,'retry_environment',payload,change)
