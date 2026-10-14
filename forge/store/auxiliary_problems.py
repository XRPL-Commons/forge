"""Owned execution waits for triage and discussion; never issue budget charges."""
import json
import re
import time
from .runtime import writable
from .models import TransitionError
from .transitions import _json


def replacement_in(db, config, detail, now):
    rows = {row['domain']: json.loads(row['settings_json']) for row in db.execute(
        "SELECT domain,settings_json FROM configuration WHERE subject='instance' AND active=1 AND domain IN ('host.json','runtime.json')")}
    if set(rows) != {'host.json','runtime.json'}:
        return False
    runtime, host = rows['runtime.json'], rows['host.json']
    tier = config.get('environment_tier','standard')
    target = runtime.get('environment_tiers',{}).get(tier,{}).get('image',runtime.get('image') if tier=='standard' else None)
    qualified = host.get('image' if tier=='standard' else 'full_image')
    return bool(isinstance(target,str) and re.fullmatch(r'(?:[A-Za-z0-9._:/-]+@)?sha256:[0-9a-f]{64}',target)
        and target==qualified and target not in (config.get('image'),detail.get('image_id'))
        and type(host.get('updated_at')) in (int,float) and 0 <= host['updated_at'] <= now)


class AuxiliaryProblems:
    def __init__(self, store, *, clock=time.time):
        self.store,self.clock=store,clock

    def record(self, domain, owner, job, capture, *, invalid=False):
        from ..factory.execution_problems import classify, contract_problem
        decision=classify(job,capture,self.clock())
        if decision and decision['kind']=='ok' and invalid:
            decision={'kind':'forge_problem','details':contract_problem(job,capture)}
        if not decision or decision['kind'] not in ('forge_problem','provider_error','oom'):
            return None
        kind,detail=decision['kind'],decision['details']
        config=job['runner_config']
        if kind=='oom':
            from .runtime_requirements import _memory
            used=_memory(config['memory'])
            wanted=next((value for value in config.get('memory_tiers',[]) if _memory(value)>used),None)
            detail={**detail,'next_memory':wanted}
        if kind=='provider_error':
            detail={**detail,'account_ref':config.get('provider_auth_file_env') or job.get('provider_id') or config.get('provider')}
        with self.store.write() as db:
            writable(db)
            prior=db.execute('SELECT * FROM auxiliary_execution_waits WHERE execution_id=?',(job['id'],)).fetchone()
            if prior:return dict(prior)
            if domain=='triage':
                from .auxiliary import job_in
                actual=job_in(db,job['id'])
                run=db.execute('SELECT * FROM repository_triage_runs WHERE id=?',(owner,)).fetchone()
                if not run or run['execution_id']!=job['id'] or not actual['cleanup'] or actual.get('capture')!=capture:
                    raise TransitionError('Triage wait requires exact cleaned retained capture')
                db.execute("UPDATE repository_triage_runs SET status='captured',error=? WHERE id=?",(kind,owner))
            elif domain=='discussion':
                from .discussions import DiscussionStore
                thread=DiscussionStore._thread(db,owner)
                actual=DiscussionStore._job(db,job['id'])
                if not thread or thread['current_execution_id']!=job['id'] or thread['cancel_requested'] or actual.get('ended'):
                    raise TransitionError('Discussion wait requires current uncancelled execution')
                DiscussionStore(self.store,clock=self.clock)._capture(db,job['id'],capture)
                db.execute('UPDATE discussion_executions SET fenced=1 WHERE id=?',(job['id'],))
                db.execute('UPDATE discussion_runtime SET ended=? WHERE execution_id=?',(kind,job['id']))
                question=db.execute("SELECT id FROM messages WHERE discussion_id=? AND role='operator' ORDER BY sequence DESC LIMIT 1",(thread['id'],)).fetchone()
                detail={**detail,'question_id':question[0] if question else None}
                reason={'forge_problem':'Forge needs a qualified runner repair before retrying this reply.',
                        'provider_error':'The coding provider is unavailable; this reply will retry after the cooldown.',
                        'oom':'This reply needs more memory.'}[kind]
                presence=dict(phase='finding',code=kind,reason=reason,until=detail.get('retry_at'))
                db.execute("UPDATE discussions SET status='queued',reason=?,presence_json=?,version=version+1,updated_at=? WHERE id=?",
                    (reason,_json(presence),self.clock(),thread['id']))
            else:raise TransitionError('Unsupported execution wait domain')
            db.execute('INSERT INTO auxiliary_execution_waits VALUES(?,?,?,?,?,?,?,NULL)',
                (job['id'],domain,str(owner),kind,_json(detail),_json(config),self.clock()))
            if kind=='forge_problem':
                safe={key:detail[key] for key in ('signature','phase','exception','file','line')}
                db.execute('INSERT OR IGNORE INTO forge_problem_reports VALUES(?,?,?)',(safe['signature'],_json(safe),self.clock()))
                db.execute('INSERT INTO forge_problem_report_auxiliary VALUES(?,?)',(safe['signature'],job['id']))
        return dict(kind=kind,details=detail)

    def recheck(self, domain, owner, config=None):
        with self.store.write() as db:
            writable(db)
            wait=db.execute('SELECT * FROM auxiliary_execution_waits WHERE domain=? AND owner_id=? AND resolved_at IS NULL',(domain,str(owner))).fetchone()
            if wait is None:return None
            detail=json.loads(wait['details_json']); previous=json.loads(wait['runner_config_json'])
            current=config or previous
            now=self.clock()
            if domain=='discussion':
                from .discussions import DiscussionStore
                thread=DiscussionStore._thread(db,owner)
                question=db.execute("SELECT id FROM messages WHERE discussion_id=? AND role='operator' ORDER BY sequence DESC LIMIT 1",(thread['id'],)).fetchone()
                if not question or question[0]!=detail.get('question_id') or thread['current_execution_id']!=wait['execution_id'] or thread['cancel_requested']:
                    db.execute('UPDATE auxiliary_execution_waits SET resolved_at=? WHERE execution_id=?',(now,wait['execution_id']))
                    return None
            if wait['kind']=='oom':
                from .runtime_requirements import _memory
                next_memory=next((tier for tier in current.get('memory_tiers',[]) if _memory(tier)>_memory(previous['memory'])),None)
                detail={**detail,'next_memory':next_memory}
                db.execute('UPDATE auxiliary_execution_waits SET details_json=? WHERE execution_id=?',(_json(detail),wait['execution_id']))
            ready=(wait['kind']=='forge_problem' and replacement_in(db,previous,detail,now)
                or wait['kind']=='provider_error' and now>=detail['retry_at']
                or wait['kind']=='oom' and detail.get('next_memory') is not None)
            if not ready:return {'pending':wait['kind']}
            if domain=='triage':
                run=db.execute('SELECT * FROM repository_triage_runs WHERE id=?',(owner,)).fetchone()
                if not run or run['execution_id']!=wait['execution_id'] or run['status']!='captured':return {'pending':'assignment_changed'}
                db.execute("UPDATE repository_triage_runs SET status='queued',execution_id=NULL,error=NULL,retry_at=NULL WHERE id=?",(owner,))
            else:
                from .discussions import DiscussionStore
                thread=DiscussionStore._thread(db,owner)
                job=DiscussionStore._job(db,wait['execution_id'])
                if thread['current_execution_id']!=wait['execution_id'] or thread['cancel_requested'] or thread['status']!='queued':return {'pending':'assignment_changed'}
                if not job['cleanup']:return {'pending':'cleanup'}
                db.execute('UPDATE discussions SET requested_at=?,version=version+1,updated_at=? WHERE id=?',(now,now,thread['id']))
            db.execute('UPDATE auxiliary_execution_waits SET resolved_at=? WHERE execution_id=?',(now,wait['execution_id']))
            return {'resolved':True}

    def memory(self, domain, owner, config):
        with self.store.read_snapshot() as db:
            rows=db.execute("SELECT details_json FROM auxiliary_execution_waits WHERE domain=? AND owner_id=? AND kind='oom' AND resolved_at IS NOT NULL",
                            (domain,str(owner))).fetchall()
        if not rows:return config
        from .runtime_requirements import _memory
        minimum=max([_memory(config['memory']), *[_memory(value) for row in rows if (value:=json.loads(row[0]).get('next_memory'))]])
        chosen=next((tier for tier in config.get('memory_tiers',[]) if _memory(tier)>=minimum),None)
        if chosen is None:raise TransitionError('Memory retry requires the previously selected allocation')
        return {**config,'memory':chosen}
