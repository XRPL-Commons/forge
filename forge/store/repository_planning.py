"""Versioned repository context and proposals; acceptance is one local transaction.

Notes and model output are suggestions. Only accepted, revision-bound typed
commands acquire external-effect or delivery authority.
"""
import json
import math
import re
import time
import uuid
from urllib.parse import urlsplit

from ..planning_sources import PlanningSources
from .models import Command, IdempotencyConflict, TransitionError, VersionConflict
from .runtime import writable
from .transitions import Lifecycle, _id, _json, _unit, _gates, manifest_hash, project_unit

ACTIONS = {'close','relabel','park','group','order','question','keep'}
NOTE_KINDS = {'convention','decision','lesson','question','next_action'}


def text(value, limit, *, empty=False):
    if not isinstance(value,str) or (not value.strip() and not empty) or len(value.encode())>limit:
        raise TransitionError('Text is missing or exceeds the field limit')
    return value.strip()


def references(value):
    if not isinstance(value,list) or not 1<=len(value)<=20:
        raise TransitionError('Provide one to twenty supporting references')
    for row in value:
        if not isinstance(row,dict) or set(row)!={'url','description','kind','identity'}:
            raise TransitionError('A reference needs URL, description, kind and immutable identity')
        parsed=urlsplit(text(row['url'],2048))
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password:
            raise TransitionError('References must be HTTPS links without credentials')
        text(row['description'],1000);text(row['identity'],256)
        if row['kind'] not in {'issue_revision','candidate','commit','decision','document'}:
            raise TransitionError('Unknown reference kind')
    if len(_json(value).encode())>8192:raise TransitionError('Supporting references exceed their limit')
    return value


def pin_in(db, key):
    row=db.execute('SELECT i.id AS issue_id,i.version AS issue_version,i.current_revision_id AS requirements_revision_id,'
        'm.unit_id,u.version AS unit_version,m.id AS member_id,u.current_candidate_id AS candidate_id '
        'FROM issues i LEFT JOIN unit_issues m ON m.issue_id=i.id AND m.left_at IS NULL '
        'LEFT JOIN units u ON u.id=m.unit_id WHERE i.id=?',(key,)).fetchone()
    if row is None:raise TransitionError('Unknown issue')
    return dict(row)


