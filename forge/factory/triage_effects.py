"""Execute accepted issue metadata decisions using fresh GitHub readback.

A transport success is not confirmation. Lost responses are reconciled from the
issue's current state before another idempotent label/closure write is attempted.
"""
import json
import time
from urllib.parse import quote

from ..store.models import Command, TransitionError
from ..store.runtime import writable
from ..store.transitions import Lifecycle, _unit, _gates, _json
from ..store.effects import Effects, admit_send_in, defer_effect_in, effect_due, retry_policy
from .github import GitHubUnavailable
from .store_operator import labels


class EffectRetryPending(Exception):
    """The effect retains its authority while its retry or reconciliation is due."""


class TriageEffects(Lifecycle):
    def __init__(self,store,github,*,config,clock=time.time):
        super().__init__(store,clock=clock)
        self.github=github;self.config=config

    def _reliability(self):
        config = self.config() if callable(self.config) else self.config
        return config.get('reliability', {})

    def _defer_effect(self, identifier):
        with self.store.write() as db:
            row = db.execute('SELECT * FROM external_effects WHERE id=?', (identifier,)).fetchone()
            defer_effect_in(db, row, self.clock(), reliability=self._reliability())
        return Effects(self.store, clock=self.clock, **retry_policy(self._reliability())).bounded(identifier)

    def _row(self,identifier):
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT e.*,a.issue_id,a.requirements_revision_id,a.actor_id,a.gate_id,a.member_id,i.github_number,r.full_name AS repository '
                'FROM external_effects e JOIN triage_effect_authority a ON a.effect_id=e.id JOIN issues i ON i.id=a.issue_id '
                'JOIN repositories r ON r.id=i.repository_id WHERE e.id=?',(identifier,)).fetchone()
            if row is None:raise TransitionError('Unknown triage effect')
            result=dict(row);result['payload']=json.loads(row['payload_json']);result['unit']=_unit(db,row['unit_id'])
            result['requirements']=json.loads(db.execute('SELECT requirements_json FROM issue_revisions WHERE id=?',(row['requirements_revision_id'],)).fetchone()[0])
            result['item']={'repo_id':result['unit']['repository_id'],'repo':row['repository'],'number':row['github_number'],'key':row['issue_id']}
            return result

    @staticmethod
    def _current(db,row):
        unit=_unit(db,row['unit_id']);payload=row['payload']
        issue=db.execute('SELECT current_revision_id FROM issues WHERE id=?',(row['issue_id'],)).fetchone()
        gate=db.execute('SELECT resolved_at FROM unit_gates WHERE id=?',(row['gate_id'],)).fetchone()
        member=db.execute('SELECT * FROM unit_issues WHERE id=?',(row['member_id'],)).fetchone()
        if (not gate or gate['resolved_at'] is not None or issue['current_revision_id']!=row['requirements_revision_id']
                or not member or member['left_at'] is not None or member['issue_id']!=row['issue_id']
                or member['unit_id']!=unit['id'] or member['requirements_revision_id']!=row['requirements_revision_id']
                or unit['state'] in {'merged','closed','dissolved'}):
            raise TransitionError('Accepted triage authority changed; review a fresh proposal')
        from ..store.job_scope import affected_jobs_in
        members = {item[0] for item in db.execute('SELECT member_id FROM triage_effect_members WHERE effect_id=?', (row['id'],))}
        if any(not job['cleanup'] for job in affected_jobs_in(db, unit, members)['jobs']):
            return False
        return True

    def _remote(self,row):
        issue,requirements=self.github.issue(row['item'])
        payload=row['payload'];changed=set(payload['add'])|set(payload['remove'])
        people={v['login'] if isinstance(v,dict) else v for v in issue.get('assignees',[])}
        closed=payload['action']=='close' and issue.get('state')=='closed' and issue.get('state_reason')=='completed'
        if (issue.get('number')!=row['github_number'] or requirements!=row['requirements']
                or issue.get('state')!='open' and not closed
                or people!=set(payload['assignees'])
                or labels(issue)-changed != set(payload['labels'])-changed):
            raise TransitionError('GitHub requirements, ownership or labels changed; review a fresh proposal')
        return issue

    def _send(self,row,method,path,payload=None):
        with self.store.write() as db:
            writable(db)
            if not self._current(db,row):raise TransitionError('Worker cleanup has not finished')
            current = db.execute('SELECT * FROM external_effects WHERE id=?', (row['id'],)).fetchone()
            first_send = not row.get('_attempt_started')
            if first_send:
                try:
                    admit_send_in(current, self.clock(), reliability=self._reliability())
                except TransitionError:
                    raise EffectRetryPending() from None
            # A relabel action may need several confirmed HTTP writes. They form
            # one attempt; a later step resumes from fresh readback before retry.
            policy = retry_policy(self._reliability())
            retry_at = (self.clock() + min(policy['retry_cap'], policy['retry_seconds'] * 2 ** min(current['attempts'], 20))
                        if first_send else current['retry_at'])
            db.execute("UPDATE external_effects SET status='uncertain',attempts=attempts+?,"
                       'uncertain_since=COALESCE(uncertain_since,?),retry_at=?,updated_at=? WHERE id=?',
                       (int(first_send), self.clock(), retry_at, self.clock(), row['id']))
            row['_attempt_started'] = True
        self.github.call('controller',row['item'],method,path,payload)

    def step(self,identifier):
        row=self._row(identifier)
        if row['status'] in {'confirmed','failed','superseded'}:return {'effect_id':identifier,'status':row['status']}
        if not effect_due(row, self.clock()):
            return {'effect_id': identifier, 'status': row['status'], 'reason': 'retry_delay', 'retry_at': row['retry_at']}
        try:
            with self.store.read_snapshot() as db:
                if not self._current(db,row):return {'effect_id':identifier,'status':'pending','reason':'cleanup'}
            issue=self._remote(row);path='/issues/'+str(row['github_number'])
            for label in row['payload']['add']:
                if label not in labels(issue):self._send(row,'POST',path+'/labels',{'labels':[label]})
                issue=self._remote(row)
                if label not in labels(issue):raise GitHubUnavailable('Label addition is not confirmed')
            for label in row['payload']['remove']:
                if label in labels(issue):self._send(row,'DELETE',path+'/labels/'+quote(label,safe=''))
                issue=self._remote(row)
                if label in labels(issue):raise GitHubUnavailable('Label removal is not confirmed')
            if row['payload']['action']=='close':
                if issue.get('state')=='open':self._send(row,'PATCH',path,{'state':'closed','state_reason':'completed'})
                issue=self._remote(row)
                if issue.get('state')!='closed' or issue.get('state_reason')!='completed':raise GitHubUnavailable('Completed closure is not confirmed')
            return self._complete(row,issue)
        except (GitHubUnavailable, EffectRetryPending) as error:
            bounded = self._defer_effect(identifier)
            return {'effect_id':identifier,'status':'pending',
                    'reason':'effect_reconciliation' if bounded else 'retry_delay' if isinstance(error, EffectRetryPending) else 'github_unavailable'}
        except TransitionError as error:
            return self._reject(row,str(error))

    def _complete(self,row,issue):
        with self.store.write() as db:
            writable(db)
            if not self._current(db,row):raise TransitionError('Cleanup has not finished')
            unit=_unit(db,row['unit_id']);command=Command(row['id']+':complete',row['actor_id'],unit['id'],unit['version'])
            config=self.config() if callable(self.config) else self.config
            current_labels=sorted(labels(issue))
            member=db.execute('SELECT id FROM unit_issues WHERE unit_id=? AND issue_id=? AND left_at IS NULL',(unit['id'],row['issue_id'])).fetchone()
            if set(config['hold_labels']).intersection(current_labels) and not any(g['code']=='label_hold' and g['member_id']==member[0] for g in _gates(db,unit['id'])):
                self._open_gate(db,command,'label_hold',{'source':'github_labels','issue_id':row['issue_id']},member[0])
            if (row['payload']['action']=='relabel' and config['ready_label'] in row['payload']['remove']
                    and not any(g['code']=='operator_backlog' and g['member_id']==member[0] for g in _gates(db,unit['id']))):
                self._open_gate(db,command,'operator_backlog',{'reason':'Issue was explicitly backlogged by triage'},member[0])
            receipt={'verified':True,'observed_at':self.clock(),'state':issue['state'],'state_reason':issue.get('state_reason'),
                     'labels':current_labels,'issue_version':db.execute('SELECT version FROM issues WHERE id=?',(row['issue_id'],)).fetchone()[0]+1}
            self._resolve(db,command,row['gate_id'],receipt)
            db.execute('UPDATE issues SET labels_json=?,github_state=?,state_reason=?,observed_at=?,version=version+1 WHERE id=?',
                (_json(current_labels),issue['state'],issue.get('state_reason'),self.clock(),row['issue_id']))
            db.execute('INSERT INTO operator_observation_requests VALUES(?,?,?) ON CONFLICT(issue_id) DO UPDATE SET '
                'actor_id=excluded.actor_id,requested_at=excluded.requested_at',(row['issue_id'],row['actor_id'],self.clock()))
            db.execute("UPDATE external_effects SET status='confirmed',receipt_json=?,retry_at=NULL,updated_at=? WHERE id=?",
                (_json(receipt),self.clock(),row['id']))
            self._save(db,command,'triage_effect_confirmed',{'effect_id':row['id']},{'effect_id':row['id'],'confirmed':True})
        return {'effect_id':row['id'],'status':'confirmed'}

    def _reject(self,row,reason):
        with self.store.write() as db:
            writable(db)
            unit=_unit(db,row['unit_id']);command=Command(row['id']+':reject',row['actor_id'],unit['id'],unit['version'])
            active=db.execute('SELECT 1 FROM unit_gates WHERE id=? AND resolved_at IS NULL',(row['gate_id'],)).fetchone()
            if active:
                self._resolve(db,command,row['gate_id'],{'rejected':True,'reason':reason})
                if unit['state'] not in {'merged','closed','dissolved'}:
                    if not any(g['code']=='external_change' and g['member_id']==row['member_id'] for g in _gates(db,unit['id'])):
                        self._open_gate(db,command,'external_change',{'reason':reason,'effect_id':row['id']},row['member_id'])
                    if db.execute('SELECT 1 FROM unit_issues WHERE id=? AND left_at IS NULL',(row['member_id'],)).fetchone():
                        self.fence_member_in(db,command,row['member_id'],reason='triage_rejected')
                self._save(db,command,'triage_effect_rejected',{'effect_id':row['id']},{'effect_id':row['id'],'rejected':True})
            db.execute("UPDATE external_effects SET status='failed',receipt_json=?,updated_at=? WHERE id=?",
                (_json({'reason':reason}),self.clock(),row['id']))
        return {'effect_id':row['id'],'status':'failed','reason':reason}

    def cycle(self,*,after=None,limit=50):
        with self.store.read_snapshot() as db:
            rows=db.execute("SELECT e.id FROM external_effects e JOIN triage_effect_authority a ON a.effect_id=e.id "
                "WHERE e.status IN('pending','running','uncertain') AND (e.retry_at IS NULL OR e.retry_at<=?) AND (? IS NULL OR e.id>?) ORDER BY e.id LIMIT ?",
                (self.clock(),after,after,limit+1)).fetchall()
        return {'items':[self.step(v[0]) for v in rows[:limit]],'next_cursor':rows[limit-1][0] if len(rows)>limit else None}
