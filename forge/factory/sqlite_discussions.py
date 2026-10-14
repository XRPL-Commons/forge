"""Live discussion service backed by bounded, typed SQLite row operations."""
from copy import deepcopy
from pathlib import Path
import json
import time
import uuid

from ..store.artifacts import Artifacts, ArtifactExpired
from ..store.discussions import DiscussionStore
from ..store.executions import provider_available
from ..store.models import TransitionError
from .activity import merge, public
from .discussions import (ACTIONS, BUTTONS, ANSWER_CAP, REASON_CAP, DISCUSSION_SECONDS, FINDING_SECONDS,
    KEY, ACCOUNTS, Discussions, agent, closed_for, ended, excerpt, offered, operator_only, partial_text, presence, proposal_in, trim)
from .github import GitHubUnavailable
from .runner import ContainerRunner, RunnerError, RunnerCapacity, RunnerUnavailable, scrub


class SQLiteDiscussions:
    def __init__(self, root, *, store, runner=ContainerRunner, inventory=None, clock=time.time, boot=None,
                 actor_id='local:control-capability'):
        from .issues import IssueInventory
        self.root, self.db, self.runner, self.clock, self.boot = Path(root), store, runner, clock, boot
        self.actor_id = actor_id
        self.commands = DiscussionStore(store, clock=clock)
        from ..store.auxiliary_problems import AuxiliaryProblems
        self.problems = AuxiliaryProblems(store,clock=clock)
        self.inventory = inventory or IssueInventory(root)

    def issue(self,key):
        if not isinstance(key,str) or not KEY.fullmatch(key):
            raise ValueError('Choose a managed issue')
        return self.inventory.view(key=key)['issue']

    def read(self):
        # Compatibility-shaped read only. Controllers query active or queued rows
        # directly; mutations never write this projection back into storage.
        return {key:self.commands.thread(key) for key in self.commands.keys(limit=1000)}

    def active_jobs(self):
        return self.commands.active()

    def live(self):
        return bool(self.active_jobs() or self.commands.keys(queued=True,limit=1) or self.commands.pending_actions(limit=1))

    def view(self,key,before=None):
        self.issue(key)
        thread=self.commands.thread(key,before=before,limit=20)
        job=thread.get('job') or {}
        return {k:thread.get(k) for k in ('messages','before','status','reason','origin','partial')} | dict(
            request=job.get('id'),mode='reconstructed',provider=job.get('runner_config',{}).get('provider'),presence=presence(thread))

    def send(self,key,message,request_id,*,actor_id=None):
        row=self.issue(key)
        if not isinstance(message,str) or not 1<=len(message.strip())<=4000:
            raise ValueError('Write a question of at most 4000 characters')
        try: uuid.UUID(request_id)
        except (TypeError,ValueError,AttributeError): raise ValueError('Invalid message identity') from None
        return self.commands.send(key,message.strip(),request_id,row['revision'],actor=actor_id or self.actor_id)

    def cancel(self,key,*,actor_id=None):
        self.issue(key)
        if self.commands.cancel(key,actor=actor_id or self.actor_id):
            self.left(key,None,'cancelled')

    def cancel_all(self):
        for key in self.commands.keys(limit=1000):
            if self.commands.cancel(key,actor=self.actor_id):
                self.left(key,None,'cancelled')

    def _submit(self,key,intent):
        from .issue_actions import request
        with self.db.read_snapshot() as db:issue_id=self.commands._issue(db,key)['id']
        try:
            receipt=request(self.root,key=issue_id,revision=intent['revision'],action=intent['action'],reason=intent['reason'],
                            request_id=intent['request_id'],actor_id=intent['actor_id'])
        except GitHubUnavailable:
            return intent['request_id']  # Durable intent remains pending for the next supervisor cycle.
        except ValueError as error:
            self.commands.mark(key,intent['request_id'],actor=intent['actor_id'],action_error=scrub(str(error))[:400])
            return intent['request_id']
        self.commands.receipt(key,intent['request_id'],receipt,actor=intent['actor_id'])
        return receipt

    def apply(self,key,message_id,*,actor_id=None,reason=None):
        self.issue(key)
        intent=self.commands.request_action(key,message_id,actor=actor_id or self.actor_id,reason=reason)
        return self._submit(key,intent)

    def reconcile_actions(self):
        for key,intent in self.commands.pending_actions():self._submit(key,intent)

    def undo(self,key,message_id,*,actor_id=None):
        self.issue(key)
        found=next((m for m in self.commands.thread(key)['messages'] if m['id']==message_id and m['role']=='assistant'),None)
        state=found.get('action_state') if found else None
        if not state or (not state['reversible'] and not state.get('undo')):
            raise ValueError('There is no confirmed close to undo')
        from .store_operator import selected_service
        service=selected_service(self.root,actor_id=actor_id or self.actor_id)
        if service is None:raise ValueError('Reopen the issue on GitHub')
        with self.db.read_snapshot() as db:issue=self.commands._issue(db,key)
        return service.reopen(issue['id'],state['request_id'])

    def context(self,key,row):
        with self.db.read_snapshot() as db:
            issue=self.commands._issue(db,key)
            unit=db.execute('SELECT u.* FROM units u JOIN unit_issues m ON m.unit_id=u.id WHERE m.issue_id=? ORDER BY m.joined_at DESC LIMIT 1',(issue['id'],)).fetchone()
            jobs=db.execute('SELECT j.*,l.provider FROM jobs j LEFT JOIN job_launches l ON l.job_id=j.id WHERE j.unit_id=? ORDER BY j.created_at DESC,j.id DESC LIMIT 4',(unit['id'],)).fetchall() if unit else []
            identity=db.execute('SELECT * FROM candidates WHERE id=?',(unit['current_candidate_id'],)).fetchone() if unit else None
            candidate=dict(key=identity['id'],pr=identity['pull_number'],branch=identity['branch'],head=identity['head_oid'],base=identity['base_oid']) if identity else None
            events=db.execute('SELECT kind,at,payload_json FROM events WHERE issue_id=? OR unit_id=? ORDER BY sequence DESC LIMIT 10',(issue['id'],unit['id'] if unit else None)).fetchall()
            decision=db.execute("SELECT details_json FROM unit_gates WHERE unit_id=? AND code='decision_required' AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1",(unit['id'],)).fetchone() if unit else None
        evidence=[]
        for job in jobs:
            try:
                capture=json.loads(Artifacts(self.db).read(job['id'],'worker_capture').content)
                result=capture.get('result') or {}
            except (KeyError,ArtifactExpired): result={}
            if not isinstance(result,dict): result={}
            evidence.append(dict(attempt=job['id'],role=job['role'],provider=job['provider'],phase=job['phase'],outcome=job['outcome'],
                created_at=job['created_at'],summary=scrub(str(result.get('summary','')))[:10000],link='/issues/'+key+'?job='+job['id']))
        actions=offered(row)
        issue_row={k:row.get(k) for k in ('key','repo','number','title','body','status','reason','dependencies','pull_requests')}
        issue_row['body']=str(issue_row.get('body') or '')[:24000]
        history=[dict(at=e['at'],kind=e['kind'],reason=scrub(str(json.loads(e['payload_json']).get('reason') or ''))[:2000]) for e in reversed(events)]
        context=dict(issue=issue_row,issue_url='https://github.com/'+row['repo']+'/issues/'+str(row['number']),actions=actions,
            available_actions=[dict(action=a,button=BUTTONS[a][0],effect=BUTTONS[a][1]) for a in actions],operator_only=operator_only(row),
            revision=row['revision'],candidate=candidate,attempts=evidence,decision_request=json.loads(decision[0]) if decision else None,
            history=history,observed_at=self.clock())
        return context,evidence[0] if evidence else None

    def waiting(self,code,reason,*,until=None,link=None,skip=None):
        for key in self.commands.keys(queued=True):
            if key==skip: continue
            thread=self.commands.thread(key,limit=1)
            here=thread.get('presence') or {}
            found=dict(phase='finding',waiting_since=here.get('waiting_since') or thread.get('requested_at'),code=code,reason=reason,
                       until=until,link=link,instead_of=here.get('instead_of'))
            self.commands.waiting(key,presence=found,reason=reason)

    def left(self,key,job,reason,detail=None,until=None,*,capture=None):
        thread=self.commands.thread(key)
        version=thread['version']
        if thread.get('cancel_requested'):
            reason,detail,until='cancelled',None,None
        if job: job=self.commands.job(job['id']) | {k:v for k,v in job.items() if k=='excerpt'}
        Discussions.leave(self,thread,job,reason,detail,until)
        self.commands.leave(key,expected_version=version,thread=thread,job=job,capture=capture)

    def _pipeline_priority(self,config):
        if not config.get('memory_tiers'): return False
        from .dispatch_controller import DispatchController
        driver=DispatchController(self.db,None,config=config,prepare=None,admission=lambda _:False,clock=self.clock)
        with self.db.read_snapshot() as db:
            units=db.execute("SELECT DISTINCT u.id FROM units u JOIN unit_issues m ON m.unit_id=u.id AND m.left_at IS NULL JOIN issue_environment_observations e ON e.issue_id=m.issue_id WHERE e.memory_retry_json IS NOT NULL AND u.state IN ('queued','building','reviewing','auditing') LIMIT 100").fetchall()
        return any(not driver.snapshot(row['id'])['reason'] for row in units)

    def tick(self,config,github,settings,*,launch=True):
        self.reconcile_actions()
        self.commands.prune_logs()
        active=self.active_jobs()
        if active:
            self.drive(active[0],github)
            if launch: self.waiting('busy','Another conversation is using the discussion worker; one runs at a time',skip=active[0]['issue_key'])
            return
        if not launch: return
        if not config.get('enabled'): return self.waiting('disabled',"Forge's agents are switched off")
        if settings.paused(): return self.waiting('paused','Forge is paused')
        if self._pipeline_priority(config): return self.waiting('pipeline','Waiting for pipeline work to launch first')
        for key in self.commands.keys(queued=True):
            thread=self.commands.thread(key)
            wait=self.problems.recheck('discussion',key,config)
            if wait and not wait.get('resolved'):continue
            if wait:thread=self.commands.thread(key)
            if self.clock()-thread['requested_at']>FINDING_SECONDS:
                self.left(key,None,'timeout'); continue
            try: row=self.issue(key)
            except (KeyError,ValueError):
                self.left(key,None,'error','This issue is no longer available in a managed repository'); continue
            context,origin=self.context(key,row)
            pool=config.get('provider_pool') or [dict(id=None,**{k:config.get(k) for k in ('provider','model','provider_environment','provider_auth_file_env')})]
            pool=sorted(pool,key=lambda e:e['provider']!=(origin or {}).get('provider'))
            ready=config.get('provider_ready'); now=self.clock(); avoid=thread.get('avoid') or {}
            with self.db.read_snapshot() as db:
                entry=next((e for e in pool if (ready is None or e['id'] in ready) and (avoid.get(e['provider']) or {}).get('until',0)<=now
                    and provider_available(db,e['provider'],e['id'],{**config,**{k:v for k,v in e.items() if k!='id'}},now)),None)
            if entry is None:
                self.waiting('accounts','Waiting for an available coding account',link=ACCOUNTS); return
            worker={**config,**{k:v for k,v in entry.items() if k!='id'}}
            worker=self.problems.memory('discussion',key,worker)
            if worker is None:continue
            quick=(config.get('discussion_model') or {}).get(worker['provider'])
            if quick: worker['model']=quick
            job=dict(id=str(uuid.uuid4()),slot='issue-discussion',role='discussion',issue_key=key,repo=row['repo'],repo_id=row['repo_id'],
                issue_number=row['number'],candidate=context['candidate'],deadline=now+DISCUSSION_SECONDS,fenced=False,cleanup=False,
                provider_id=entry['id'],runner_config=worker,context=context,boot=self.boot,created_at=now,
                messages=[{k:m[k] for k in ('role','text')} for m in thread['messages'] if m['role'] in {'user','assistant'}][-12:])
            self.commands.reserve(key,job,expected_version=thread['version'],origin=origin)
            self.drive(job,github)
            return

    def drive(self,job,github):
        job=self.commands.job(job['id'])
        key=job['issue_key']; runner=self.runner(job['runner_config'])
        try:
            now=self.clock()
            if job['fenced'] or now>=job['deadline']:
                if not job.get('completed') and not job.get('ended'):
                    self.left(key,job,'cancelled' if job['fenced'] else 'timeout')
                job=self.commands.job(job['id'])
                runner.reset(job)
                self.commands.cleanup(job['id'])
                return
            current=runner.inspect(job)
            orphaned=self.boot is not None and job.get('boot')!=self.boot
            if current is None:
                if job.get('container_id'):
                    self.left(key,job,'restart' if orphaned else 'error',None if orphaned else 'its worker container disappeared'); return
                identifier=runner.create(job)
                if not self.commands.progress(job['id'],container_id=identifier): return
                job=self.commands.job(job['id']); current=runner.inspect(job)
            if orphaned and current['running']:
                self.commands.progress(job['id'],boot=self.boot)
            if current['status']=='created':
                job=self.commands.job(job['id'])
                if job['fenced']: return
                runner.prepare(job,github.token('recovery',job['repo_id'],worker=True))
                if self.commands.job(job['id'])['fenced']: return
                runner.start(job)
                self.commands.progress(job['id'],joined_at=self.clock())
                return
            if current['running']:
                observation=runner.observe(job,current); logs=observation.get('logs','')
                thread=self.commands.thread(key,limit=1); here=thread.get('presence') or {}
                if here.get('phase')=='joined':
                    here['activity'],here['seen']=merge(here.get('activity'),here.get('seen'),logs,self.clock(),here.get('joined_at'))
                self.commands.activity(key,job['id'],partial=partial_text(logs),presence=here)
                return
            capture=runner.capture(job); result=capture.get('result')
            invalid=(not isinstance(result,dict) or result.get('attempt')!=job['id'] or result.get('role')!='discussion'
                or result.get('outcome') not in {'pass','reject','unable','wait','blocked_environment'}
                or not isinstance(result.get('summary'),str) or not result['summary'].strip())
            fault=self.problems.record('discussion',key,job,capture,invalid=invalid)
            if fault:
                job=self.commands.job(job['id']);runner.reset(job);self.commands.cleanup(job['id'])
                return
            if (not isinstance(result,dict) or result.get('attempt')!=job['id'] or result.get('role')!='discussion' or result.get('outcome')!='pass'):
                job['excerpt']=excerpt(capture.get('logs'))
                reason,detail,until=ended(capture,job,self.clock())
                if reason=='error' and orphaned: reason,detail='restart',None
                self.left(key,job,reason,detail,until,capture=capture); return
            answer=result.get('summary')
            if not isinstance(answer,str) or not answer.strip(): raise RunnerError('its answer was empty')
            proposal,unavailable,close_now=proposal_in(result,job)
            # Only the operator who wrote the latest message, in those words, lets the persona close.
            asker=self.commands.asker(key) if close_now else None
            close_now=bool(asker and asker[1]==job['messages'][-1]['text'])
            thread=self.commands.thread(key,limit=1); here=thread.get('presence') or {}
            activity,_=merge(here.get('activity'),here.get('seen'),capture.get('logs',''),self.clock(),here.get('joined_at'))
            entry=dict(id=job['id'],role='assistant',text=scrub(trim(answer,ANSWER_CAP)),proposal=proposal,
                revision=job['context']['revision'],at=self.clock(),agent=agent(job),joined_at=job.get('joined_at'),
                instead_of=here.get('instead_of'),activity=public(activity))
            if unavailable: entry['unavailable']=True
            if close_now: entry['requested']=asker[1]
            accepted=self.commands.answer(key,job['id'],entry,capture=capture,presence=dict(phase='answered',agent=agent(job),at=self.clock()))
            if accepted and close_now:self.reconcile_actions()
            if accepted:
                job=self.commands.job(job['id']); runner.reset(job); self.commands.cleanup(job['id'])
        except RunnerCapacity:
            self.waiting('capacity','Waiting for memory; pipeline workers have priority')
        except (RunnerError,RunnerUnavailable,GitHubUnavailable) as error:
            current=self.commands.job(job['id'])
            if current.get('completed') or current.get('ended'): return
            if isinstance(error,GitHubUnavailable): detail='GitHub did not grant a read-only repository token'
            elif isinstance(error,RunnerUnavailable): detail='the container engine is unavailable'
            else: detail=scrub(str(error))[:200] or 'its worker could not complete'
            self.left(key,current,'error',detail)
