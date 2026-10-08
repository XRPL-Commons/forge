"""Authenticated operator intents with durable, independently read-back GitHub effects.

Requests fence work before returning. The supervisor executes bounded pending
requests; process restarts reconcile remote state before repeating a write.
"""
import json
import re
import time
import uuid
from urllib.parse import quote

from ..store.models import Command, IdempotencyConflict, TransitionError, VersionConflict
from ..store.queries import Queries
from ..store.runtime import writable
from ..store.transitions import Lifecycle, _id, _json, _unit, _gates, _charges, manifest_hash, project_unit
from ..store.runtime_requirements import RuntimeRequirements
from .github import GitHubUnavailable
from .ownership import ownership
from .store_views import action_revision, available_actions

ACTIONS = {'queue','backlog','pause','resume','retry','recheck','start_anyway','environment','close','assign_reviewer'}


def labels(issue):
    return {row['name'] if isinstance(row, dict) else row for row in issue.get('labels', [])}


class OperatorActions(Lifecycle):
    def __init__(self, store, github, *, actor_id=None, config, requirements=None, verification=None, clock=time.time):
        super().__init__(store, clock=clock)
        self.github, self.actor_id = github, actor_id
        self.config = config
        self.requirements, self.verification = requirements, verification

    def _config(self):
        return self.config() if callable(self.config) else self.config

    @staticmethod
    def _baseline(db, unit_id):
        unit = _unit(db, unit_id)
        members = [dict(row) for row in db.execute('SELECT id,issue_id,requirements_revision_id FROM unit_issues WHERE unit_id=? AND left_at IS NULL ORDER BY id', (unit_id,))]
        return {**{key:unit[key] for key in ('membership_generation','fencing_generation','budget_generation','current_candidate_id','state')}, 'members':members}

    def request(self, key, action, revision, reason, *, request_id=None, environment=None):
        actor = self.actor_id
        if not isinstance(actor,str) or not actor or len(actor)>256:
            raise ValueError('An authenticated operator identity is required')
        if action not in ACTIONS or not isinstance(reason,str) or not reason.strip() or len(reason)>2000:
            raise ValueError('A supported action and a short reason are required')
        identifier = request_id or str(uuid.uuid4())
        try:
            if str(uuid.UUID(identifier)) != identifier:raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise ValueError('Request identity must be a canonical UUID') from None
        payload = dict(key=key,action=action,revision=revision,reason=reason.strip(),environment=environment,actor=actor)
        fingerprint = manifest_hash(payload)
        config=self._config()
        target_branch=None
        # Unit target identity is immutable. For a new delivery, observe the
        # repository before admission and then recheck the public revision in
        # the transaction; a guessed 'main' can strand non-main repositories.
        with self.store.read_snapshot() as db:
            existing=db.execute('SELECT payload_digest FROM operator_action_authority WHERE request_id=?',(identifier,)).fetchone()
            if existing:
                if existing[0]!=fingerprint:raise IdempotencyConflict('Request identity already belongs to another decision')
                return identifier
            observed=Queries._issue(db,key)
            if observed['lifecycle']['unit_id'] is None:
                item=dict(repo_id=observed['repository_id'],repo=observed['repository'],number=observed['github_number'])
            else:item=None
        if item is not None:
            target_branch=self.github.target(item)
            if not isinstance(target_branch,str) or not target_branch or len(target_branch)>256:
                raise GitHubUnavailable('Repository target branch is unverified')
        with self.store.write() as db:
            writable(db)
            prior=db.execute('SELECT * FROM operator_action_authority WHERE request_id=?',(identifier,)).fetchone()
            if prior:
                if prior['payload_digest']!=fingerprint:raise IdempotencyConflict('Request identity already belongs to another decision')
                return identifier
            issue=Queries._issue(db,key)
            if action_revision(db,key)!=revision:raise VersionConflict('Issue changed; reload before deciding')
            if issue.get('body_truncated') or issue['github_state']!='open' or issue['observed_at'] is None or not 0<=self.clock()-issue['observed_at']<300:
                raise TransitionError('A fresh open issue observation is required')
            if db.execute("SELECT 1 FROM issue_action_requests WHERE issue_id=? AND status='pending'",(key,)).fetchone():
                raise TransitionError('This issue already has a pending decision')
            unit_id=issue['lifecycle']['unit_id']
            unit=_unit(db,unit_id) if unit_id else None
            cleanup=bool(unit and db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0',(unit_id,)).fetchone())
            allowed=available_actions(issue,unit,cleanup=cleanup)
            if unit and not any(g['code']=='operator_pause' for g in _gates(db,unit_id)):allowed.append('pause')
            if action=='close':allowed.append('close')
            if action=='environment' and unit and not cleanup:allowed.append('environment')
            if action=='start_anyway' and unit and not cleanup and any(g['code']=='dependency' and json.loads(g['details_json']).get('requirements') for g in _gates(db,unit_id)):
                allowed.append('start_anyway')
            if action not in allowed:raise TransitionError('Action is not available for the current issue')
            if action=='environment':
                from .environments import override
                environment=override(environment)
                payload['environment']=environment
            reviewers=[]
            if action=='assign_reviewer':
                from .issue_actions import reviewer_logins
                reviewers=reviewer_logins(reason)
                if not 1<=len(reviewers)<=10 or any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})',v) for v in reviewers):
                    raise ValueError('Choose one to ten GitHub reviewer logins')
            created=False
            if unit is None:
                if action not in {'queue','close'}:raise TransitionError('Issue has no active delivery')
                # A queued delivery remains fenced by operator_sync until the
                # remote intent is read back; ordinary readiness still applies.
                unit_id=_id(identifier,'unit')
                command=Command(identifier+':admit',actor,unit_id,1)
                raw=db.execute('SELECT i.*,r.enabled FROM issues i JOIN repositories r ON r.id=i.repository_id WHERE i.id=?',(key,)).fetchone()
                if not raw['enabled']:raise TransitionError('Repository is disabled')
                self._create_unit(db,command,unit_id,raw,target_branch,raw['current_revision_id'])
                unit=_unit(db,unit_id);created=True
            else:command=Command(identifier+':admit',actor,unit_id,unit['version'])
            self._live(unit)
            self._singleton(db,unit)
            if db.execute("SELECT 1 FROM merge_requests WHERE unit_id=? AND status IN ('pending','uncertain')",(unit_id,)).fetchone():
                raise TransitionError('Merge outcome must be reconciled before an operator decision')
            if action=='retry':
                from ..store.recovery import pending_mandatory
                if pending_mandatory(db,unit_id):raise TransitionError('Mandatory investigation must finish before retry')
            before=self._lifecycle_summary(project_unit(db,unit_id))
            if action in {'pause','backlog','close'}:
                self._fence(db,command)
                code='operator_backlog' if action=='backlog' else 'operator_pause'
                if not any(g['code']==code for g in _gates(db,unit_id)):
                    self._open_gate(db,command,code,{'request_id':identifier,'reason':reason.strip()})
            gate=self._open_gate(db,command,'operator_sync',{'request_id':identifier,'action':action})
            ready=config['ready_label']
            add=['hold'] if action=='pause' else [ready] if action in {'queue','resume','retry'} else []
            remove=[ready] if action=='backlog' else list(config['hold_labels']) if action=='resume' else []
            if action=='environment':remove=[v for v in issue['labels'] if v.startswith('env:')]
            plan=dict(baseline=self._baseline(db,unit_id),requirements=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(issue['current_revision_id'],)).fetchone()[0]),
                labels=issue['labels'],assignees=issue['assignees'],add=add,remove=remove,reviewers=reviewers)
            if action=='queue' and set(config['hold_labels']).intersection(issue['labels']):
                raise TransitionError('Remove the hold before queueing this issue')
            self._save(db,command,'request_operator_action',payload,{'request_id':identifier,'gate_id':gate},initial=created,before=before)
            db.execute("INSERT INTO issue_action_requests(id,issue_id,action,expected_revision,status,reason,message,plan_json,environment_json,created_at) VALUES(?,?,?,?,'pending',?,'Applying operator decision',?,?,?)",
                (identifier,key,action,revision,reason.strip(),_json(plan),_json(environment) if environment else None,self.clock()))
            db.execute('INSERT INTO operator_action_authority(request_id,actor_id,unit_id,expected_unit_version,expected_issue_version,payload_digest,gate_id,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                (identifier,actor,unit_id,unit['version'],issue['version'],fingerprint,gate,self.clock()))
        return identifier

    def _request(self, identifier):
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT q.*,a.actor_id,a.unit_id,a.gate_id FROM issue_action_requests q JOIN operator_action_authority a ON a.request_id=q.id WHERE q.id=?',(identifier,)).fetchone()
            if row is None:raise TransitionError('Request has no authenticated runtime authority')
            row=dict(row);row['plan']=json.loads(row['plan_json']);row['environment']=json.loads(row['environment_json']) if row['environment_json'] else None
            issue=Queries._issue(db,row['issue_id'])
            row['item']=dict(repo_id=issue['repository_id'],repo=issue['repository'],number=issue['github_number'],key=issue['id'])
            row['unit']=_unit(db,row['unit_id'])
            row['current']=self._baseline(db,row['unit_id'])==row['plan']['baseline']
            candidate=db.execute('SELECT * FROM candidates WHERE id=?',(row['unit']['current_candidate_id'],)).fetchone()
            row['candidate']=dict(candidate) if candidate else None
            return row

    def _intent(self,row,step,payload):
        with self.store.write() as db:
            writable(db)
            # Recheck local authority immediately before each remote effect.
            if self._baseline(db,row['unit_id'])!=row['plan']['baseline']:
                raise TransitionError('Delivery changed while applying the decision')
            previous=db.execute('SELECT payload_json FROM operator_action_effects WHERE request_id=? AND step=?',(row['id'],step)).fetchone()
            if previous and previous[0]!=_json(payload):raise IdempotencyConflict('Effect identity has another payload')
            db.execute("INSERT OR IGNORE INTO operator_action_effects VALUES(?,?,'pending',?,NULL,?,?)",(row['id'],step,_json(payload),self.clock(),self.clock()))

    def _receipt(self,row,step,receipt):
        with self.store.write() as db:
            db.execute("UPDATE operator_action_effects SET status='confirmed',receipt_json=?,updated_at=? WHERE request_id=? AND step=?",(_json(receipt),self.clock(),row['id'],step))

    def _remote(self,row):
        item,plan=row['item'],row['plan']
        issue,requirements=self.github.issue(item)
        if requirements!=plan['requirements'] or issue.get('number')!=item['number']:
            raise TransitionError('Issue requirements changed; reload before deciding')
        allowed_closed=row['action']=='close' and issue.get('state')=='closed' and issue.get('state_reason')=='not_planned'
        if issue.get('state')!='open' and not allowed_closed:raise TransitionError('Issue closure changed while applying the decision')
        changed=set(plan['add'])|set(plan['remove'])
        if {v for v in labels(issue)-changed if not v.startswith('forge:')} != {v for v in set(plan['labels'])-changed if not v.startswith('forge:')}:
            raise TransitionError('Issue labels changed while applying the decision')
        people=[v.get('login') if isinstance(v,dict) else v for v in issue.get('assignees',[])]
        if set(people)-set(plan['reviewers']) != set(plan['assignees'])-set(plan['reviewers']):
            raise TransitionError('Issue ownership changed while applying the decision')
        if row['action'] in {'queue','resume','retry','start_anyway'}:
            permitted={**issue,'labels':[v for v in issue.get('labels',[]) if (v.get('name') if isinstance(v,dict) else v) not in plan['remove']]}
            if labels(permitted).intersection(self._config()['hold_labels']) or ownership(permitted,item,item['repo']):
                raise TransitionError('A current hold or human owner prevents resuming work')
        return issue

    def _labels(self,row):
        issue=self._remote(row)
        for label in row['plan']['add']:
            step='add:'+label;self._intent(row,step,{'label':label})
            if label not in labels(issue):self.github.call('controller',row['item'],'POST',f"/issues/{row['item']['number']}/labels",{'labels':[label]})
            issue=self._remote(row)
            if label not in labels(issue):raise GitHubUnavailable('Operator label addition is unconfirmed')
            self._receipt(row,step,{'present':True})
        for label in row['plan']['remove']:
            step='remove:'+label;self._intent(row,step,{'label':label})
            if label in labels(issue):self.github.call('controller',row['item'],'DELETE',f"/issues/{row['item']['number']}/labels/"+quote(label,safe=''))
            issue=self._remote(row)
            if label in labels(issue):raise GitHubUnavailable('Operator label removal is unconfirmed')
            self._receipt(row,step,{'present':False})
        return issue

    def _close(self,row):
        candidate=row['candidate']
        if candidate:
            pr=self.github.pr(row['item'],candidate['pull_number'])
            if (pr.get('merged') is not False or (pr.get('head') or {}).get('sha')!=candidate['head_oid']
                    or (pr.get('base') or {}).get('sha')!=candidate['base_oid']):
                raise TransitionError('Candidate changed or merged; close decision needs reconciliation')
            self._intent(row,'close-pr',{'number':candidate['pull_number'],'head':candidate['head_oid']})
            if pr.get('state')=='open':self.github.call('controller',row['item'],'PATCH',f"/pulls/{candidate['pull_number']}",{'state':'closed'})
            pr=self.github.pr(row['item'],candidate['pull_number'])
            if (pr.get('state')!='closed' or pr.get('merged') is not False
                    or (pr.get('head') or {}).get('sha')!=candidate['head_oid'] or (pr.get('base') or {}).get('sha')!=candidate['base_oid']):
                raise GitHubUnavailable('Candidate closure is unconfirmed')
            self._receipt(row,'close-pr',{'closed':True,'merged':False})
        self._intent(row,'close-issue',{'number':row['item']['number'],'state_reason':'not_planned'})
        issue=self._remote(row)
        if issue.get('state')=='open':self.github.call('controller',row['item'],'PATCH',f"/issues/{row['item']['number']}",{'state':'closed','state_reason':'not_planned'})
        issue=self._remote(row)
        if issue.get('state')!='closed' or issue.get('state_reason')!='not_planned':raise GitHubUnavailable('Issue closure is unconfirmed')
        self._receipt(row,'close-issue',{'closed':True,'state_reason':'not_planned'})
        return issue

    def _reviewers(self,row):
        candidate=row['candidate'];names=row['plan']['reviewers']
        if candidate is None:raise TransitionError('Review request has no current candidate')
        pr=self.github.pr(row['item'],candidate['pull_number'])
        if pr.get('state')!='open' or (pr.get('head') or {}).get('sha')!=candidate['head_oid'] or (pr.get('base') or {}).get('sha')!=candidate['base_oid']:
            raise TransitionError('Review candidate changed')
        path=f"/pulls/{candidate['pull_number']}/requested_reviewers"
        self._intent(row,'reviewers',{'pull_number':candidate['pull_number'],'reviewers':names})
        current=self.github.call('controller',row['item'],'GET',path)
        seen={u['login'] for u in current.get('users',[])}
        if not set(names)<=seen:self.github.call('controller',row['item'],'POST',path,{'reviewers':names})
        current=self.github.call('controller',row['item'],'GET',path)
        if not set(names)<={u['login'] for u in current.get('users',[])}:raise GitHubUnavailable('Reviewer request is unconfirmed')
        pr=self.github.pr(row['item'],candidate['pull_number'])
        if pr.get('state')!='open' or (pr.get('head') or {}).get('sha')!=candidate['head_oid'] or (pr.get('base') or {}).get('sha')!=candidate['base_oid']:
            raise TransitionError('Review candidate changed')
        self._receipt(row,'reviewers',{'logins':names,'candidate_id':candidate['id']})
        return self._remote(row)

    def _complete(self,row,issue):
        command=Command(row['id']+':complete:'+str(row['unit']['version']),row['actor_id'],row['unit_id'],row['unit']['version'])
        def change(db,unit):
            if self._baseline(db,unit['id'])!=row['plan']['baseline']:raise TransitionError('Delivery changed while applying the decision')
            action=row['action'];gates=_gates(db,unit['id'])
            if action in {'resume','queue','retry','close'}:self._quiescent(db,unit['id'])
            if action=='retry':
                from ..store.recovery import pending_mandatory
                if pending_mandatory(db,unit['id']):raise TransitionError('Mandatory investigation must finish before retry')
                if _charges(db,unit):
                    db.execute('UPDATE units SET budget_generation=budget_generation+1 WHERE id=?',(unit['id'],))
                    db.execute("UPDATE unit_issues SET state='pending' WHERE unit_id=? AND left_at IS NULL AND state='failed'",(unit['id'],))
                    self._event(db,command,'budget_reset',{'before':unit['budget_generation'],'after':unit['budget_generation']+1,'reason':row['reason']})
                db.execute('INSERT INTO unit_context(unit_id,retry_plan_json) VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET retry_plan_json=excluded.retry_plan_json',
                    (unit['id'],_json({'reason':row['reason'],'actor_id':row['actor_id'],'request_id':row['id']})))
            requirements=RuntimeRequirements(self.store,clock=self.clock)
            revision=row['plan']['baseline']['members'][0]['requirements_revision_id']
            if action=='environment':requirements._set_override_in(db,command,unit,revision,row['environment'])
            if action=='start_anyway':requirements._clear_declared_dependencies_in(db,command,unit,revision,{'reason':row['reason'],'actor_id':row['actor_id']})
            if action=='assign_reviewer':
                db.execute('INSERT INTO candidate_reviewer_requests VALUES(?,?,?,?,?,0)',(unit['current_candidate_id'],row['id'],row['actor_id'],_json(row['plan']['reviewers']),self.clock()))
            resolve={'operator_sync'}
            if action in {'queue','resume'}:resolve|={'operator_pause','operator_backlog','label_hold'}
            if action=='retry':resolve|={'budget_exhausted','approach_required','decision_required'}
            for gate in gates:
                if gate['code'] in resolve:
                    if gate['code']=='operator_sync' and gate['id']!=row['gate_id']:raise TransitionError('Operator intent changed')
                    self._resolve(db,command,gate['id'],{'operator':row['actor_id'],'request_id':row['id'],'verified':True,'observed_at':self.clock()})
            # Fresh remote state is recorded with the same transaction as the
            # local release. Requirement changes were independently rejected.
            db.execute('UPDATE issues SET labels_json=?,assignees_json=?,observed_at=?,version=version+1 WHERE id=?',
                (_json(sorted(labels(issue))),_json([u['login'] if isinstance(u,dict) else u for u in issue.get('assignees',[])]),self.clock(),row['issue_id']))
            if action=='close':
                db.execute("UPDATE issues SET github_state='closed',state_reason='not_planned' WHERE id=?",(row['issue_id'],))
                self._settle(db,command,unit,'closed',close_code='github_not_planned')
            db.execute("UPDATE issue_action_requests SET status='applied',message='Operator decision applied' WHERE id=?",(row['id'],))
            db.execute('UPDATE operator_action_authority SET result_json=?,updated_at=? WHERE request_id=?',(_json({'applied':True}),self.clock(),row['id']))
            return {'request_id':row['id'],'applied':True}
        return self._apply(command,'complete_operator_action',{'request_id':row['id']},change)

    def _reject(self,row,message):
        with self.store.write() as db:
            command=Command(row['id']+':reject',row['actor_id'],row['unit_id'],_unit(db,row['unit_id'])['version'])
            gate=db.execute('SELECT * FROM unit_gates WHERE id=? AND resolved_at IS NULL',(row['gate_id'],)).fetchone()
            if gate:
                self._resolve(db,command,gate['id'],{'rejected':True})
                if _unit(db,row['unit_id'])['state'] not in {'merged','closed','dissolved'}:
                    if not any(g['code']=='external_change' for g in _gates(db,row['unit_id'])):
                        self._open_gate(db,command,'external_change',{'request_id':row['id'],'reason':message})
                    self._fence(db,command)
                self._save(db,command,'reject_operator_action',{'request_id':row['id']},{'rejected':True})
            db.execute("UPDATE issue_action_requests SET status='rejected',message=? WHERE id=?",(message,row['id']))
            db.execute('UPDATE operator_action_authority SET result_json=?,updated_at=? WHERE request_id=?',(_json({'rejected':True,'reason':message}),self.clock(),row['id']))
        return {'request_id':row['id'],'rejected':True}

    def step(self,identifier):
        with self.store.read_snapshot() as db:
            imported=db.execute('SELECT q.issue_id FROM issue_action_requests q JOIN imported_operator_requests p ON p.request_id=q.id LEFT JOIN operator_action_authority a ON a.request_id=q.id WHERE q.id=? AND a.request_id IS NULL',(identifier,)).fetchone()
        if imported:
            from .operator_reconciliation import ImportedOperatorController
            return {'request_id':identifier,**ImportedOperatorController(self.store,self.github,clock=self.clock).step(imported[0])}
        row=self._request(identifier)
        if row['status']!='pending':return {'request_id':identifier,'status':row['status']}
        try:
            if not row['current']:raise TransitionError('Delivery changed while applying the decision')
            if row['action'] in {'resume','queue','retry','close'}:
                with self.store.read_snapshot() as db:
                    if row['action']=='retry':
                        from ..store.recovery import pending_mandatory
                        if pending_mandatory(db,row['unit_id']):return {'request_id':identifier,'pending':'recovery'}
                    if db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0',(row['unit_id'],)).fetchone():
                        return {'request_id':identifier,'pending':'cleanup'}
            issue=self._labels(row)
            if row['action']=='close':issue=self._close(row)
            if row['action']=='assign_reviewer':issue=self._reviewers(row)
            row=self._request(identifier)
            result=self._complete(row,issue)
            if row['action'] in {'recheck','environment','start_anyway'} and self.requirements:
                try:self.requirements.observe(row['unit_id'])
                except (GitHubUnavailable,TransitionError):pass  # The independent requirements stage retries; intent is applied.
            return result
        except GitHubUnavailable:
            return {'request_id':identifier,'pending':'github_observation'}
        except VersionConflict:
            return {'request_id':identifier,'pending':'concurrent_observation'}
        except TransitionError as error:
            return self._reject(row,str(error))

    def cycle(self,*,after='',limit=50):
        if not 1<=limit<=100:raise ValueError('Invalid action page size')
        with self.store.read_snapshot() as db:
            ids=[r[0] for r in db.execute("SELECT q.id FROM issue_action_requests q LEFT JOIN operator_action_authority a ON a.request_id=q.id LEFT JOIN imported_operator_requests p ON p.request_id=q.id WHERE q.status='pending' AND (a.request_id IS NOT NULL OR p.request_id IS NOT NULL) AND q.id>? ORDER BY q.id LIMIT ?",(after,limit+1))]
        return {'items':[self.step(identifier) for identifier in ids[:limit]],'next_cursor':ids[limit-1] if len(ids)>limit else None}

    def verify(self,key,revision,gate_id,statement,*,request_id=None):
        if self.verification is None or not self.actor_id:raise TransitionError('Operator verification is unavailable')
        identifier=request_id or str(uuid.uuid4())
        if not isinstance(statement,str) or not statement.strip() or len(statement)>2000:raise ValueError('Describe the verification that passed')
        try:uuid.UUID(identifier)
        except (ValueError,TypeError,AttributeError):raise ValueError('Invalid verification identity') from None
        fingerprint=manifest_hash(statement)
        with self.store.write() as db:
            writable(db)
            previous=db.execute('SELECT * FROM operator_verification_authority WHERE request_id=?',(identifier,)).fetchone()
            if previous:
                if (previous['actor_id'],previous['issue_id'],previous['expected_revision'],previous['gate_id'],previous['statement_digest'])!=(self.actor_id,key,revision,gate_id,fingerprint):
                    raise IdempotencyConflict('Verification identity already belongs to another decision')
                unit_id,version=previous['unit_id'],previous['expected_unit_version']
            else:
                if action_revision(db,key)!=revision:raise VersionConflict('Issue changed; reload before verifying')
                issue=Queries._issue(db,key);unit_id=issue['lifecycle']['unit_id'];version=issue['lifecycle']['unit_version']
                gate=db.execute("SELECT id FROM unit_gates WHERE id=? AND unit_id=? AND code='verification_human' AND resolved_at IS NULL",(gate_id,unit_id)).fetchone()
                if gate is None:raise TransitionError('The issue has no matching human verification request')
                db.execute('INSERT INTO operator_verification_authority VALUES(?,?,?,?,?,?,?,?,?)',
                    (identifier,self.actor_id,key,unit_id,version,revision,gate_id,fingerprint,self.clock()))
        return self.verification.confirm_human(Command(identifier,self.actor_id,unit_id,version),gate_id,statement=statement)

    def stop_job(self,job_id,reason,*,action='stop'):
        if action not in {'stop','reset'} or not isinstance(reason,str) or not reason.strip() or len(reason)>2000 or not self.actor_id:
            raise ValueError('A stop or reset decision and authenticated actor are required')
        identifier=str(uuid.uuid4())
        with self.store.read_snapshot() as db:
            job=db.execute('SELECT unit_id,cleanup FROM jobs WHERE id=?',(job_id,)).fetchone()
            if job is None:raise TransitionError('Unknown execution')
            unit=_unit(db,job['unit_id'])
        command=Command(identifier,self.actor_id,unit['id'],unit['version'])
        def change(db,current):
            if current['state'] not in {'merged','closed','dissolved'} and not any(g['code']=='operator_pause' for g in _gates(db,current['id'])):
                self._open_gate(db,command,'operator_pause',{'reason':reason.strip(),'job_id':job_id})
            self._fence(db,command)
            db.execute("INSERT INTO operator_requests(id,action,target,reason,status,result_json) VALUES(?,?,?,?,'applied',?)",
                (identifier,action,job_id,reason.strip(),_json({'actor_id':self.actor_id,'fenced':True})))
            return {'job_id':job_id,'fenced':True}
        self._apply(command,'operator_stop_job',{'job_id':job_id,'action':action,'reason':reason.strip()},change)
        return identifier

    def request_check(self,key):
        if not self.actor_id:raise TransitionError('An authenticated operator identity is required')
        with self.store.write() as db:
            writable(db)
            if not db.execute('SELECT 1 FROM issues WHERE id=?',(key,)).fetchone():raise TransitionError('Unknown issue')
            db.execute('INSERT INTO operator_observation_requests VALUES(?,?,?) ON CONFLICT(issue_id) DO UPDATE SET actor_id=excluded.actor_id,requested_at=excluded.requested_at',(key,self.actor_id,self.clock()))


def selected_service(root, *, actor_id=None):
    """Build HTTP/CLI services only after explicit runtime selection."""
    from ..store.runtime import selected
    store=selected(root)
    if store is None:return None
    from .sqlite_runtime import operator_actions
    return operator_actions(root,actor_id)
