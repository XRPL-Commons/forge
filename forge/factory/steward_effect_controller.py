"""Reconcile operator-approved issue mutations through the existing outbox.

POST requests cannot be made idempotent by a client marker. After a lost reply,
read all bounded pages for the exact marker and never blindly repeat the POST.
"""
import json
import time

from ..store.effects import Effects, admit_send_in, defer_effect_in, effect_due, retry_policy
from ..store.models import Command, TransitionError
from ..store.runtime import writable
from ..store.steward_effects import current_in, row_in
from ..store.transitions import Lifecycle, _json, _unit
from .github import GitHubUnavailable


class StewardEffectController(Lifecycle):
    def __init__(self,store,github,*,config,admission=lambda item: True,clock=time.time):
        super().__init__(store,clock=clock);self.github=github;self.config=config;self.admission=admission

    def reliability(self):
        value=self.config() if callable(self.config) else self.config
        return value.get('reliability',{})

    def _row(self,identifier):
        with self.store.read_snapshot() as db:return row_in(db,identifier)

    def _item(self,member):
        return {'repo_id':member['repository_id'],'repo':member['repo'],'number':member['number'],'key':member['issue_id']}

    def _requests(self,row):
        step=row['payload'];choice=row['choice'];parameters=choice['parameters'];scope=row['scope']
        marker='<!-- forge-steward:'+row['id']+' -->'
        references=', '.join(member['repo']+'#'+str(member['number']) for member in scope)
        if step['operation']=='create':
            child=step['child'];body=child['body']+'\n\nAcceptance criteria:\n'+'\n'.join('- '+v for v in child['acceptance_criteria'])
            body+='\n\nIntent preserved: '+parameters['intent_note']+'\nSource issues: '+references+'\n\n'+marker
            return self._item(scope[0]),'POST','/issues',{'title':child['title'],'body':body},marker
        if step['operation']=='target_comment':
            body='Continue the intent from '+references+':\n\n'+parameters['intent_note']+'\n\n'+marker
            return step['item'],'POST','/issues/'+str(step['item']['number'])+'/comments',{'body':body},marker
        member=next(m for m in scope if m['member_id']==step.get('member_id'))
        item=self._item(member);path='/issues/'+str(member['number'])
        if step['operation']=='close':return item,'PATCH',path,{'state':'closed','state_reason':'not_planned'},marker
        if choice['action']=='question':body=parameters['text']
        else:
            body=parameters['intent_note']
            if choice['action']=='fold':body+='\n\nContinue in repository '+str(parameters['repository_id'])+' issue #'+str(parameters['issue_number'])+'.'
            if choice['action']=='split':
                with self.store.read_snapshot() as db:
                    children=[json.loads(r[0])['number'] for r in db.execute("SELECT e.receipt_json FROM external_effects e JOIN steward_issue_effects s ON s.effect_id=e.id WHERE s.decision_id=? AND e.status='confirmed' AND json_extract(e.payload_json,'$.operation')='create' ORDER BY s.position",(row['decision_id'],))]
                body+='\n\nContinue in: '+', '.join(scope[0]['repo']+'#'+str(number) for number in children)
        body+='\n\nSource scope: '+references+'\n\n'+marker
        return item,'POST',path+'/comments',{'body':body},marker

    def _readback(self,row,item,method,path,payload,marker):
        if method=='PATCH':
            issue,_=self.github.issue(item)
            if issue.get('number')==item['number'] and issue.get('state')=='closed' and issue.get('state_reason')=='not_planned':
                return {'number':item['number'],'state':'closed','state_reason':'not_planned','verified':True,'observed_at':self.clock()}
            return None
        listing='/issues?state=all&sort=created&direction=desc' if row['payload']['operation']=='create' else path
        author=self.github.publisher('controller')
        matches=[value for value in self.github.pages('controller',item,listing)
            if value.get('body')==payload['body'] and (value.get('user') or {}).get('login')==author
            and (value.get('user') or {}).get('type')=='Bot'
            and ('title' not in payload or value.get('title')==payload['title'] and not value.get('pull_request'))]
        if len(matches)>1:raise TransitionError('Multiple remote objects match this immutable step')
        if not matches:return None
        value=matches[0]
        return {'id':value['id'],'number':value.get('number',item.get('number')),'verified':True,'observed_at':self.clock()}

    def _remote_scope(self,row):
        """Fresh requirements, labels and assignees precede each actual write."""
        candidate=row['dossier']['authority']['candidate']
        if candidate:
            with self.store.read_snapshot() as db:
                number=db.execute('SELECT pull_number FROM candidates WHERE id=?',(candidate['id'],)).fetchone()[0]
            pull=self.github.call('controller',self._item(row['scope'][0]),'GET','/pulls/'+str(number))
            if (pull.get('merged') is not False or (pull.get('head') or {}).get('sha')!=candidate['head_oid']
                    or (pull.get('base') or {}).get('sha')!=candidate['base_oid']):
                raise TransitionError('Candidate changed before issue mutation')
        with self.store.read_snapshot() as db:
            expected={member['member_id']:json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',
                         (member['requirements_revision_id'],)).fetchone()[0]) for member in row['scope']}
            closed={json.loads(r[0])['member_id'] for r in db.execute("SELECT e.payload_json FROM external_effects e JOIN steward_issue_effects s ON s.effect_id=e.id WHERE s.decision_id=? AND e.status='confirmed' AND json_extract(e.payload_json,'$.operation')='close'",(row['decision_id'],))}
        for member in row['scope']:
            issue,requirements=self.github.issue(self._item(member))
            labels=sorted(value['name'] if isinstance(value,dict) else value for value in issue.get('labels',[]))
            assignees=sorted(value['login'] if isinstance(value,dict) else value for value in issue.get('assignees',[]))
            assignee_types={value['login']:value['type'] for value in issue.get('assignees',[])
                            if isinstance(value,dict) and value.get('type')}
            desired='closed' if member['member_id'] in closed else 'open'
            if (issue.get('number')!=member['number'] or requirements!=expected[member['member_id']]
                    or issue.get('state')!=desired or labels!=sorted(json.loads(member['labels_json']))
                    or assignees!=sorted(json.loads(member['assignees_json']))
                    or assignee_types!=json.loads(member['assignee_types_json'])):
                raise TransitionError('Source issue requirements, ownership, labels or state changed')
        if row['choice']['action']=='fold':
            parameters=row['choice']['parameters'];target=next(v for v in row['dossier']['facts']['observations']['issues']
                if v['repository_id']==parameters['repository_id'] and v['issue_number']==parameters['issue_number'])
            with self.store.read_snapshot() as db:repo=db.execute('SELECT full_name FROM repositories WHERE id=?',(target['repository_id'],)).fetchone()[0]
            issue,requirements=self.github.issue({'repo_id':target['repository_id'],'repo':repo,'number':target['issue_number']})
            if issue.get('number')!=target['issue_number'] or issue.get('state')!='open' or requirements!=target['revision']:
                raise TransitionError('Fold target changed')

    def _defer(self,row,reason):
        with self.store.write() as db:
            current=db.execute('SELECT * FROM external_effects WHERE id=?',(row['id'],)).fetchone()
            if current['status']=='confirmed':
                # Remote confirmation is final. A blocked local completion
                # retains that receipt and yields to other outbox work.
                db.execute('UPDATE external_effects SET retry_at=? WHERE id=?',
                    (self.clock()+retry_policy(self.reliability())['retry_cap'],row['id']))
            else:
                defer_effect_in(db,current,self.clock(),reliability=self.reliability())
        Effects(self.store,clock=self.clock,**retry_policy(self.reliability())).bounded(row['id'])
        return {'effect_id':row['id'],'status':'pending','reason':reason}

    def _confirm(self,row,receipt):
        with self.store.read_snapshot() as db:unit=_unit(db,row['unit_id'])
        command=Command(row['id']+':confirmed',row['actor_id'],unit['id'],unit['version'])
        def confirm(db,unit):
            db.execute("UPDATE external_effects SET status='confirmed',receipt_json=?,retry_at=NULL,updated_at=? WHERE id=? AND status IN('pending','running','uncertain')",
                       (_json(receipt),self.clock(),row['id']))
            for gate in db.execute("SELECT id FROM unit_gates WHERE unit_id=? AND code='forge_problem' AND resolved_at IS NULL "
                "AND json_extract(details_json,'$.source')='scheduling' AND json_extract(details_json,'$.effect_id')=?",(unit['id'],row['id'])).fetchall():
                self._resolve(db,command,gate['id'],receipt)
            self._event(db,command,'external_effect_changed',{'effect_id':row['id'],'status':'confirmed'})
            return {'effect_id':row['id'],'status':'confirmed'}
        self._apply(command,'confirm_steward_issue_step',{'effect_id':row['id'],'receipt':receipt},confirm)
        self._finish_group(row['decision_id'])
        return {'effect_id':row['id'],'status':'confirmed'}

    def _finish_group(self,decision_id):
        with self.store.read_snapshot() as db:
            group=db.execute('SELECT completed_at FROM steward_effect_groups WHERE decision_id=?',(decision_id,)).fetchone()
            rows=db.execute('SELECT e.id,e.status FROM external_effects e JOIN steward_issue_effects s ON s.effect_id=e.id WHERE s.decision_id=? ORDER BY s.position',(decision_id,)).fetchall()
            if group['completed_at'] is not None or any(r['status']!='confirmed' for r in rows):return
            row=row_in(db,rows[-1]['id']);unit=_unit(db,row['unit_id'])
        # Candidate retirement needs independent fresh proof; an issue closure
        # never claims that its current PR was merged or may be abandoned blindly.
        evidence=None
        candidate=row['dossier']['authority']['candidate']
        if candidate and row['choice']['action'] in {'split','drop','fold'}:
            with self.store.read_snapshot() as db:pr=db.execute('SELECT pull_number FROM candidates WHERE id=?',(candidate['id'],)).fetchone()[0]
            observed=self.github.call('controller',self._item(row['scope'][0]),'GET','/pulls/'+str(pr))
            if (observed.get('merged') is not False or (observed.get('head') or {}).get('sha')!=candidate['head_oid']
                    or (observed.get('base') or {}).get('sha')!=candidate['base_oid']):
                raise TransitionError('Candidate changed before issue action completion')
            evidence={'candidate_id':candidate['id'],'head_oid':candidate['head_oid'],'pull_number':pr,'merged':False,'verified':True,'observed_at':self.clock()}
        command=Command(decision_id+':issues-complete',row['actor_id'],unit['id'],unit['version'])
        def finish(db,unit):
            current_in(db,row)
            if row['choice']['action'] in {'split','drop','fold'}:
                self._quiescent(db,unit['id'])
                self._prove_not_merged(db,unit,evidence or {})
                for member in row['scope']:
                    db.execute("UPDATE issues SET github_state='closed',state_reason='not_planned',observed_at=?,version=version+CASE WHEN github_state!='closed' THEN 1 ELSE 0 END WHERE id=?",(self.clock(),member['issue_id']))
                self._settle(db,command,unit,'closed',close_code='github_not_planned')
            else:
                # A posted question keeps the original obligation until a new
                # observation gives a useful answer; posting is not resolution.
                self._event(db,command,'decision_applied',{'decision_id':decision_id})
            db.execute('UPDATE steward_effect_groups SET completed_at=? WHERE decision_id=?',(self.clock(),decision_id))
            return {'decision_id':decision_id,'remote_confirmed':True}
        self._apply(command,'complete_steward_issue_action',{'decision_id':decision_id},finish)

    def step(self,identifier):
        row=self._row(identifier)
        if row['status'] in {'failed','superseded'}:return {'effect_id':identifier,'status':row['status']}
        if not effect_due(row,self.clock()):return {'effect_id':identifier,'status':'pending','reason':'retry_delay','retry_at':row['retry_at']}
        if row['status']=='confirmed':
            try:
                self._finish_group(row['decision_id']);return {'effect_id':identifier,'status':'confirmed'}
            except (GitHubUnavailable,TransitionError) as error:
                return self._defer(row,str(error))
        with self.store.read_snapshot() as db:
            pending=db.execute("SELECT 1 FROM steward_issue_effects s JOIN external_effects e ON e.id=s.effect_id WHERE s.decision_id=? AND s.position<? AND e.status!='confirmed'",(row['decision_id'],row['position'])).fetchone()
            if pending:return {'effect_id':identifier,'status':'pending','reason':'earlier_step'}
        if row['payload']['operation']=='report_wait':
            with self.store.write() as db:
                writable(db);db.execute("UPDATE external_effects SET retry_at=?,updated_at=? WHERE id=?",
                    (self.clock()+retry_policy(self.reliability())['retry_cap'],self.clock(),identifier))
            return {'effect_id':identifier,'status':'pending','reason':'operator_report_draft'}
        try:
            item,method,path,payload,marker=self._requests(row)
            found=self._readback(row,item,method,path,payload,marker)
            if found:return self._confirm(row,found)
            if method=='POST' and row['attempts']:
                return self._defer(row,'post_outcome_unconfirmed')
            config=self.config() if callable(self.config) else self.config
            if not config.get('enabled') or not all(self.admission(self._item(member)) for member in row['scope']):
                return self._defer(row,'runtime_or_repository_disabled')
            self._remote_scope(row)
            with self.store.write() as db:
                writable(db);current_in(db,row)
                current=db.execute('SELECT * FROM external_effects WHERE id=?',(identifier,)).fetchone()
                admit_send_in(current,self.clock(),reliability=self.reliability())
                policy=retry_policy(self.reliability());delay=min(policy['retry_cap'],policy['retry_seconds']*2**min(current['attempts'],20))
                db.execute("UPDATE external_effects SET status='uncertain',attempts=attempts+1,uncertain_since=COALESCE(uncertain_since,?),retry_at=?,updated_at=? WHERE id=?",
                           (self.clock(),self.clock()+delay,self.clock(),identifier))
            self.github.call('controller',item,method,path,payload)
            found=self._readback(row,item,method,path,payload,marker)
            if found:return self._confirm(row,found)
            return self._defer(row,'readback_unconfirmed')
        except GitHubUnavailable:
            return self._defer(row,'github_unavailable')
        except TransitionError as error:
            return self._defer(row,str(error))

    def cycle(self,*,after=None,limit=50):
        with self.store.read_snapshot() as db:
            rows=db.execute("SELECT e.id FROM external_effects e JOIN steward_issue_effects s ON s.effect_id=e.id JOIN steward_effect_groups g ON g.decision_id=s.decision_id WHERE g.completed_at IS NULL AND e.status IN('pending','running','uncertain','confirmed') AND (e.retry_at IS NULL OR e.retry_at<=?) AND (? IS NULL OR e.id>?) ORDER BY e.id LIMIT ?",
                            (self.clock(),after,after,limit+1)).fetchall()
        return {'items':[self.step(row[0]) for row in rows[:limit]],'next_cursor':rows[limit-1][0] if len(rows)>limit else None}
