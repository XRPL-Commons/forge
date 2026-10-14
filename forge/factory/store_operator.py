"""Authenticated operator intents with durable, independently read-back GitHub effects.

Requests fence work before returning. The supervisor executes bounded pending
requests; process restarts reconcile remote state before repeating a write.
"""
import json
import re
import time
import uuid
from urllib.parse import quote

from ..request_metrics import RequestBudgetExhausted
from ..store.delivery_inputs import approved_in
from ..store.models import Command, IdempotencyConflict, TransitionError, VersionConflict
from ..store.queries import Queries
from ..store.runtime import writable
from ..store.transitions import Lifecycle, _id, _json, _unit, _gates, _charges, manifest_hash, project_unit
from ..store.runtime_requirements import RuntimeRequirements
from .github import GitHubUnavailable
from .ownership import ownership
from .store_views import action_revision, available_actions

DELIVERY_ACTIONS = {'drop','dissolve','close_member','resume_delivery'}
ACTIONS = DELIVERY_ACTIONS | {'queue','backlog','pause','resume','retry','recheck','start_anyway','environment','close','assign_reviewer'}
# Decisions the operator UI applies at once and answers with the refreshed issue.
IMMEDIATE = {'queue', 'backlog', 'pause', 'resume', 'retry', 'recheck', 'start_anyway'}
LOGIN = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})')


def reviewer_logins(reason):
    """The decision for assign_reviewer is the chosen logins, nothing else."""
    logins = []
    for value in re.split(r'[\s,;]+', (reason or '').strip()):
        name = value.lstrip('@')
        if not name:
            continue
        if not LOGIN.fullmatch(name):
            raise ValueError('Choose GitHub logins for the reviewers.')
        if name.lower() not in [l.lower() for l in logins]:
            logins.append(name)
    if not 1 <= len(logins) <= 10:
        raise ValueError('Choose between one and ten reviewers.')
    return logins


def labels(issue):
    return {row['name'] if isinstance(row, dict) else row for row in issue.get('labels', [])}


def undo_id(request_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL,'forge:undo-close:'+request_id))


