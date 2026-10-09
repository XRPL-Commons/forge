"""Operator/client boundary for repository plans; reads stay bounded and lazy."""
import time
import json

from ..store.models import TransitionError, VersionConflict
from ..store.repository_planning import RepositoryPlanning
from ..store.runtime import selected
from .config import load


class PlanningService:
    def __init__(self,root,*,actor_id,clock=time.time,parts=None):
        self.root,self.actor_id,self.clock=root,actor_id,clock
        self.store=selected(root) if parts is None else parts['store']
        if self.store is None:raise TransitionError('Repository planning requires the selected SQLite runtime')
        self.plans=RepositoryPlanning(self.store,clock=clock)
        self._parts=parts

    def parts(self):
        if self._parts is None:
            from .sqlite_runtime import components
            self._parts=components(self.root,clock=self.clock)
        return self._parts

    def read(self,repository_id,*,proposal_id=None,row_id=None,after=None):
        with self.store.read_snapshot() as db:
            repo=db.execute('SELECT id,full_name AS name FROM repositories WHERE id=?',(repository_id,)).fetchone()
            if not repo:raise TransitionError('Unknown repository')
            if proposal_id:
                found=db.execute('SELECT repository_id FROM triage_proposals WHERE id=?',(proposal_id,)).fetchone()
                if not found or found[0]!=repository_id:raise TransitionError('Proposal belongs to another repository')
            if row_id:
                found=db.execute('SELECT p.repository_id FROM triage_proposal_rows r JOIN triage_proposals p ON p.id=r.proposal_id WHERE r.id=?',(row_id,)).fetchone()
                if not found or found[0]!=repository_id:raise TransitionError('Row belongs to another repository')
        if row_id:return {'members':self.plans.members(row_id,after=after or '')}
        if proposal_id:return {'proposal':self.plans.proposal(proposal_id,after=int(after) if after is not None else -1)}
        return {'repository':dict(repo),'planning':self.plans.repository(repository_id),'proposals':self.plans.proposals(repository_id,before=after)}

    def action(self,repository_id,value):
        if not isinstance(value,dict):raise TransitionError('Expected a planning operation')
        action=value.get('action')
        fields={'triage':{'request_id'},'rules':{'request_id','expected_revision','rules','proposals_enabled'},
            'note':{'request_id','note_id','expected_revision','kind','content','sources','deleted'},
            'edit':{'request_id','proposal_id','row_id','expected_version','value'},
            'accept':{'request_id','proposal_id','expected_version','accept_rows','reject_rows'}}
        if action not in fields or set(value)!={'action'}|fields[action]:raise TransitionError('Unexpected fields for planning operation')
        args={k:v for k,v in value.items() if k!='action'}
        args['actor_id']=self.actor_id
        self.read(repository_id,proposal_id=value.get('proposal_id'),row_id=value.get('row_id'))
        if action=='triage':return self.parts()['planning'].request(repository_id,**args)
        if action=='rules':return self.plans.configure(repository_id,**args)
        if action=='note':return self.plans.note(repository_id,**args)
        if action=='edit':return self.plans.edit_row(**args)
        with self.store.read_snapshot() as db:
            replay=db.execute('SELECT 1 FROM repository_planning_requests WHERE request_id=?',(value['request_id'],)).fetchone()
        if replay:
            return self.plans.accept(**args,config=load(self.root))
        from ..store.delivery import Delivery
        from ..store.repository_requirements import order_in
        parts=self.parts();github=parts['github'];proofs=[];issue_evidence=[]
        with self.store.read_snapshot() as db:
            repo=dict(db.execute('SELECT id,full_name AS name FROM repositories WHERE id=?',(repository_id,)).fetchone())
            rows=[dict(r) for r in db.execute('SELECT DISTINCT c.id AS candidate_id,c.unit_id,c.pull_number,c.head_oid FROM candidates c '
                'JOIN triage_proposal_inputs p ON p.candidate_id=c.id JOIN triage_row_members m ON m.issue_id=p.issue_id '
                'JOIN triage_proposal_rows r ON r.id=m.row_id AND r.proposal_id=p.proposal_id '
                "WHERE p.proposal_id=? AND r.action='group'",(value['proposal_id'],))]
            issue_rows=[]
            for row_id in value['accept_rows']:
                issue_rows.extend(dict(row) for row in db.execute('SELECT p.*,i.github_number,i.labels_json,i.assignees_json,r.requirements_json '
                    'FROM triage_row_members m JOIN triage_proposal_rows t ON t.id=m.row_id '
                    'JOIN triage_proposal_inputs p ON p.proposal_id=t.proposal_id AND p.issue_id=m.issue_id '
                    'JOIN issues i ON i.id=p.issue_id JOIN issue_revisions r ON r.id=p.requirements_revision_id '
                    'WHERE t.proposal_id=? AND t.id=?',(value['proposal_id'],row_id)))
        for issue in issue_rows:
            remote,requirements=github.issue({'repo_id':repo['id'],'repo':repo['name'],'number':issue['github_number']})
            labels=sorted(v['name'] if isinstance(v,dict) else v for v in remote.get('labels',[]))
            assignees=sorted(v['login'] if isinstance(v,dict) else v for v in remote.get('assignees',[]))
            if remote.get('state')!='open' or requirements!=json.loads(issue['requirements_json']) or labels!=sorted(json.loads(issue['labels_json'])) or assignees!=sorted(json.loads(issue['assignees_json'])):
                raise VersionConflict('Issue requirements, labels or ownership changed; request a fresh proposal')
            issue_evidence.append({k:issue[k] for k in ('issue_id','issue_version','requirements_revision_id')}|{'verified':True,'observed_at':self.clock()})
        for candidate in rows:
            remote=github.call('controller',{'repo_id':repo['id'],'repo':repo['name']},'GET',f"/pulls/{candidate['pull_number']}")
            if remote.get('number')!=candidate['pull_number'] or remote.get('state')!='open' or remote.get('merged') or (remote.get('head') or {}).get('sha')!=candidate['head_oid']:
                raise VersionConflict('An existing PR changed; request a fresh proposal')
            proofs.append({**candidate,'verified':True,'merged':False,'observed_at':self.clock(),'author':{k:(remote.get('user') or {}).get(k) for k in ('id','type','login')}})
        return self.plans.accept(**args,compose_in=Delivery(self.store,clock=self.clock).compose_in,order_in=order_in,
            existing_prs=proofs,issue_evidence=issue_evidence,config=parts['config'])


def client_request(root,clients,token,value):
    from ..client_connections import fields,need
    fields(value,('repository','instance','generation','operation','payload'))
    need(value['operation'] in ('read','action'))
    capability='repositories:read' if value['operation']=='read' else 'repositories:triage'
    grant=clients.authorize(token,value['repository'],capability,value['instance'],value['generation'])
    service=PlanningService(root,actor_id='client:'+grant['id'])
    payload=value['payload'];need(isinstance(payload,dict))
    if value['operation']=='read':
        need(not set(payload)-{'proposal_id','row_id','after'})
        result=service.read(value['repository']['id'],**payload)
        clients.authorize(token,value['repository'],capability,value['instance'],value['generation'])
        return result
    with clients.effect_guard(token,value['instance'],value['generation']):
        return service.action(value['repository']['id'],payload)