class RepositoryPlanning(Lifecycle):
    def _apply_repository(self, request_id, actor_id, operation, payload, change):
        text(request_id,256);text(actor_id,256)
        fingerprint=manifest_hash(payload)
        with self.store.write() as db:
            writable(db)
            previous=db.execute('SELECT * FROM repository_planning_requests WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if (previous['actor_id'],previous['operation'],previous['payload_digest'])!=(actor_id,operation,fingerprint):
                    raise IdempotencyConflict('Request identity belongs to another repository decision')
                return json.loads(previous['result_json'])
            result=change(db)
            db.execute('INSERT INTO repository_planning_requests VALUES(?,?,?,?,?,?)',
                (request_id,actor_id,operation,fingerprint,_json(result),self.clock()))
            db.execute("INSERT INTO decisions(id,request_id,actor_id,command,status,payload_json,result_json,created_at) VALUES(?,?,?,?,'applied',?,?,?)",
                (_id(request_id,'decision'),request_id,actor_id,operation,_json({'digest':fingerprint}),_json(result),self.clock()))
            db.execute("INSERT INTO events(transaction_id,kind,at,actor_id,payload_json) VALUES(?,'decision_applied',?,?,?)",
                (request_id,self.clock(),actor_id,_json({'command':operation,**result})))
            return result

    def _repository(self,db, repository_id):
        row=db.execute('SELECT * FROM repository_planning WHERE repository_id=?',(repository_id,)).fetchone()
        if row is None:
            if not db.execute('SELECT 1 FROM repositories WHERE id=?',(repository_id,)).fetchone():raise TransitionError('Unknown repository')
            return dict(repository_id=repository_id,revision=0,rules_revision=0,rules='',proposals_enabled=1,next_triage_at=0)
        return dict(row)

    def due(self, *, limit=50):
        """Coalesce relevant repository events; a launch owns its captured watermark.

        The cursor is scheduling state, not a second copy of repository facts. It
        advances only with an immutable planning context. Events arriving after
        capture remain pending even if that worker completes much later.
        """
        with self.store.read_snapshot() as db:
            result=[]
            for row in db.execute('SELECT r.id AS repository_id,r.full_name,coalesce(p.next_triage_at,0) AS next_at '
                    'FROM repositories r LEFT JOIN repository_planning p ON p.repository_id=r.id '
                    'WHERE p.proposals_enabled=1 OR p.repository_id IS NULL ORDER BY next_at,r.id'):
                cursor=db.execute('SELECT value FROM store_metadata WHERE key=?',
                    ('repository_planning_cursor:'+str(row['repository_id']),)).fetchone()
                captured=json.loads(cursor[0]) if cursor else {'sequence':0,'at':0}
                event=db.execute("SELECT e.sequence,e.at FROM events e LEFT JOIN issues i ON i.id=e.issue_id "
                    "LEFT JOIN units u ON u.id=e.unit_id WHERE e.sequence>? AND "
                    "coalesce(i.repository_id,u.repository_id,json_extract(e.payload_json,'$.repository_id'))=? AND ("
                    "e.kind IN ('issue_admitted','membership_changed','candidate_selected','candidate_superseded','merged','closed','dissolved') OR "
                    "(e.kind='issue_observed' AND json_extract(e.payload_json,'$.planning_changed')=1) OR "
                    "(e.kind='decision_applied' AND json_extract(e.payload_json,'$.command') IN ('repository_rules','repository_note') "
                    "AND e.actor_id!='supervisor:triage')) ORDER BY e.sequence LIMIT 1",
                    (captured['sequence'],row['repository_id'])).fetchone()
                reason='nightly' if row['next_at']<=self.clock() else 'repository_event' if event and max(event['at']+30,captured['at']+300)<=self.clock() else None
                if reason:result.append({**dict(row),'reason':reason})
                if len(result)>=limit:break
            return result

    @staticmethod
    def _capture_cursor_in(db,proposal_id):
        sequence=db.execute('SELECT coalesce(max(sequence),0) FROM events').fetchone()[0]
        db.execute('INSERT INTO store_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            ('repository_planning_inputs:'+proposal_id,str(sequence)))

    def configure(self,repository_id,*,request_id,actor_id,expected_revision,rules,proposals_enabled=True):
        rules=text(rules,16384,empty=True)
        if type(proposals_enabled) is not bool:raise TransitionError('Proposal scheduling requires a boolean')
        payload=dict(repository_id=repository_id,expected_revision=expected_revision,rules=rules,proposals_enabled=proposals_enabled)
        def change(db):
            current=self._repository(db,repository_id)
            if current['revision']!=expected_revision:raise VersionConflict('Repository planning changed')
            revision=current['revision']+1
            rules_revision=current['rules_revision']+int(current['rules']!=rules or current['rules_revision']==0)
            db.execute('INSERT INTO repository_planning VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(repository_id) DO UPDATE SET '
                'revision=excluded.revision,rules=excluded.rules,rules_revision=excluded.rules_revision,'
                'proposals_enabled=excluded.proposals_enabled,updated_at=excluded.updated_at,actor_id=excluded.actor_id',
                (repository_id,revision,rules,rules_revision,int(proposals_enabled),current['next_triage_at'],self.clock(),actor_id))
            if rules_revision!=current['rules_revision']:
                db.execute('INSERT INTO repository_rule_history VALUES(?,?,?,?,?)',(repository_id,rules_revision,rules,actor_id,self.clock()))
            return {'repository_id':repository_id,'revision':revision,'rules_revision':rules_revision}
        return self._apply_repository(request_id,actor_id,'repository_rules',payload,change)

    def note(self,repository_id,*,note_id,expected_revision,kind,content,sources,request_id,actor_id,deleted=False):
        text(note_id,256);content=text(content,8192);sources=references(sources)
        if kind not in NOTE_KINDS or type(deleted) is not bool:raise TransitionError('Unknown note kind or deletion state')
        payload=dict(repository_id=repository_id,note_id=note_id,expected_revision=expected_revision,kind=kind,content=content,sources=sources,deleted=deleted)
        def change(db):
            self._repository(db,repository_id)
            prior=db.execute('SELECT * FROM repository_note_revisions WHERE note_id=? ORDER BY revision DESC LIMIT 1',(note_id,)).fetchone()
            if (prior['revision'] if prior else 0)!=expected_revision or prior and prior['repository_id']!=repository_id:
                raise VersionConflict('Repository note changed')
            if not deleted and (not prior or prior['deleted']):
                active=db.execute('SELECT count(*) FROM repository_note_revisions n WHERE repository_id=? AND deleted=0 '
                    'AND revision=(SELECT max(revision) FROM repository_note_revisions x WHERE x.note_id=n.note_id)',(repository_id,)).fetchone()[0]
                if active>=50:raise TransitionError('Archive or combine a note before adding another; repository context is limited to fifty notes')
            revision=expected_revision+1
            db.execute('INSERT INTO repository_note_revisions VALUES(?,?,?,?,?,?,?,?,?)',
                (note_id,revision,repository_id,kind,content,_json(sources),int(deleted),self.clock(),actor_id))
            return {'repository_id':repository_id,'note_id':note_id,'revision':revision}
        return self._apply_repository(request_id,actor_id,'repository_note',payload,change)

    def begin(self,repository_id,*,proposal_id,base_branch,request_id,actor_id,queued=False):
        text(proposal_id,256);text(base_branch,256)
        def change(db):
            current=self._repository(db,repository_id)
            if current['revision']==0:
                db.execute('INSERT INTO repository_planning(repository_id,updated_at,actor_id) VALUES(?,?,?)',(repository_id,self.clock(),actor_id))
                current=self._repository(db,repository_id)
                db.execute('INSERT INTO repository_rule_history VALUES(?,1,?,?,?)',(repository_id,'',actor_id,self.clock()))
            db.execute("INSERT INTO triage_proposals(id,repository_id,rules_revision,base_branch,status,rationale,created_at,actor_id) VALUES(?,?,?,?,'draft','',?,?)",
                (proposal_id,repository_id,current['rules_revision'],base_branch,self.clock(),actor_id))
            if queued:
                if db.execute("SELECT 1 FROM repository_triage_runs WHERE repository_id=? AND status IN ('queued','running','captured')",(repository_id,)).fetchone():
                    raise TransitionError('This repository already has a triage run')
                db.execute("INSERT INTO repository_triage_runs(id,repository_id,status,requested_at,requested_by) VALUES(?,?,'queued',?,?)",
                    (proposal_id,repository_id,self.clock(),actor_id))
            count=0
            # A snapshot owns every open issue. Rows stay normalized; no backlog blob.
            for row in db.execute("SELECT id FROM issues WHERE repository_id=? AND github_state='open' ORDER BY id",(repository_id,)):
                pin=pin_in(db,row['id'])
                db.execute('INSERT INTO triage_proposal_inputs VALUES(?,?,?,?,?,?,?,?)',(proposal_id,*pin.values()))
                count+=1
            self._capture_cursor_in(db,proposal_id)
            db.execute('UPDATE repository_planning SET next_triage_at=? WHERE repository_id=?',((int(self.clock())//86400+1)*86400,repository_id))
            return {'proposal_id':proposal_id,'repository_id':repository_id,'version':1,'issues':count}
        return self._apply_repository(request_id,actor_id,'triage_begin',dict(repository_id=repository_id,proposal_id=proposal_id,base_branch=base_branch,queued=queued),change)

    def _proposal(self,db,proposal_id,version):
        row=db.execute('SELECT * FROM triage_proposals WHERE id=?',(proposal_id,)).fetchone()
        if row is None:raise TransitionError('Unknown proposal')
        if row['version']!=version:raise VersionConflict('Proposal changed; read it again before deciding')
        return dict(row)

    @staticmethod
    def _row(value,rules):
        if not isinstance(value,dict) or set(value)!={'action','members','reason','rule_quote','parameters'}:
            raise TransitionError('Proposal rows need action, members, reason, rule quote and parameters')
        action=value['action'];members=value['members'];reason=text(value['reason'],8192)
        if action not in ACTIONS or not isinstance(members,list) or not members or any(not isinstance(m,str) or not m for m in members) or len(set(members))!=len(members):
            raise TransitionError('Choose an action and distinct issue members')
        if action=='group' and len(members)<2:raise TransitionError('A composition needs at least two issues')
        if action!='group' and len(members)!=1:raise TransitionError('Only grouping accepts multiple members')
        quote=value['rule_quote']
        if quote is not None and (not text(quote,16384) or quote not in rules):raise TransitionError('A deciding rule must be quoted from the pinned rules')
        params=value['parameters']
        if not isinstance(params,dict) or len(_json(params).encode())>65536:raise TransitionError('Invalid proposal parameters')
        allowed={'close':{'evidence'},'relabel':{'add','remove'},'park':{'add'},'group':{'dependencies','existing_prs','publication_target'},
                 'order':{'prerequisites'},'question':{'question'},'keep':set()}[action]
        if set(params)!=allowed:raise TransitionError('Unexpected parameters for the proposed action')
        if action=='close':references(params['evidence'])
        if action in {'relabel','park'}:
            for labels in params.values():
                if not isinstance(labels,list) or len(labels)>25 or any(not isinstance(v,str) or not v or len(v)>50 for v in labels):
                    raise TransitionError('Use at most twenty-five explicit labels')
        if action=='order':
            refs=params['prerequisites']
            if not isinstance(refs,list) or len(refs)!=len(set(refs)) or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#[1-9][0-9]*',v) for v in refs):
                raise TransitionError('Prerequisites must be distinct owner/repository#number references')
        if action=='group':
            edges=params['dependencies'];prs=params['existing_prs']
            if not isinstance(edges,list) or not isinstance(prs,list):raise TransitionError('Composition requires explicit dependency and existing PR lists')
            for edge in edges:
                if not isinstance(edge,dict) or set(edge)!={'issue_id','prerequisite_issue_id'} or edge['issue_id'] not in members or edge['prerequisite_issue_id'] not in members or edge['issue_id']==edge['prerequisite_issue_id']:
                    raise TransitionError('Internal dependencies must name distinct members of this composition')
            for pr in prs:
                if not isinstance(pr,dict) or set(pr)!={'unit_id','candidate_id','pull_number','head_oid','author'} or any(not isinstance(pr[k],str) or not pr[k] for k in ('unit_id','candidate_id','head_oid')) or type(pr['pull_number']) is not int or pr['pull_number']<1:
                    raise TransitionError('Existing PR choices must pin unit, candidate, pull number and head')
            for pr in prs:
                author=pr['author']
                if not isinstance(author,dict) or set(author)!={'id','type','login'} or type(author['id']) is not int or author['id']<=0 or author['type'] not in {'User','Bot'} or not isinstance(author['login'],str) or not author['login']:raise TransitionError('Every source PR requires its exact observed author identity')
            target=params['publication_target']
            if not isinstance(target,dict) or target.get('kind') not in {'new','existing'}:raise TransitionError('Choose the publication target')
            if target['kind']=='new' and set(target)!={'kind'}:raise TransitionError('Unexpected new PR target fields')
            if target['kind']=='existing':
                if set(target)!={'kind','unit_id','candidate_id','pull_number','head_oid','author','ownership_transfer'} or {k:target[k] for k in ('unit_id','candidate_id','pull_number','head_oid','author')} not in prs or target['ownership_transfer'] is not True:
                    raise TransitionError('Explicitly transfer one exact existing PR to the delivery builder')
                author=target['author']
                if not isinstance(author,dict) or set(author)!={'id','type','login'} or type(author['id']) is not int or author['id']<=0 or author['type'] not in {'User','Bot'} or not isinstance(author['login'],str) or not author['login']:
                    raise TransitionError('Existing PR continuation requires its exact observed author identity')
            if len({pr['unit_id'] for pr in prs})!=len(prs):raise TransitionError('Each existing PR must be explicitly chosen once')
        if action=='question':text(params['question'],4000)
        return dict(action=action,members=sorted(members),reason=reason,rule_quote=quote,parameters=params)

    def publish(self,proposal_id,*,expected_version,rows,rationale,request_id,actor_id,execution_id=None):
        rationale=text(rationale,8192)
        payload=dict(proposal_id=proposal_id,expected_version=expected_version,rows=rows,rationale=rationale)
        if execution_id is not None:payload['execution_id']=execution_id
        def change(db):
            proposal=self._proposal(db,proposal_id,expected_version)
            if proposal['status']!='draft':raise TransitionError('Only a draft can publish its initial proposal')
            rules=db.execute('SELECT rules FROM repository_rule_history WHERE repository_id=? AND revision=?',
                (proposal['repository_id'],proposal['rules_revision'])).fetchone()[0]
            inputs={row[0] for row in db.execute('SELECT issue_id FROM triage_proposal_inputs WHERE proposal_id=?',(proposal_id,))}
            covered=set()
            for position,value in enumerate(rows):
                row=self._row(value,rules)
                if not set(row['members'])<=inputs or covered.intersection(row['members']):raise TransitionError('Each observed issue must have exactly one disposition')
                covered.update(row['members'])
                identifier=_id(proposal_id,'row:'+str(position))
                db.execute('INSERT INTO triage_proposal_rows(id,proposal_id,position,action,reason,rule_quote,parameters_json) VALUES(?,?,?,?,?,?,?)',
                    (identifier,proposal_id,position,row['action'],row['reason'],row['rule_quote'],_json(row['parameters'])))
                self._history(db,identifier,1,row,actor_id)
                db.executemany('INSERT INTO triage_row_members VALUES(?,?)',[(identifier,key) for key in row['members']])
            if covered!=inputs:raise TransitionError('Every open issue in the captured backlog needs a disposition')
            db.execute("UPDATE triage_proposals SET status='proposed',rationale=?,version=version+1 WHERE id=?",(rationale,proposal_id))
            result={'proposal_id':proposal_id,'version':expected_version+1,'rows':len(rows)}
            if execution_id is not None:
                result['notes']=self._captured_notes_in(db,proposal,execution_id,actor_id)
            return result
        return self._apply_repository(request_id,actor_id,'triage_publish',payload,change)

    def _captured_notes_in(self,db,proposal,execution_id,actor_id):
        from .auxiliary import job_in, _artifact
        job=job_in(db,execution_id)
        run=db.execute('SELECT execution_id FROM repository_triage_runs WHERE id=?',(proposal['id'],)).fetchone()
        if not run or run[0]!=execution_id or not job['cleanup'] or actor_id!='supervisor:triage':
            raise TransitionError('Planner notes require the cleaned, captured repository worker')
        context=json.loads(_artifact(db,execution_id,'planning_context'))
        result=(job.get('capture') or {}).get('result') or {}
        notes=result.get('notes',[]);handoff=result.get('handoff')
        if not isinstance(notes,list) or len(notes)>5 or len(_json([notes,handoff]).encode())>32768:
            raise TransitionError('Planner context output exceeds its five-note / 32 KiB bound')
        updates=[]
        for index,note in enumerate(notes):
            if not isinstance(note,dict) or set(note)!={'note_id','kind','content','sources'}:
                raise TransitionError('Planner notes need identity, kind, content and sources')
            if note['note_id'] is not None and note['note_id'] not in context['note_revisions']:
                raise TransitionError('A planner can revise only a note in its captured context')
            updates.append({**note,'note_id':note['note_id'] or _id(execution_id,'note:'+str(index))})
        if handoff is not None:
            if not isinstance(handoff,dict) or set(handoff)!={'content','sources'}:
                raise TransitionError('A planner handoff needs content and sources')
            updates.append({**handoff,'note_id':_id(str(proposal['repository_id']),'planner-handoff'),'kind':'next_action'})
        if len({n['note_id'] for n in updates})!=len(updates):raise TransitionError('A planner note may be revised only once per result')
        repository=db.execute('SELECT full_name FROM repositories WHERE id=?',(proposal['repository_id'],)).fetchone()[0]
        authority=PlanningSources(repository,job['head'],context['issues'])
        receipts=[]
        for note in updates:
            content=text(note['content'],8192);sources=references(note['sources'])
            if note['kind'] not in NOTE_KINDS:raise TransitionError('Unknown planner note kind')
            for source in sources:
                # Sources bind to the exact evidence this worker received. Notes
                # remain interpretations; source validity never grants work authority.
                if not authority.matches(source):raise TransitionError('Planner note source must identify its captured issue, candidate or repository commit')
            prior=db.execute('SELECT * FROM repository_note_revisions WHERE note_id=? ORDER BY revision DESC LIMIT 1',(note['note_id'],)).fetchone()
            expected=context['note_revisions'].get(note['note_id'],0)
            reason=None
            if prior and (prior['repository_id']!=proposal['repository_id'] or prior['revision']!=expected or prior['actor_id']!='supervisor:triage' or prior['deleted']):
                reason='note_changed'
            elif not prior and expected:reason='note_changed'
            elif not prior:
                count=db.execute('SELECT count(*) FROM repository_note_revisions n WHERE repository_id=? AND deleted=0 '
                    'AND revision=(SELECT max(revision) FROM repository_note_revisions x WHERE x.note_id=n.note_id)',(proposal['repository_id'],)).fetchone()[0]
                if count>=50:reason='context_full'
            if reason:
                receipts.append({'note_id':note['note_id'],'status':reason});continue
            db.execute('INSERT INTO repository_note_revisions VALUES(?,?,?,?,?,?,0,?,?)',
                (note['note_id'],expected+1,proposal['repository_id'],note['kind'],content,_json(sources),self.clock(),actor_id))
            receipts.append({'note_id':note['note_id'],'status':'saved','revision':expected+1})
        return receipts

    def _history(self,db,row_id,revision,row,actor):
        db.execute('INSERT INTO triage_row_history VALUES(?,?,?,?,?,?,?,?)',
            (row_id,revision,row['action'],row['reason'],row['rule_quote'],_json(row['parameters']),actor,self.clock()))
        db.executemany('INSERT INTO triage_row_member_history VALUES(?,?,?)',[(row_id,revision,key) for key in row['members']])

    def edit_row(self,proposal_id,row_id,*,expected_version,value,request_id,actor_id):
        def change(db):
            proposal=self._proposal(db,proposal_id,expected_version)
            old=db.execute('SELECT * FROM triage_proposal_rows WHERE id=? AND proposal_id=?',(row_id,proposal_id)).fetchone()
            if not old or old['status']!='proposed' or proposal['status']!='proposed':raise TransitionError('Only an undecided proposal row can be edited')
            rules=db.execute('SELECT rules FROM repository_rule_history WHERE repository_id=? AND revision=?',
                (proposal['repository_id'],proposal['rules_revision'])).fetchone()[0]
            row=self._row(value,rules)
            members=sorted(r[0] for r in db.execute('SELECT issue_id FROM triage_row_members WHERE row_id=?',(row_id,)))
            if members!=row['members']:raise TransitionError('Changing group membership requires a new complete proposal')
            db.execute('UPDATE triage_proposal_rows SET revision=revision+1,action=?,reason=?,rule_quote=?,parameters_json=? WHERE id=?',
                (row['action'],row['reason'],row['rule_quote'],_json(row['parameters']),row_id))
            self._history(db,row_id,old['revision']+1,row,actor_id)
            db.execute('UPDATE triage_proposals SET version=version+1 WHERE id=?',(proposal_id,))
            return {'proposal_id':proposal_id,'row_id':row_id,'version':expected_version+1}
        return self._apply_repository(request_id,actor_id,'triage_edit',dict(proposal_id=proposal_id,row_id=row_id,expected_version=expected_version,value=value),change)

    def accept(self,proposal_id,*,expected_version,accept_rows,reject_rows,request_id,actor_id,
               compose_in=None,order_in=None,existing_prs=(),issue_evidence=(),config):
        if (not isinstance(accept_rows,list) or not isinstance(reject_rows,list)
                or not accept_rows and not reject_rows or len(set(accept_rows+reject_rows))!=len(accept_rows)+len(reject_rows)):
            raise TransitionError('Select distinct rows to accept or reject')
        payload=dict(proposal_id=proposal_id,expected_version=expected_version,accept_rows=sorted(accept_rows),
                     reject_rows=sorted(reject_rows))
        def change(db):
            proposal=self._proposal(db,proposal_id,expected_version)
            if proposal['status']!='proposed':raise TransitionError('Proposal is already settled')
            repository=self._repository(db,proposal['repository_id'])
            if repository['rules_revision']!=proposal['rules_revision']:raise VersionConflict('Triage rules changed; request a new proposal')
            selected=[];seen_units=set()
            # Validate all selected pins before composing/fencing anything.
            for row_id in accept_rows+reject_rows:
                row=db.execute('SELECT * FROM triage_proposal_rows WHERE id=? AND proposal_id=?',(row_id,proposal_id)).fetchone()
                if row is None or row['status']!='proposed':raise VersionConflict('Proposal row changed')
                if row_id in reject_rows:continue
                row=dict(row);row['parameters']=json.loads(row['parameters_json'])
                row['members']=[dict(v) for v in db.execute('SELECT p.* FROM triage_proposal_inputs p JOIN triage_row_members m '
                    'ON m.issue_id=p.issue_id WHERE p.proposal_id=? AND m.row_id=? ORDER BY p.issue_id',(proposal_id,row_id))]
                source_units=set()
                for member in row['members']:
                    expected={k:v for k,v in member.items() if k!='proposal_id'}
                    if pin_in(db,member['issue_id'])!=expected:raise VersionConflict('Issue, requirements, membership or candidate changed')
                    issue=db.execute('SELECT * FROM issues WHERE id=?',(member['issue_id'],)).fetchone()
                    proof=next((p for p in issue_evidence if p.get('issue_id')==member['issue_id']),None)
                    fresh_proof=bool(proof and proof.get('verified') is True and proof.get('issue_version')==member['issue_version']
                        and proof.get('requirements_revision_id')==member['requirements_revision_id'] and type(proof.get('observed_at')) in (int,float)
                        and 0<=self.clock()-proof['observed_at']<=60)
                    if issue['github_state']!='open' or not (fresh_proof or 0<=self.clock()-issue['observed_at']<=300):
                        raise TransitionError('Accept requires a fresh open issue observation')
                    if row['action']=='group' and set(json.loads(issue['labels_json'])).intersection(set(config['hold_labels'])|{'wip'}):
                        raise TransitionError('Held issues cannot be composed')
                    if member['unit_id']:source_units.add(member['unit_id'])
                if row['action']=='group':
                    if seen_units.intersection(source_units):raise TransitionError('One source delivery cannot receive conflicting compositions')
                    seen_units.update(source_units)
                selected.append(row)
            effects=[];units=[]
            for row in selected:
                row_request=_id(request_id,row['id'])
                if row['action']=='group':
                    if compose_in is None:raise TransitionError('Delivery composition is unavailable')
                    from .delivery import Delivery
                    members=[{k:v for k,v in m.items() if k not in {'proposal_id','candidate_id'}} for m in row['members']]
                    sources={m['unit_id'] for m in members if m['unit_id']}
                    proofs=[proof for proof in existing_prs if proof['unit_id'] in sources]
                    choices=row['parameters']['existing_prs']
                    observed=[{k:proof[k] for k in ('unit_id','candidate_id','pull_number','head_oid','author')} for proof in proofs]
                    if sorted(choices,key=lambda p:p['unit_id'])!=sorted(observed,key=lambda p:p['unit_id']):
                        raise VersionConflict('The explicitly approved existing PR choices no longer match current evidence')
                    args=dict(repository_id=proposal['repository_id'],base_branch=proposal['base_branch'],members=members,
                        dependencies=row['parameters']['dependencies'],existing_prs=proofs,rationale=row['reason'],publication_target=row['parameters']['publication_target'])
                    approval={'approved':True,'composition_hash':Delivery.composition_hash(**args)}
                    result=compose_in(db,Command(row_request,actor_id,_id(row_request,'unit'),1),**args,approval=approval)
                    units.append(result['unit_id'])
                elif row['action']=='order':
                    if order_in is None:raise TransitionError('Prerequisite decisions are unavailable')
                    member=row['members'][0]
                    order_in(db,issue_id=member['issue_id'],requirements_revision_id=member['requirements_revision_id'],
                        prerequisites=row['parameters']['prerequisites'],actor_id=actor_id,request_id=row_request)
                elif row['action'] in {'close','park','relabel'}:
                    effects.append(self._effect_in(db,proposal,row,row_request,actor_id,config))
                db.execute("UPDATE triage_proposal_rows SET status='accepted',decision_id=? WHERE id=?",(_id(request_id,'decision'),row['id']))
            for row_id in reject_rows:
                db.execute("UPDATE triage_proposal_rows SET status='rejected',decision_id=? WHERE id=?",(_id(request_id,'decision'),row_id))
            remaining=db.execute("SELECT 1 FROM triage_proposal_rows WHERE proposal_id=? AND status='proposed' LIMIT 1",(proposal_id,)).fetchone()
            state='proposed' if remaining else 'accepted' if db.execute("SELECT 1 FROM triage_proposal_rows WHERE proposal_id=? AND status='accepted' LIMIT 1",(proposal_id,)).fetchone() else 'rejected'
            db.execute('UPDATE triage_proposals SET status=?,version=version+1,accepted_at=?,accepted_by=? WHERE id=?',
                (state,self.clock(),actor_id,proposal_id))
            return {'proposal_id':proposal_id,'version':expected_version+1,'status':state,'effects':effects,'units':units}
        return self._apply_repository(request_id,actor_id,'triage_accept',payload,change)

    def _effect_in(self,db,proposal,row,request,actor,config):
        member=row['members'][0]
        issue=db.execute('SELECT * FROM issues WHERE id=?',(member['issue_id'],)).fetchone()
        created=member['unit_id'] is None
        unit_id=member['unit_id'] or _id(request,'unit')
        command=Command(request+':fence',actor,unit_id,member['unit_version'] or 1)
        if created:self._create_unit(db,command,unit_id,issue,proposal['base_branch'],member['requirements_revision_id'])
        unit=_unit(db,unit_id);self._live(unit)
        command=Command(command.request_id,actor,unit_id,unit['version'])
        current_member=db.execute('SELECT id FROM unit_issues WHERE unit_id=? AND issue_id=? AND left_at IS NULL',(unit_id,member['issue_id'])).fetchone()[0]
        if db.execute("SELECT 1 FROM merge_requests WHERE unit_id=? AND status IN('pending','uncertain')",(unit_id,)).fetchone():
            raise TransitionError('Reconcile the pending merge before accepting triage changes')
        if any(g['code']=='operator_sync' for g in _gates(db,unit_id)):raise TransitionError('A previous operator effect is still pending')
        before=self._lifecycle_summary(project_unit(db,unit_id))
        if created:
            self._open_gate(db,command,'operator_backlog',{'reason':'Triage changed metadata; work has not been queued'})
        affected=self.fence_member_in(db,command,current_member,reason='accepted_triage')
        gate=self._open_gate(db,command,'triage_sync',{'reason':row['reason'],'proposal_id':proposal['id'],'effect_id':request},current_member)
        self._save(db,command,'triage_effect',{'row_id':row['id']},{'effect_id':request},initial=created,before=before)
        unit=_unit(db,unit_id)
        params=row['parameters'];add=params.get('add',[]);remove=params.get('remove',[])
        if row['action']=='park':add=sorted(set(add)|{config['hold_labels'][0]});remove=[config['ready_label']]
        payload={'action':row['action'],'add':add,'remove':remove,'reason':row['reason'],
            'state_reason':'completed' if row['action']=='close' else None,'evidence':params.get('evidence',[]),
            'labels':json.loads(issue['labels_json']),'assignees':json.loads(issue['assignees_json']),
            'member_id':current_member,'candidate_id':unit['current_candidate_id']}
        db.execute("INSERT INTO external_effects(id,request_id,unit_id,kind,status,expected_version,payload_json,created_at,updated_at) "
            "VALUES(?,?,?,'triage_issue','pending',?,?,?,?)",(request,request,unit_id,unit['version'],_json(payload),self.clock(),self.clock()))
        db.execute('INSERT INTO triage_effect_authority VALUES(?,?,?,?,?,?,?,?,?)',
            (request,proposal['id'],row['id'],member['issue_id'],member['issue_version'],member['requirements_revision_id'],actor,gate,current_member))
        db.executemany('INSERT INTO triage_effect_members VALUES(?,?)',[(request,key) for key in affected['member_ids']])
        return request

    def repository(self,repository_id):
        with self.store.read_snapshot() as db:
            result=self._repository(db,repository_id)
            result['proposals_enabled']=bool(result['proposals_enabled'])
            result['runs']=[dict(row) for row in db.execute('SELECT id,status,error,requested_at FROM repository_triage_runs WHERE repository_id=? ORDER BY requested_at DESC LIMIT 5',(repository_id,))]
            result['notes']=[]
            for raw in db.execute('SELECT * FROM repository_note_revisions n WHERE repository_id=? AND deleted=0 '
                'AND revision=(SELECT max(revision) FROM repository_note_revisions x WHERE x.note_id=n.note_id) ORDER BY note_id LIMIT 50',(repository_id,)):
                note=dict(raw);note['sources']=json.loads(note.pop('sources_json'));note['needs_refresh']=False
                for source in note['sources']:
                    if source['kind']=='issue_revision':
                        valid=db.execute('SELECT 1 FROM issues WHERE current_revision_id=?',(source['identity'],)).fetchone()
                        note['needs_refresh']|=not bool(valid)
                    elif source['kind']=='candidate':
                        valid=db.execute('SELECT 1 FROM units WHERE current_candidate_id=?',(source['identity'],)).fetchone()
                        note['needs_refresh']|=not bool(valid)
                    elif source['kind']=='decision':
                        note['needs_refresh']|=not bool(db.execute('SELECT 1 FROM decisions WHERE id=?',(source['identity'],)).fetchone())
                # Commits/documents are immutable source references, not claims about
                # current repository state. Operators refresh them by note revision.
                note['source_policy']='current_revision' if any(v['kind'] in {'issue_revision','candidate'} for v in note['sources']) else 'immutable_reference'
                result['notes'].append(note)
            return result

    def proposals(self,repository_id,*,before=None,limit=20):
        if type(limit) is not int or not 1<=limit<=100:raise TransitionError('Proposal page limit is 1–100')
        stamp,key=(before.split('|',1) if before else (None,None))
        with self.store.read_snapshot() as db:
            rows=db.execute('SELECT * FROM triage_proposals WHERE repository_id=? AND (? IS NULL OR (created_at,id)<(?,?)) ORDER BY created_at DESC,id DESC LIMIT ?',
                (repository_id,stamp,float(stamp) if stamp is not None else None,key,limit+1)).fetchall()
            return {'items':[dict(v) for v in rows[:limit]],'next':str(rows[limit-1]['created_at'])+'|'+rows[limit-1]['id'] if len(rows)>limit else None}

    def proposal(self,proposal_id,*,after=-1,limit=50):
        if type(limit) is not int or not 1<=limit<=100 or type(after) is not int or after< -1:raise TransitionError('Invalid proposal page')
        with self.store.read_snapshot() as db:
            raw=db.execute('SELECT * FROM triage_proposals WHERE id=?',(proposal_id,)).fetchone()
            if raw is None:raise TransitionError('Unknown proposal')
            result=dict(raw)
            run=db.execute('SELECT status,error,requested_at FROM repository_triage_runs WHERE id=?',(proposal_id,)).fetchone()
            result['run']=dict(run) if run else None
            rows=db.execute('SELECT * FROM triage_proposal_rows WHERE proposal_id=? AND position>? ORDER BY position LIMIT ?',
                (proposal_id,after,limit+1)).fetchall()
            result['rows']=[]
            for raw in rows[:limit]:
                row=dict(raw);row['parameters']=json.loads(row.pop('parameters_json'))
                member_page=self.members(row['id'],limit=5)
                row['members']=member_page['items'];row['members_next']=member_page['next'];row['member_count']=member_page['total']
                row['effects']=[dict(v) for v in db.execute('SELECT e.id,e.status,e.attempts,e.retry_at,e.receipt_json FROM external_effects e '
                    'JOIN triage_effect_authority a ON a.effect_id=e.id WHERE a.row_id=? ORDER BY e.id',(row['id'],))]
                for effect in row['effects']:effect['receipt']=json.loads(effect.pop('receipt_json') or 'null')
                if result['rows'] and len(json.dumps(result['rows']+[row]).encode())>700000:break
                result['rows'].append(row)
            result['next']=result['rows'][-1]['position'] if len(rows)>len(result['rows']) else None
            result['total']=db.execute('SELECT count(*) FROM triage_proposal_rows WHERE proposal_id=?',(proposal_id,)).fetchone()[0]
            return result

    def members(self,row_id,*,after='',limit=100):
        if type(limit) is not int or not 1<=limit<=100 or not isinstance(after,str):raise TransitionError('Invalid member page')
        with self.store.read_snapshot() as db:
            rows=db.execute('SELECT m.issue_id,i.github_number,r.title,p.issue_version,p.requirements_revision_id,p.unit_id,p.unit_version,p.member_id,p.candidate_id,c.pull_number,c.head_oid,x.author_json '
                'FROM triage_row_members m JOIN triage_proposal_rows row ON row.id=m.row_id '
                'JOIN triage_proposal_inputs p ON p.proposal_id=row.proposal_id AND p.issue_id=m.issue_id '
                'JOIN issues i ON i.id=m.issue_id JOIN issue_revisions r ON r.id=p.requirements_revision_id '
                'LEFT JOIN candidates c ON c.id=p.candidate_id LEFT JOIN triage_candidate_context x ON x.proposal_id=p.proposal_id AND x.candidate_id=p.candidate_id WHERE m.row_id=? AND m.issue_id>? ORDER BY m.issue_id LIMIT ?',
                (row_id,after,limit+1)).fetchall()
            items=[dict(v) for v in rows[:limit]]
            for item in items:item['candidate_author']=json.loads(item.pop('author_json') or 'null')
            return {'items':items,'next':rows[limit-1]['issue_id'] if len(rows)>limit else None,
                'total':db.execute('SELECT count(*) FROM triage_row_members WHERE row_id=?',(row_id,)).fetchone()[0]}

    def queue(self,repository_id,*,request_id,actor_id,base_branch):
        proposal_id=_id(request_id,'triage')
        self.begin(repository_id,proposal_id=proposal_id,base_branch=base_branch,request_id=request_id,actor_id=actor_id,queued=True)
        return {'proposal_id':proposal_id,'status':'queued'}

    def refresh_inputs(self,proposal_id):
        with self.store.write() as db:
            writable(db)
            proposal=self._proposal(db,proposal_id,1)
            run=db.execute('SELECT status FROM repository_triage_runs WHERE id=?',(proposal_id,)).fetchone()
            if proposal['status']!='draft' or not run or run[0]!='queued':raise TransitionError('Planning inputs are already pinned to a worker')
            rules=self._repository(db,proposal['repository_id'])
            if rules['rules_revision']!=proposal['rules_revision']:raise VersionConflict('Triage rules changed before the planning run')
            db.execute('DELETE FROM triage_proposal_inputs WHERE proposal_id=?',(proposal_id,))
            for row in db.execute("SELECT id FROM issues WHERE repository_id=? AND github_state='open' ORDER BY id",(proposal['repository_id'],)):
                pin=pin_in(db,row[0])
                db.execute('INSERT INTO triage_proposal_inputs VALUES(?,?,?,?,?,?,?,?)',(proposal_id,*pin.values()))
            self._capture_cursor_in(db,proposal_id)

    def context(self,proposal_id,*,head):
        with self.store.read_snapshot() as db:
            proposal=db.execute('SELECT * FROM triage_proposals WHERE id=?',(proposal_id,)).fetchone()
            if not proposal:raise TransitionError('Unknown proposal')
            rules=db.execute('SELECT rules FROM repository_rule_history WHERE repository_id=? AND revision=?',
                (proposal['repository_id'],proposal['rules_revision'])).fetchone()[0]
            issues=[]
            for row in db.execute('SELECT p.*,i.github_number,i.labels_json,i.assignees_json,r.title,r.body,u.base_branch,c.pull_number,c.head_oid '
                'FROM triage_proposal_inputs p JOIN issues i ON i.id=p.issue_id JOIN issue_revisions r ON r.id=p.requirements_revision_id '
                'LEFT JOIN units u ON u.id=p.unit_id LEFT JOIN candidates c ON c.id=p.candidate_id WHERE p.proposal_id=? ORDER BY i.github_number',(proposal_id,)):
                issue=dict(row);issue['labels']=json.loads(issue.pop('labels_json'));issue['assignees']=json.loads(issue.pop('assignees_json'))
                # Full immutable issue text stays available through GitHub; the planner
                # receives a marked excerpt, never a claim that an excerpt is complete.
                issue['body_excerpt']=issue.pop('body')[:6000]
                issue['body_complete']=len(row['body'])<=6000
                issue['gates']=[{'code':g['code'],'member_id':g['member_id']} for g in _gates(db,issue['unit_id'])] if issue['unit_id'] else []
                issues.append(issue)
            notes=self.repository(proposal['repository_id'])['notes']
            from ..planning_contract import provider_triage_rows
            result={'proposal_id':proposal_id,'rules':rules,'rows_schema':provider_triage_rows(rules=rules),'notes':[n for n in notes if not n['needs_refresh']],'issues':issues,
                'note_revisions':{n['note_id']:n['revision'] for n in notes},
                'handoff_note_id':_id(str(proposal['repository_id']),'planner-handoff'),
                'event_cursor':int(db.execute('SELECT value FROM store_metadata WHERE key=?',
                    ('repository_planning_inputs:'+proposal_id,)).fetchone()[0])}
            repository=db.execute('SELECT full_name FROM repositories WHERE id=?',(proposal['repository_id'],)).fetchone()[0]
            result['note_sources_schema']=PlanningSources(repository,head,issues).schema()
            if len(_json(result).encode())>512*1024:
                raise TransitionError('Repository context exceeds the 512 KiB planning input limit; reduce note or issue text before requesting triage')
            return result

    def runs(self,*,active=False):
        with self.store.read_snapshot() as db:
            predicate="status IN ('running','captured')" if active else "status='queued'"
            return [dict(row) for row in db.execute('SELECT * FROM repository_triage_runs WHERE '+predicate+' ORDER BY requested_at,id LIMIT 50')]

    def job(self,execution_id,*,context=False):
        from .auxiliary import job_in, _artifact
        with self.store.read_snapshot() as db:
            job=job_in(db,execution_id)
            if context:
                payload=_artifact(db,execution_id,'planning_context')
                if manifest_hash(json.loads(payload))!=job['context_hash']:raise TransitionError('Planning input evidence does not match its launch')
                job['context']=json.loads(payload)
            return job

    def reserve(self,proposal_id,job,context):
        from .auxiliary import save_in
        import hashlib
        payload=_json(context).encode()
        if len(payload)>512*1024:raise TransitionError('Planning context exceeds its 512 KiB launch limit')
        if manifest_hash(context)!=job['context_hash']:raise TransitionError('Planning context identity changed')
        if context.get('note_sources_schema')!=PlanningSources(job['repo'],job['head'],context['issues']).schema():
            raise TransitionError('Planning note sources must match the assigned repository and head')
        with self.store.write() as db:
            writable(db)
            proposal=self._proposal(db,proposal_id,1)
            run=db.execute('SELECT * FROM repository_triage_runs WHERE id=?',(proposal_id,)).fetchone()
            if not run or run['status']!='queued' or proposal['status']!='draft':raise VersionConflict('Triage launch was already claimed')
            if job.get('proposal_id')!=proposal_id or job.get('repo_id')!=proposal['repository_id'] or job.get('role')!='triage' or job.get('slot')!='repository-triage':raise TransitionError('Planning worker must match the exact repository request')
            if type(job.get('deadline')) not in (int,float) or not math.isfinite(job['deadline']) or job['deadline']<=self.clock():raise TransitionError('Planning worker needs a finite future deadline')
            if job.get('runner_config',{}).get('provider'):
                from .executions import provider_available
                if not provider_available(db,job['runner_config']['provider'],job.get('provider_id'),job['runner_config'],self.clock()):raise TransitionError('Planning coding account was claimed by another worker')
            if self._repository(db,proposal['repository_id'])['rules_revision']!=proposal['rules_revision']:raise VersionConflict('Rules changed before launch')
            for pin in db.execute('SELECT * FROM triage_proposal_inputs WHERE proposal_id=?',(proposal_id,)):
                if pin_in(db,pin['issue_id'])!={k:v for k,v in dict(pin).items() if k!='proposal_id'}:raise VersionConflict('Issue changed before triage launch')
            if db.execute("SELECT 1 FROM auxiliary_leases WHERE slot='repository-triage'").fetchone():raise TransitionError('Another repository is using the triage worker')
            for issue in context['issues']:
                if issue.get('candidate_id') and issue.get('candidate_author'):
                    db.execute('INSERT INTO triage_candidate_context VALUES(?,?,?) ON CONFLICT DO NOTHING',(proposal_id,issue['candidate_id'],_json(issue['candidate_author'])))
            save_in(db,'triage',proposal_id,job,self.clock())
            db.execute('INSERT INTO store_metadata VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                ('repository_planning_cursor:'+str(proposal['repository_id']),_json({'sequence':context['event_cursor'],'at':self.clock()})))
            db.execute('INSERT INTO auxiliary_artifacts VALUES(?,?,?,?,?)',(job['id'],'planning_context',0,hashlib.sha256(payload).hexdigest(),payload))
            db.execute("UPDATE repository_triage_runs SET status='running',execution_id=? WHERE id=?",(job['id'],proposal_id))

    def progress(self,execution_id,**updates):
        from .auxiliary import save_in, job_in, MUTABLE
        if set(updates)-MUTABLE:raise TransitionError('Unknown execution progress field')
        with self.store.write() as db:
            writable(db)
            job=job_in(db,execution_id);job.update(updates)
            run=db.execute('SELECT * FROM repository_triage_runs WHERE execution_id=?',(execution_id,)).fetchone()
            if not run:raise TransitionError('Unknown triage worker')
            save_in(db,'triage',run['id'],job,self.clock())
            if job.get('capture') is not None:
                db.execute("UPDATE repository_triage_runs SET status='captured' WHERE id=? AND status='running'",(run['id'],))
            return job

    def settle_run(self,proposal_id,*,error=None):
        with self.store.write() as db:
            writable(db)
            run=db.execute('SELECT * FROM repository_triage_runs WHERE id=?',(proposal_id,)).fetchone()
            if not run:raise TransitionError('Unknown triage run')
            if run['execution_id']:
                execution=db.execute('SELECT cleanup FROM auxiliary_executions WHERE id=?',(run['execution_id'],)).fetchone()
                if not execution or not execution['cleanup']:raise TransitionError('Triage must be cleaned before settling its result')
            db.execute('UPDATE repository_triage_runs SET status=?,error=? WHERE id=?',('failed' if error else 'complete',error,proposal_id))