def action_state_in(db,request_id):
    """Project recorded request/effect outcomes; a request ID is never success."""
    row=db.execute('SELECT q.*,a.result_json FROM issue_action_requests q JOIN operator_action_authority a ON a.request_id=q.id WHERE q.id=?',(request_id,)).fetchone()
    if row is None:return None
    undo=db.execute("SELECT id,status,message FROM issue_action_requests WHERE id=? AND action='reopen'",(undo_id(request_id),)).fetchone()
    reversible=False
    if row['action']=='close' and row['status']=='applied' and undo is None:
        effect=db.execute("SELECT receipt_json FROM operator_action_effects WHERE request_id=? AND step='close-issue' AND status='confirmed'",(request_id,)).fetchone()
        issue=db.execute('SELECT github_state,state_reason,version FROM issues WHERE id=?',(row['issue_id'],)).fetchone()
        result=json.loads(row['result_json']) if row['result_json'] else {}
        reversible=bool(effect and json.loads(effect[0]).get('closure_event') and issue['github_state']=='closed'
                        and issue['state_reason']=='not_planned' and issue['version']==result.get('closed_issue_version'))
    return dict(request_id=request_id,status=row['status'],message=row['message'] or '',reversible=reversible,
                undo=dict(request_id=undo['id'],status=undo['status'],message=undo['message'] or '') if undo else None)


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

    @staticmethod
    def _cleanup_pending(db,unit,member_id=None):
        if member_id is None:
            return db.execute('SELECT 1 FROM jobs WHERE unit_id=? AND cleanup=0 LIMIT 1', (unit['id'],)).fetchone() is not None
        from ..store.job_scope import affected_jobs_in
        return any(not job['cleanup'] for job in affected_jobs_in(db, unit, {member_id})['jobs'])

    def _scope_quiet(self,db,unit,member_id):
        if self._cleanup_pending(db,unit,member_id):
            raise TransitionError('Affected workers require verified cleanup before this decision')
        if (db.execute("SELECT 1 FROM merge_requests WHERE unit_id=? AND status IN ('pending','uncertain')",(unit['id'],)).fetchone()
                or db.execute("SELECT 1 FROM external_effects WHERE unit_id=? AND status IN ('pending','running','uncertain')",(unit['id'],)).fetchone()):
            raise TransitionError('External effects must be reconciled before this decision')

    @staticmethod
    def _membership_in(db,issue,unit):
        member_id=issue['lifecycle']['member_id']
        members=[dict(row) for row in db.execute('SELECT m.*,i.github_number FROM unit_issues m JOIN issues i ON i.id=m.issue_id WHERE m.unit_id=? AND m.left_at IS NULL ORDER BY m.id',(unit['id'],))]
        dependent_ids={row[0] for row in db.execute('SELECT member_id FROM member_dependencies WHERE unit_id=? AND prerequisite_member_id=? AND membership_generation=?',(unit['id'],member_id,unit['membership_generation']))}
        dependents=[member for member in members if member['id'] in dependent_ids]
        return {'unit_id':unit['id'],'member_count':len(members),
                'pending_decision':bool(db.execute("SELECT 1 FROM issue_action_requests q JOIN operator_action_authority a ON a.request_id=q.id WHERE a.unit_id=? AND q.status='pending'",(unit['id'],)).fetchone()),
                'unit_paused':any(gate['code'] in {'operator_pause','operator_backlog'} and gate['member_id'] is None for gate in _gates(db,unit['id'])),
                'decision':{'member_id':member_id,'membership_generation':unit['membership_generation'],
                            'dependent_decisions':{member['id']:'wait_external' for member in dependents}},
                'members':[{'member_id':member['id'],'issue_id':member['issue_id'],
                            'number':member['github_number']} for member in members],
                'dependent_members':[member['id'] for member in dependents]}

    def membership(self,key):
        with self.store.read_snapshot() as db:
            issue=Queries._issue(db,key)
            unit_id=issue['lifecycle']['unit_id']
            if not unit_id or not approved_in(db,unit_id):return {'delivery':None}
            return {'delivery':self._membership_in(db,issue,_unit(db,unit_id)),'revision':action_revision(db,key)}

    def _membership_evidence(self,row):
        evidence={'approved':True,'reason':row['reason']}
        candidate=row['candidate']
        if candidate:
            pull=self.github.pr(row['item'],candidate['pull_number'])
            if (pull.get('merged') is not False or (pull.get('head') or {}).get('sha')!=candidate['head_oid']
                    or (pull.get('base') or {}).get('sha')!=candidate['base_oid']):
                raise TransitionError('Candidate changed or merged; membership decision needs reconciliation')
            evidence.update(verified=True,merged=False,candidate_id=candidate['id'],pull_number=candidate['pull_number'],
                            head_oid=candidate['head_oid'],observed_at=self.clock())
        return evidence

    def request(self, key, action, revision, reason, *, request_id=None, environment=None, membership=None):
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
        payload = dict(key=key,action=action,revision=revision,reason=reason.strip(),environment=environment,membership=membership,actor=actor)
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
            if issue.get('body_truncated') or (issue['github_state']!='open' and not (action=='close_member' and issue['state_reason'] in {'completed','not_planned'})) or issue['observed_at'] is None or not 0<=self.clock()-issue['observed_at']<300:
                raise TransitionError('A fresh open issue observation is required')
            if db.execute("SELECT 1 FROM issue_action_requests WHERE issue_id=? AND status='pending'",(key,)).fetchone():
                raise TransitionError('This issue already has a pending decision')
            unit_id=issue['lifecycle']['unit_id']
            unit=_unit(db,unit_id) if unit_id else None
            grouped=bool(unit and approved_in(db,unit_id))
            if unit and db.execute("SELECT 1 FROM issue_action_requests q JOIN operator_action_authority a ON a.request_id=q.id WHERE a.unit_id=? AND q.status='pending' AND q.action IN ('drop','dissolve','close_member','resume_delivery')",(unit_id,)).fetchone():
                raise TransitionError('Finish the pending membership decision before another issue action')
            scoped_member=issue['lifecycle']['member_id'] if grouped and action not in {'retry','assign_reviewer','close'}|DELIVERY_ACTIONS else None
            cleanup=bool(unit and self._cleanup_pending(db,unit,scoped_member))
            allowed=available_actions(issue,unit,cleanup=cleanup)
            if unit and not any(g['code']=='operator_pause' and g['member_id']==scoped_member for g in _gates(db,unit_id)):allowed.append('pause')
            if unit and grouped and action=='backlog':allowed.append('backlog')
            if action in DELIVERY_ACTIONS:
                if not grouped:raise TransitionError('Membership decisions require an approved delivery')
                expected=self._membership_in(db,issue,unit)
                if membership!=expected['decision']:
                    raise VersionConflict('Membership or dependent decisions changed; reload before deciding')
                if action=='resume_delivery' and not expected['unit_paused']:
                    raise TransitionError('This delivery has no unit-wide operator pause')
                if action in {'drop','dissolve'} and expected['member_count']<2:
                    raise TransitionError('Only deliveries with multiple members can be split')
                if db.execute("SELECT 1 FROM issue_action_requests q JOIN operator_action_authority a ON a.request_id=q.id WHERE a.unit_id=? AND q.status='pending'",(unit_id,)).fetchone():
                    raise TransitionError('Finish pending issue decisions before changing membership')
                allowed.append(action)
            elif membership is not None:raise ValueError('Membership pins are only accepted for membership decisions')
            if action=='close':
                if grouped:raise TransitionError('Closing a grouped issue requires an explicit member removal decision')
                allowed.append('close')
            if action=='environment' and unit and not cleanup:allowed.append('environment')
            if action=='start_anyway' and unit and not cleanup and any(g['code']=='dependency' and g['member_id'] in {None,scoped_member} and json.loads(g['details_json']).get('requirements') for g in _gates(db,unit_id)):
                allowed.append('start_anyway')
            if action not in allowed:raise TransitionError('Action is not available for the current issue')
            if action=='environment':
                from .environments import override
                environment=override(environment)
                payload['environment']=environment
            reviewers=[]
            if action=='assign_reviewer':
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
            member=next((value for value in self._members(db,unit_id) if value['issue_id']==key),None)
            if member is None:raise TransitionError('Issue has no current delivery membership')
            if db.execute("SELECT 1 FROM merge_requests WHERE unit_id=? AND status IN ('pending','uncertain')",(unit_id,)).fetchone():
                raise TransitionError('Merge outcome must be reconciled before an operator decision')
            if action=='retry' or action in DELIVERY_ACTIONS:
                from ..store.recovery import pending_mandatory
                if pending_mandatory(db,unit_id):raise TransitionError('Mandatory investigation must finish before retry')
            before=self._lifecycle_summary(project_unit(db,unit_id))
            if action in ({'pause','backlog','close'}|DELIVERY_ACTIONS)-{'resume_delivery'}:
                if scoped_member:self.fence_member_in(db,command,scoped_member,reason='operator_'+action)
                else:self._fence(db,command)
                code='operator_backlog' if action=='backlog' else 'operator_pause'
                if action not in DELIVERY_ACTIONS and not any(g['code']==code and g['member_id']==scoped_member for g in _gates(db,unit_id)):
                    self._open_gate(db,command,code,{'request_id':identifier,'reason':reason.strip()},scoped_member)
            gate=self._open_gate(db,command,'operator_sync',{'request_id':identifier,'action':action},scoped_member)
            ready=config['ready_label']
            add=['hold'] if action=='pause' else [ready] if action in {'queue','resume','retry'} else []
            remove=[ready] if action=='backlog' else list(config['hold_labels']) if action=='resume' else []
            if action=='environment':remove=[v for v in issue['labels'] if v.startswith('env:')]
            plan=dict(membership=membership,member_id=member['id'],scope_member_id=scoped_member,baseline=self._baseline(db,unit_id),requirements=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(issue['current_revision_id'],)).fetchone()[0]),
                labels=issue['labels'],assignees=issue['assignees'],add=add,remove=remove,reviewers=reviewers)
            if action=='recheck':
                from ..store.worker_claims import eligible_gate_in
                plan['worker_claim_gates']=[candidate['id'] for candidate in _gates(db,unit_id)
                    if candidate['id'] and candidate['member_id'] in {None,member['id']} and eligible_gate_in(db,candidate)]
                plan['scheduling_gates']=[candidate['id'] for candidate in _gates(db,unit_id)
                    if candidate['id'] and candidate['code']=='forge_problem' and candidate['member_id'] in {None,member['id']}
                    and json.loads(candidate['details_json']).get('source')=='scheduling']
            if action=='recheck' and issue['lifecycle']['reason_code']=='external_change':
                plan['external_change_gates']=[gate['id'] for gate in _gates(db,unit_id)
                    if gate['code']=='external_change' and gate['id'] and gate['member_id'] in {None,member['id']}]
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
            if row['action']=='reopen' and db.execute('SELECT version FROM issues WHERE id=?',(row['issue_id'],)).fetchone()[0]!=row['plan']['issue_version']:
                raise TransitionError('Issue changed after Undo was requested')
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
        allowed_closed=issue.get('state')=='closed' and (row['action']=='close' and issue.get('state_reason')=='not_planned' or row['action']=='close_member' and issue.get('state_reason') in {'not_planned','completed'})
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
        candidate=row['candidate'] if row['action']=='close' else None
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
        issue=self._remote(row)
        if row['action']=='close_member':
            # Member closure is a composition decision, not a reversible singleton close.
            if issue.get('state')=='closed':return issue
            self._intent(row,'close-issue',{'number':row['item']['number'],'state_reason':'not_planned'})
            self.github.call('controller',row['item'],'PATCH',f"/issues/{row['item']['number']}",{'state':'closed','state_reason':'not_planned'})
            issue=self._remote(row)
            if issue.get('state')!='closed' or issue.get('state_reason')!='not_planned':raise GitHubUnavailable('Issue closure is unconfirmed')
            self._receipt(row,'close-issue',{'closed':True,'state_reason':'not_planned'})
            return issue
        if row['action']=='close' and issue.get('state')=='open':self._comment(row)
        with self.store.read_snapshot() as db:
            effect=db.execute("SELECT * FROM operator_action_effects WHERE request_id=? AND step='close-issue'",(row['id'],)).fetchone()
        transitions=self._issue_transitions(row)
        if effect:
            intent=json.loads(effect['payload_json'])
            if 'previous_event' not in intent:
                raise TransitionError('This older close request has no GitHub event lineage; review its closure on GitHub')
            if effect['status']=='confirmed':
                receipt=json.loads(effect['receipt_json'])
                if not transitions or transitions[-1]!=receipt['closure_event']:
                    raise TransitionError('The original Forge closure changed on GitHub')
                if issue.get('state')!='closed' or issue.get('state_reason')!='not_planned':
                    raise GitHubUnavailable('Issue closure is unconfirmed')
                return issue
        else:
            if issue.get('state')!='open':raise TransitionError('Another action already closed this issue')
            intent={'number':row['item']['number'],'state_reason':'not_planned',
                    'previous_event':transitions[-1]['id'] if transitions else None}
            self._intent(row,'close-issue',intent)
        following=self._following(transitions,intent['previous_event'])
        if not following and issue.get('state')=='open':
            self._intent(row,'close-issue',intent)
            self.github.call('controller',row['item'],'PATCH',f"/issues/{row['item']['number']}",{'state':'closed','state_reason':'not_planned'})
        issue=self._remote(row)
        transitions=self._issue_transitions(row)
        following=self._following(transitions,intent['previous_event'])
        if not following:raise GitHubUnavailable('Issue closure event is unconfirmed')
        if len(following)!=1 or not self._own_transition(following[0],'closed'):
            raise TransitionError('Issue closure does not match this Forge decision')
        if issue.get('state')!='closed' or issue.get('state_reason')!='not_planned':raise GitHubUnavailable('Issue closure is unconfirmed')
        self._receipt(row,'close-issue',{'closed':True,'state_reason':'not_planned','closure_event':following[0]})
        return issue

    def _comment(self,row):
        """The operator's reason as the closing comment, posted once across restarts."""
        body='Closed from Forge as not planned: '+row['reason']
        with self.store.read_snapshot() as db:
            done=db.execute("SELECT status FROM operator_action_effects WHERE request_id=? AND step='close-comment'",(row['id'],)).fetchone()
        if done and done[0]=='confirmed':return
        path=f"/issues/{row['item']['number']}/comments"
        # An unconfirmed earlier write may have landed: never post it twice.
        if not (done and any(c.get('body')==body for c in self.github.pages('controller',row['item'],path))):
            self._intent(row,'close-comment',{'body':body})
            self.github.call('controller',row['item'],'POST',path,{'body':body})
        self._receipt(row,'close-comment',{'posted':True})

    def _issue_transitions(self,row):
        events=self.github.pages('controller',row['item'],f"/issues/{row['item']['number']}/events")
        result=[]
        for event in events:
            if event.get('event') not in {'closed','reopened'}:continue
            actor=event.get('actor') or {}
            if type(event.get('id')) is not int or type(actor.get('id')) is not int or not actor.get('login'):
                raise GitHubUnavailable('Issue event identity is unconfirmed')
            result.append({'id':event['id'],'event':event['event'],
                           'actor':{key:actor.get(key) for key in ('id','login','type')}})
        if len({event['id'] for event in result})!=len(result):raise GitHubUnavailable('Issue event sequence is unconfirmed')
        return result

    @staticmethod
    def _following(events,previous):
        if previous is None:return events
        for index,event in enumerate(events):
            if event['id']==previous:return events[index+1:]
        raise TransitionError('Original issue event is no longer verifiable')

    def _own_transition(self,event,kind):
        return (event['event']==kind and event['actor']['type']=='Bot'
                and event['actor']['login']==self.github.publisher('controller'))

    def reopen(self,key,request_id):
        """Persist one Undo of an exact confirmed closure; the supervisor owns its effect."""
        if not isinstance(self.actor_id,str) or not self.actor_id or len(self.actor_id)>256:
            raise TransitionError('An authenticated operator identity is required')
        identifier=undo_id(request_id)
        with self.store.write() as db:
            writable(db)
            previous=db.execute('SELECT q.issue_id,a.actor_id FROM issue_action_requests q JOIN operator_action_authority a ON a.request_id=q.id WHERE q.id=?',(identifier,)).fetchone()
            if previous:
                if (previous['issue_id'],previous['actor_id'])!=(key,self.actor_id):raise IdempotencyConflict('Undo already belongs to another operator decision')
                return identifier
            original=db.execute('SELECT q.*,a.unit_id FROM issue_action_requests q JOIN operator_action_authority a ON a.request_id=q.id WHERE q.id=?',(request_id,)).fetchone()
            state=action_state_in(db,request_id)
            if not original or original['issue_id']!=key or not state['reversible']:
                raise TransitionError('Only the unchanged, confirmed Forge closure can be undone')
            if db.execute("SELECT 1 FROM issue_action_requests WHERE issue_id=? AND status='pending'",(key,)).fetchone():
                raise TransitionError('Another decision is being applied to this issue')
            issue=Queries._issue(db,key);unit=_unit(db,original['unit_id'])
            effect=db.execute("SELECT receipt_json FROM operator_action_effects WHERE request_id=? AND step='close-issue'",(request_id,)).fetchone()
            plan={'baseline':self._baseline(db,unit['id']),'original_request':request_id,
                  'closure_event':json.loads(effect[0])['closure_event'],'issue_version':issue['version']}
            revision=action_revision(db,key)
            db.execute("INSERT INTO issue_action_requests(id,issue_id,action,expected_revision,status,reason,message,plan_json,created_at) VALUES(?,?,'reopen',?,'pending','Undo the confirmed Forge closure','Reopening requested',?,?)",
                (identifier,key,revision,_json(plan),self.clock()))
            db.execute('INSERT INTO operator_action_authority(request_id,actor_id,unit_id,expected_unit_version,expected_issue_version,payload_digest,updated_at) VALUES(?,?,?,?,?,?,?)',
                (identifier,self.actor_id,unit['id'],unit['version'],issue['version'],manifest_hash({'actor':self.actor_id,'original_request':request_id,'plan':plan}),self.clock()))
            self._event(db,Command(identifier,self.actor_id,unit['id'],unit['version']),'lifecycle_changed',
                        {'operator_request':identifier,'action':'reopen','status':'pending','original_request':request_id},issue_id=key)
        return identifier

    def _completed_close(self,row):
        """Read back an already-issued close before deciding whether a NEW write is allowed."""
        with self.store.read_snapshot() as db:
            effect=db.execute("SELECT * FROM operator_action_effects WHERE request_id=? AND step='close-issue'",(row['id'],)).fetchone()
        if effect is None:return None
        intent=json.loads(effect['payload_json'])
        if 'previous_event' not in intent:
            raise TransitionError('This older close request has no GitHub event lineage; review its closure on GitHub')
        events=self._issue_transitions(row)
        following=self._following(events,intent['previous_event'])
        remote,_=self.github.issue(row['item'])
        if not following:
            if remote.get('state')=='closed':raise GitHubUnavailable('Issue closure event is unconfirmed')
            return None
        if len(following)!=1 or not self._own_transition(following[0],'closed'):
            raise TransitionError('Issue closure does not match this Forge decision')
        if remote.get('number')!=row['item']['number'] or remote.get('state')!='closed' or remote.get('state_reason')!='not_planned':
            raise GitHubUnavailable('Issue closure is unconfirmed')
        if effect['status']=='confirmed' and json.loads(effect['receipt_json'])['closure_event']!=following[0]:
            raise TransitionError('The confirmed Forge closure event changed')
        self._receipt(row,'close-issue',{'closed':True,'state_reason':'not_planned','closure_event':following[0]})
        return remote

    def _acknowledge_close(self,row):
        """A completed effect is historical fact, not permission to overwrite newer lifecycle facts."""
        with self.store.write() as db:
            writable(db)
            current=db.execute('SELECT status FROM issue_action_requests WHERE id=?',(row['id'],)).fetchone()
            if current['status']!='pending':return {'request_id':row['id'],'status':current['status']}
            issue=db.execute('SELECT github_state,state_reason,version FROM issues WHERE id=?',(row['issue_id'],)).fetchone()
            result={'applied':True}
            if issue['github_state']=='closed' and issue['state_reason']=='not_planned':
                result['closed_issue_version']=issue['version']
            db.execute("UPDATE issue_action_requests SET status='applied',message='Operator decision applied' WHERE id=?",(row['id'],))
            db.execute('UPDATE operator_action_authority SET result_json=?,updated_at=? WHERE request_id=?',(_json(result),self.clock(),row['id']))
            db.execute('INSERT INTO operator_observation_requests VALUES(?,?,?) ON CONFLICT(issue_id) DO UPDATE SET actor_id=excluded.actor_id,requested_at=excluded.requested_at',(row['issue_id'],row['actor_id'],self.clock()))
            self._event(db,Command(row['id']+':receipt',row['actor_id'],row['unit_id'],row['unit']['version']),'lifecycle_changed',
                        {'operator_request':row['id'],'action':'close','status':'applied','reconciled_effect':True},issue_id=row['issue_id'])
        return {'request_id':row['id'],'applied':True}

    def _reopen_step(self,row):
        with self.store.read_snapshot() as db:
            effect=db.execute("SELECT * FROM operator_action_effects WHERE request_id=? AND step='reopen-issue'",(row['id'],)).fetchone()
        closure=row['plan']['closure_event']
        events=self._issue_transitions(row)
        original=next((event for event in events if event['id']==closure['id']),None)
        if original!=closure:raise TransitionError('Original Forge closure is no longer verifiable')
        following=self._following(events,closure['id'])
        remote,_=self.github.issue(row['item'])
        if effect is None:
            if following or remote.get('state')!='closed' or remote.get('state_reason')!='not_planned':
                raise TransitionError('The issue was changed after the original Forge closure')
            self._intent(row,'reopen-issue',{'closure_event':closure,'number':row['item']['number']})
        if not following and remote.get('state')=='closed':
            if remote.get('state_reason')!='not_planned':raise TransitionError('The original closure reason changed on GitHub')
            self._intent(row,'reopen-issue',{'closure_event':closure,'number':row['item']['number']})
            self.github.call('controller',row['item'],'PATCH',f"/issues/{row['item']['number']}",{'state':'open'})
        events=self._issue_transitions(row);following=self._following(events,closure['id'])
        remote,_=self.github.issue(row['item'])
        if not following:raise GitHubUnavailable('Reopening event is unconfirmed')
        if len(following)!=1 or not self._own_transition(following[0],'reopened'):
            raise TransitionError('A later GitHub change prevents Undo of the original closure')
        if remote.get('state')!='open':raise GitHubUnavailable('Reopening is unconfirmed')
        if effect is not None and effect['status']=='confirmed' and json.loads(effect['receipt_json'])['reopen_event']!=following[0]:
            raise TransitionError('The confirmed Forge reopening event changed')
        self._receipt(row,'reopen-issue',{'reopened':True,'closure_event':closure,'reopen_event':following[0]})
        with self.store.write() as db:
            writable(db)
            current=db.execute('SELECT status FROM issue_action_requests WHERE id=?',(row['id'],)).fetchone()
            if current['status']!='pending':return {'request_id':row['id'],'status':current['status']}
            db.execute("UPDATE issue_action_requests SET status='applied',message='Issue reopened' WHERE id=?",(row['id'],))
            db.execute('UPDATE operator_action_authority SET result_json=?,updated_at=? WHERE request_id=?',(_json({'reopened':True,'original_request':row['plan']['original_request']}),self.clock(),row['id']))
            db.execute('INSERT INTO operator_observation_requests VALUES(?,?,?) ON CONFLICT(issue_id) DO UPDATE SET actor_id=excluded.actor_id,requested_at=excluded.requested_at',(row['issue_id'],row['actor_id'],self.clock()))
            self._event(db,Command(row['id']+':complete',row['actor_id'],row['unit_id'],row['unit']['version']),'lifecycle_changed',
                        {'operator_request':row['id'],'action':'reopen','status':'applied'},issue_id=row['issue_id'])
        return {'request_id':row['id'],'applied':True}

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

    def _recheck_publication(self,row):
        candidate=row['candidate']
        if candidate is None:return
        pull=self.github.pr(row['item'],candidate['pull_number'])
        head,base=pull.get('head') or {},pull.get('base') or {}
        if (pull.get('number')!=candidate['pull_number'] or head.get('repo',{}).get('id')!=row['item']['repo_id']
                or base.get('repo',{}).get('id')!=row['item']['repo_id'] or head.get('ref')!=candidate['branch']):
            raise TransitionError('Pull request identity changed; restore the original repository and branch before reloading')
        if pull.get('merged') is not False:
            raise TransitionError('Pull request is merged or unverified; its merge must be reconciled before continuing')
        if pull.get('state')!='open':raise TransitionError('Reopen the pull request on GitHub, then reload and continue')
        if pull.get('draft') is not False:raise TransitionError('Mark the pull request ready for review on GitHub, then reload and continue')
        if base.get('ref')!=row['unit']['base_branch']:
            raise TransitionError('Restore the pull request target branch on GitHub, then reload and continue')
        from .contracts import SHA
        if not all(isinstance(value,str) and SHA.fullmatch(value) for value in (head.get('sha'),base.get('sha'))):
            raise GitHubUnavailable('Pull request commits could not be verified')
        if head['sha']==candidate['head_oid']:return
        author={key:(pull.get('user') or {}).get(key) for key in ('id','login','type')}
        with self.store.read_snapshot() as db:
            source=db.execute("SELECT j.id,l.publisher FROM candidate_evidence e JOIN jobs j ON j.attempt_id=e.attempt_id "
                "JOIN job_launches l ON l.job_id=j.id WHERE e.candidate_id=? AND e.kind='publication' AND j.role='build'",
                (candidate['id'],)).fetchone()
            target=db.execute('SELECT target_json FROM unit_publication_targets WHERE unit_id=?',(row['unit_id'],)).fetchone()
        target=json.loads(target[0]) if target else None
        if source is None:raise TransitionError('Original publication ownership needs reconciliation before rebuilding')
        if target and target.get('kind')=='existing':
            owned=(target['pull_number']==candidate['pull_number'] and target.get('ownership_transfer') is True
                   and target.get('author')==author)
        else:owned=(type(author['id']) is int and author['id']>0 and author['type']=='Bot' and author['login']==source['publisher'])
        if not owned:raise TransitionError('Pull request author differs from the approved publisher; ownership needs reconciliation')
        row['publication_job_id']=source['id']
        row['reloaded_publication']=dict(pull_number=candidate['pull_number'],branch=candidate['branch'],
            head_oid=head['sha'],base_oid=base['sha'],author=author)

    def _complete(self,row,issue):
        command=Command(row['id']+':complete:'+str(row['unit']['version']),row['actor_id'],row['unit_id'],row['unit']['version'])
        def change(db,unit):
            if self._baseline(db,unit['id'])!=row['plan']['baseline']:raise TransitionError('Delivery changed while applying the decision')
            action=row['action'];gates=_gates(db,unit['id'])
            if action in {'resume','queue','retry','close'}|DELIVERY_ACTIONS:self._scope_quiet(db,unit,row['plan'].get('scope_member_id'))
            if action=='retry' or action in DELIVERY_ACTIONS:
                from ..store.recovery import pending_mandatory
                if pending_mandatory(db,unit['id']):raise TransitionError('Mandatory investigation must finish before retry')
                if _charges(db,unit):
                    db.execute('UPDATE units SET budget_generation=budget_generation+1 WHERE id=?',(unit['id'],))
                    db.execute("UPDATE unit_issues SET state='pending' WHERE unit_id=? AND left_at IS NULL AND state='failed'",(unit['id'],))
                    self._event(db,command,'budget_reset',{'before':unit['budget_generation'],'after':unit['budget_generation']+1,'reason':row['reason']})
                db.execute('INSERT INTO unit_context(unit_id,retry_plan_json) VALUES(?,?) ON CONFLICT(unit_id) DO UPDATE SET retry_plan_json=excluded.retry_plan_json',
                    (unit['id'],_json({'reason':row['reason'],'actor_id':row['actor_id'],'request_id':row['id']})))
            requirements=RuntimeRequirements(self.store,clock=self.clock)
            selected=next(member for member in row['plan']['baseline']['members'] if member['issue_id']==row['issue_id'])
            revision=selected['requirements_revision_id']
            if action=='environment':requirements._set_override_in(db,command,unit,revision,row['environment'])
            if action=='start_anyway':requirements._clear_declared_dependencies_in(db,command,unit,revision,{'reason':row['reason'],'actor_id':row['actor_id']})
            if action=='assign_reviewer':
                db.execute('INSERT INTO candidate_reviewer_requests VALUES(?,?,?,?,?,0)',(unit['current_candidate_id'],row['id'],row['actor_id'],_json(row['plan']['reviewers']),self.clock()))
            if row.get('reloaded_publication'):
                self._scope_quiet(db,unit,None)
                self._invalidate(db,command,unit,'candidate_changed',{'verified':True,'operator_request':row['id'],
                    'publication':row['reloaded_publication'],'source_job_id':row['publication_job_id']})
                for gate in gates:
                    if gate['id'] in row['plan']['external_change_gates']:
                        self._resolve(db,command,gate['id'],{'operator_request':row['id'],'candidate_invalidated':row['candidate']['id']})
            resolve={'operator_sync'}
            if action=='resume_delivery':resolve|={'operator_pause','operator_backlog'}
            if action in {'queue','resume'}:resolve|={'operator_pause','operator_backlog','label_hold'}
            if action=='retry':resolve|={'budget_exhausted','approach_required','decision_required'}
            for gate in gates:
                scope=row['plan'].get('scope_member_id')
                if action=='resume_delivery' and gate['member_id'] is not None:continue
                if scope is not None and gate['member_id']!=scope:continue
                if scope is None and gate['member_id'] is not None and action not in {'retry','close'}|DELIVERY_ACTIONS:continue
                if gate['code'] in resolve:
                    if gate['code']=='operator_sync' and gate['id']!=row['gate_id']:raise TransitionError('Operator intent changed')
                    self._resolve(db,command,gate['id'],{'operator':row['actor_id'],'request_id':row['id'],'verified':True,'observed_at':self.clock()})
            # Fresh remote state is recorded with the same transaction as the
            # local release. Requirement changes were independently rejected.
            db.execute('UPDATE issues SET labels_json=?,assignees_json=?,observed_at=?,version=version+1 WHERE id=?',
                (_json(sorted(labels(issue))),_json([u['login'] if isinstance(u,dict) else u for u in issue.get('assignees',[])]),self.clock(),row['issue_id']))
            if action in {'close','close_member'}:
                db.execute("UPDATE issues SET github_state='closed',state_reason=? WHERE id=?",(issue['state_reason'],row['issue_id']))
                if action=='close':self._settle(db,command,unit,'closed',close_code='github_not_planned')
            if action in DELIVERY_ACTIONS:
                from ..store.delivery import Delivery
                authority=Delivery(self.store,clock=self.clock)
                evidence=row['membership_evidence']
                if action=='resume_delivery':result={'resumed':True}
                elif action=='dissolve':
                    result=authority.dissolve_in(db,command,unit,evidence=evidence)
                else:
                    result=authority.drop_member_in(db,command,unit,**row['plan']['membership'],evidence=evidence,disposition='close' if action=='close_member' else 'release')
            else:result={'applied':True}
            if action=='close':result['closed_issue_version']=db.execute('SELECT version FROM issues WHERE id=?',(row['issue_id'],)).fetchone()[0]
            db.execute("UPDATE issue_action_requests SET status='applied',message='Operator decision applied' WHERE id=?",(row['id'],))
            db.execute('UPDATE operator_action_authority SET result_json=?,updated_at=? WHERE request_id=?',(_json({'applied':True,**result}),self.clock(),row['id']))
            if action=='recheck':
                from ..store.worker_claims import WorkerClaims
                WorkerClaims(self.store,clock=self.clock).recheck_in(db,command,unit,
                    row['plan'].get('worker_claim_gates',[]),member_id=selected['id'],explicit=True)
                from ..store.scheduling import Scheduling
                for scheduling_gate in row['plan'].get('scheduling_gates',[]):
                    Scheduling(self.store,clock=self.clock).recheck_in(db,command,unit,scheduling_gate,explicit=True)
                db.execute('INSERT INTO operator_observation_requests VALUES(?,?,?) ON CONFLICT(issue_id) DO UPDATE SET actor_id=excluded.actor_id,requested_at=excluded.requested_at',(row['issue_id'],row['actor_id'],self.clock()))
            return {'request_id':row['id'],'applied':True,**result}
        return self._apply(command,'complete_operator_action',{'request_id':row['id']},change)

    def _reject(self,row,message):
        with self.store.write() as db:
            command=Command(row['id']+':reject',row['actor_id'],row['unit_id'],_unit(db,row['unit_id'])['version'])
            gate=db.execute('SELECT * FROM unit_gates WHERE id=? AND resolved_at IS NULL',(row['gate_id'],)).fetchone()
            if gate:
                self._resolve(db,command,gate['id'],{'rejected':True})
                self._save(db,command,'reject_operator_action',{'request_id':row['id']},{'rejected':True})
            if row['action']=='reopen':
                self._event(db,command,'lifecycle_changed',{'operator_request':row['id'],'action':'reopen','status':'rejected','reason':message},issue_id=row['issue_id'])
            db.execute("UPDATE issue_action_requests SET status='rejected',message=? WHERE id=?",(message,row['id']))
            db.execute('UPDATE operator_action_authority SET result_json=?,updated_at=? WHERE request_id=?',(_json({'rejected':True,'reason':message}),self.clock(),row['id']))
            db.execute('INSERT INTO operator_observation_requests VALUES(?,?,?) ON CONFLICT(issue_id) DO UPDATE SET actor_id=excluded.actor_id,requested_at=excluded.requested_at',(row['issue_id'],row['actor_id'],self.clock()))
        return {'request_id':row['id'],'rejected':True}

    def step(self,identifier):
        row=self._request(identifier)
        if row['status']!='pending':return {'request_id':identifier,'status':row['status']}
        try:
            if row['action']=='reopen':return self._reopen_step(row)
            if row['action']=='close':
                completed=self._completed_close(row)
                if completed is not None:
                    row=self._request(identifier)
                    if row['current']:return self._complete(row,completed)
                    return self._acknowledge_close(row)
            if not row['current']:raise TransitionError('Delivery changed while applying the decision')
            if row['action'] in {'resume','queue','retry','close'}|DELIVERY_ACTIONS:
                with self.store.read_snapshot() as db:
                    if row['action']=='retry' or row['action'] in DELIVERY_ACTIONS:
                        from ..store.recovery import pending_mandatory
                        if pending_mandatory(db,row['unit_id']):return {'request_id':identifier,'pending':'recovery'}
                    if self._cleanup_pending(db,_unit(db,row['unit_id']),row['plan'].get('scope_member_id')):
                        return {'request_id':identifier,'pending':'cleanup'}
            if row['action'] in DELIVERY_ACTIONS:
                self._membership_evidence(row)  # Reject merged or changed candidates before any GitHub closure.
            issue=self._labels(row)
            if row['action'] in {'close','close_member'}:issue=self._close(row)
            if row['action']=='assign_reviewer':issue=self._reviewers(row)
            row=self._request(identifier)
            if row['plan'].get('external_change_gates'):
                self._recheck_publication(row)
            if row['action'] in DELIVERY_ACTIONS:row['membership_evidence']=self._membership_evidence(row)
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
            ids=[r[0] for r in db.execute("SELECT q.id FROM issue_action_requests q JOIN operator_action_authority a ON a.request_id=q.id WHERE q.status='pending' AND q.id>? ORDER BY q.id LIMIT ?",(after,limit+1))]
        items=[]
        for identifier in ids[:limit]:
            try:items.append(self.step(identifier))
            except RequestBudgetExhausted:
                items.append({'request_id':identifier,'deferred':'github_budget'})
        deferred=any(row.get('deferred')=='github_budget' for row in items)
        return {'items':items,'next_cursor':after if deferred else ids[limit-1] if len(ids)>limit else None,
                'deferred':'github_budget' if deferred else None}

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
            job=db.execute('SELECT unit_id,member_id,cleanup,role FROM jobs WHERE id=?',(job_id,)).fetchone()
            if job is None:raise TransitionError('Unknown execution')
            unit=_unit(db,job['unit_id'])
        command=Command(identifier,self.actor_id,unit['id'],unit['version'])
        def change(db,current):
            task = db.execute('SELECT 1 FROM job_tasks WHERE job_id=?', (job_id,)).fetchone()
            if approved_in(db,current['id']) and (task or job['role']=='recovery'):
                scopes = [row[0] for row in db.execute('SELECT member_id FROM job_members WHERE job_id=? ORDER BY member_id', (job_id,))]
                if not scopes:
                    raise TransitionError('Execution has no recorded member scope')
            elif approved_in(db,current['id']) and job['member_id'] is not None and job['role']=='build':
                scopes = [job['member_id']]
            else:
                scopes = [None]  # Planning, integration, Review and Audit own the full delivery.
            for scope in scopes:
                if current['state'] not in {'merged','closed','dissolved'} and not any(g['code']=='operator_pause' and g['member_id']==scope for g in _gates(db,current['id'])):
                    self._open_gate(db,command,'operator_pause',{'reason':reason.strip(),'job_id':job_id},scope)
                if scope is not None:
                    self.fence_member_in(db,command,scope,reason='operator_'+action)
                else:
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
    """Build HTTP/CLI services over the selected store."""
    from ..store.runtime import required
    required(root)
    from .sqlite_runtime import operator_actions
    return operator_actions(root,actor_id)
