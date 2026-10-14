"""Publish a verified tree through the Controller App, with durable CAS steps.

GitHub's updateRefs beforeOid and createCommitOnBranch expectedHeadOid are the
remote compare-and-swap boundary. A worker marker is never publication proof.
"""
import base64
import json
import re
import time
import uuid
from urllib.parse import quote

from ..publication_artifacts import PublicationClaim
from ..store.executions import ExecutionQueries
from ..store.models import Command, TransitionError, IdempotencyConflict
from ..store.publications import Publications, target_identity
from .effect_controller import EffectController
from .github import GitHubUnavailable, GitHubBudgetExhausted

CAS = '''mutation($input:UpdateRefsInput!){updateRefs(input:$input){clientMutationId}}'''
COMMIT = '''mutation($input:CreateCommitOnBranchInput!){createCommitOnBranch(input:$input){commit{oid}}}'''
ZERO='0'*40


class _Pending(Exception):
    pass


class Publisher:
    def __init__(self,store,github,output_loader,*,signing_capability,admission=None,clock=time.time,reliability=None,max_mutations=16):
        self.store,self.github,self.output_loader=store,github,output_loader
        self.signing_capability,self.admission,self.clock=signing_capability,admission,clock
        if type(max_mutations) is not int or not 1<=max_mutations<=64:raise ValueError('Publication mutation bound must be 1..64')
        self.max_mutations=max_mutations
        self.commands=Publications(store,clock=clock,reliability=reliability)
        self.jobs=ExecutionQueries(store)
        self.effects=EffectController(store,github,clock=clock,reliability=reliability)

    def _capability(self,job,target):
        capability=self.signing_capability(job)
        if (not isinstance(capability,dict) or capability.get('qualified') is not True
                or capability.get('strategy') not in ('rest','graphql')):
            raise PublicationClaim('publication_signing_unqualified')
        if (capability['strategy']!=target['strategy'] or capability.get('app_id')!=target['app_id']
                or target['signing_qualified'] is not True):raise PublicationClaim('publication_capability_changed')
        self.workflows_granted=capability.get('workflows_granted') is True
        connection=self.github.connection('controller',job['repo_id'])
        if connection.get('app_id')!=target['app_id']:raise PublicationClaim('publication_app_changed')

    def ensure(self,job,output,target,report):
        target=target_identity(target)
        try:self._capability(job,target)
        except PublicationClaim as error:return {'status':'blocked','claim':error.payload()}
        with self.effects._lock():
            current=self.jobs.job(job['id'])
            command=Command('publication:'+job['id']+':'+str(uuid.uuid4()),'supervisor:publisher',current['unit_id'],current['unit_version'])
            queued=self.commands.queue(command,job['id'],execution_fence=job['execution_fence'],
                input_manifest=job['input_manifest'],output=output,target=target,report=report)
            return self._step(queued['intent_id'])

    def step(self,identifier):
        with self.effects._lock():return self._step(identifier)

    def _result(self,identifier):
        value=self.commands.get(identifier)
        return dict(status=value['status'],intent_id=identifier,receipt=value['receipt'],claim=value['claim'])

    def _admit(self,intent,job):
        self.commands.admitted(intent['id'])
        if self.admission is not None and not self.admission(job):raise TransitionError('Publication admission changed')
        self._capability(job,intent['target'])

    def _call(self,job,method,path,payload=None):
        extra={'workflows':'write'} if method!='GET' and getattr(self,'workflow_needed',False) and getattr(self,'workflows_granted',False) else None
        return self.github.call('controller',job,method,path,payload,**({'extra':extra} if extra else {}))

    def _graphql(self,job,query,variables):
        extra={'workflows':'write'} if getattr(self,'workflow_needed',False) and getattr(self,'workflows_granted',False) else None
        return self.github.graphql('controller',job,query,variables,**({'extra':extra} if extra else {}))

    def _ref(self,job,branch):
        try:ref=self._call(job,'GET','/git/ref/heads/'+quote(branch,safe=''))
        except GitHubUnavailable as error:
            if error.status==404:return None
            raise
        if ref.get('ref')!='refs/heads/'+branch or (ref.get('object') or {}).get('type')!='commit':
            raise PublicationClaim('publication_ref_unverifiable')
        return ref['object']['sha']

    def _send(self,intent,job,key,request,send,*,repeat_safe=False):
        step=self.commands.prepare_step(intent['id'],key,request)
        if step['status']=='confirmed':return json.loads(step['receipt_json'])
        if step['status']=='sending' and not repeat_safe:
            self.commands.uncertain(intent['id'],key);raise _Pending()
        if self.sends>=self.max_mutations:raise _Pending()
        self._admit(intent,job)
        try:self.commands.claim_step(intent['id'],key,repeat_safe=repeat_safe)
        except TransitionError:
            if step['attempts']:self.commands.uncertain(intent['id'],key)
            raise _Pending()
        self.sends+=1
        try:receipt=send()
        except (GitHubUnavailable,GitHubBudgetExhausted) as error:
            # A redacted HTTP response cannot prove a signing-rule rejection.
            if isinstance(error,GitHubUnavailable) and error.status==429:
                self.commands.refused_step(intent['id'],key)
            elif isinstance(error,GitHubUnavailable) and error.status in (401,403,404,422):
                self.commands.block(intent['id'],dict(code='publication_refused',step=key,http_status=error.status))
            else:self.commands.uncertain(intent['id'],key)
            raise _Pending() from None
        self.commands.record_step(intent['id'],key,receipt)
        return receipt

    def _cas(self,intent,job,key,before,after,*,force=False):
        branch=intent['target']['branch']
        request=dict(repository_id=job['repo_id'],branch=branch,before=before,after=after,force=force)
        step=self.commands.prepare_step(intent['id'],key,request)
        if key=='initial_ref' and step['status']=='confirmed':return
        observed=self._ref(job,branch)
        if observed==after and step['attempts']:
            self.commands.record_step(intent['id'],key,{'head_oid':after})
            return
        if step['status']=='confirmed':
            if observed!=after:raise PublicationClaim('publication_head_changed')
            return
        if observed!=before:raise PublicationClaim('publication_head_changed')
        repository=self._call(job,'GET','')
        if repository.get('id')!=job['repo_id'] or not repository.get('node_id'):raise PublicationClaim('publication_repository_changed')
        def send():
            self._graphql(job,CAS,{'input':{'repositoryId':repository['node_id'],
                'refUpdates':[dict(name='refs/heads/'+branch,beforeOid=before or ZERO,afterOid=after,force=force)]}})
            return {'head_oid':after}
        self._send(intent,job,key,request,send,repeat_safe=True)
        if self._ref(job,branch)!=after:raise PublicationClaim('publication_head_changed')

    def _commit_proof(self,job,oid,output,parent,marker,bot):
        raw=self._call(job,'GET','/git/commits/'+oid)
        commit=self._call(job,'GET','/commits/'+oid)
        if (raw.get('sha')!=oid or (raw.get('tree') or {}).get('sha')!=output.tree_oid
                or [p.get('sha') for p in raw.get('parents',[])]!=[parent]
                or marker not in raw.get('message','') or (raw.get('verification') or {}).get('verified') is not True
                or commit.get('sha')!=oid or (commit.get('author') or {}).get('login')!=bot
                or (commit.get('author') or {}).get('type')!='Bot'):
            raise PublicationClaim('publication_commit_unverified')
        return raw

    @staticmethod
    def _pr_matches(pr,job,target,head,author=None):
        user=pr.get('user') or {}
        return (pr.get('state')=='open' and not pr.get('merged') and pr.get('draft') is False
            and (pr.get('head') or {}).get('sha')==head and (pr.get('head') or {}).get('ref')==target['branch']
            and ((pr.get('head') or {}).get('repo') or {}).get('id')==job['repo_id']
            and (pr.get('base') or {}).get('ref')==target['base_branch']
            and ((pr.get('base') or {}).get('repo') or {}).get('id')==job['repo_id']
            and (author is None or all(user.get(k)==v for k,v in author.items())))

    def _pull(self,intent,job,head,bot):
        target=intent['target'];number=target['pull_number'];author=target['authorization']['author'] if target['authorization'] else {'login':bot,'type':'Bot'}
        if number is not None:
            pr=self._call(job,'GET','/pulls/'+str(number))
            if pr.get('number')!=number or not self._pr_matches(pr,job,target,head,author):raise PublicationClaim('publication_pr_changed')
            return pr
        owner=target['repository'].split('/')[0]
        rows=self._call(job,'GET','/pulls?state=open&head='+quote(owner+':'+target['branch'],safe='')+'&base='+quote(target['base_branch'],safe='')+'&per_page=100')
        if not isinstance(rows,list) or len(rows)>=100:raise PublicationClaim('publication_pr_unverifiable')
        if len(rows)>1:raise PublicationClaim('publication_pr_ambiguous')
        if rows:
            pr=self._call(job,'GET','/pulls/'+str(rows[0]['number']))
            if not self._pr_matches(pr,job,target,head,author):raise PublicationClaim('publication_pr_changed')
            step=self.commands.get(intent['id'])['steps'].get('pr')
            if not step or not step['attempts']:raise PublicationClaim('publication_pr_not_owned')
            return pr
        return None

    def _update_body(self,intent,job,pr,marker):
        start='<!-- forge-publication:start -->';end='<!-- forge-publication:end -->'
        block=start+'\n'+intent['report']['pr_body']+'\n<!-- '+marker.replace('\n','; ')+' -->\n'+end
        body=pr.get('body') or ''
        if body.count(start)>1 or body.count(end)>1 or body.count(start)!=body.count(end):
            raise PublicationClaim('publication_pr_body_ambiguous')
        if start in body:
            first=body.index(start);last=body.index(end)+len(end)
            if last<=first:raise PublicationClaim('publication_pr_body_ambiguous')
            updated=body[:first]+block+body[last:]
        else:updated=body.rstrip()+'\n\n'+block
        payload={'body':updated}
        if len(updated.encode())>65536:raise PublicationClaim('publication_pr_body_limit',limit=65536)
        try:step=self.commands.prepare_step(intent['id'],'pr_body',payload)
        except IdempotencyConflict:raise PublicationClaim('publication_pr_body_changed') from None
        if step['status']=='confirmed':return
        if body==updated and step['attempts']:
            self.commands.record_step(intent['id'],'pr_body',{'number':pr['number']});return
        self._send(intent,job,'pr_body',payload,
            lambda:{'number':self._call(job,'PATCH','/pulls/'+str(pr['number']),payload)['number']},repeat_safe=True)

    def _read_fault(self,intent,key,error):
        if error.status in (401,403,404,422):
            self.commands.block(intent['id'],{'code':'publication_observation_refused','http_status':error.status});return
        key=key or 'read:'+str(sum(k.startswith('read:') for k in intent['steps']))
        self.commands.prepare_step(intent['id'],key,{'purpose':'publication_readback'})
        try:self.commands.claim_step(intent['id'],key,repeat_safe=True)
        except TransitionError:pass
        self.commands.uncertain(intent['id'],key)

    def _step(self,identifier):
        self.sends=0
        intent=self.commands.get(identifier)
        if intent['status'] in ('confirmed','blocked','superseded'):return self._result(identifier)
        read_fault=next((k for k,v in intent['steps'].items() if k.startswith('read:') and v['status']!='confirmed'),None)
        if read_fault:
            self.commands.uncertain(identifier,read_fault)
            if self.commands.get(identifier)['status']=='blocked':return self._result(identifier)
            if intent['steps'][read_fault]['retry_at']>self.clock():return self._result(identifier)
        reads_succeeded=False
        try:
            job=self.jobs.job(intent['job_id']);self._admit(intent,job)
            output=self.output_loader(intent['job_id'])
            if output.digest()!=intent['output_digest']:raise PublicationClaim('publication_output_changed')
            self.workflow_needed=any(path.startswith('.github/workflows/') for path in output.facts.get('protected_paths',[]))
            target=intent['target'];bot=self.github.publisher('controller')
            base=output.source['base_oid'];before=target['expected_head_oid'];parent=before or base;force=False
            marker='Forge-Publication: '+identifier+'\nForge-Output: '+output.digest()
            message=intent['report']['pr_title']+'\n\n'+marker
            if before and not self.github.contains(job,base,before):
                if target['strategy']=='graphql' or not target['authorization']['replace_ref']:
                    raise PublicationClaim('publication_base_changed')
                parent=base;force=True
            if target['pull_number'] is not None:
                existing=self._call(job,'GET','/pulls/'+str(target['pull_number']))
                if (existing.get('number')!=target['pull_number'] or not self._pr_matches(existing,job,target,
                        (existing.get('head') or {}).get('sha'),target['authorization']['author'])):
                    raise PublicationClaim('publication_pr_changed')
            if target['strategy']=='rest':
                if any(len(json.dumps({'tree':list(entries)}).encode())>6*1024*1024 for _,entries in output.trees):
                    raise PublicationClaim('publication_tree_payload_limit',limit=6*1024*1024)
                for change in output.changes:
                    if change.new_mode in (None,'160000'):continue
                    payload={'encoding':'base64','content':base64.b64encode(change.content).decode()}
                    result=self._send(intent,job,'blob:'+change.new_oid,payload,
                        lambda payload=payload:{'sha':self._call(job,'POST','/git/blobs',payload)['sha']},repeat_safe=True)
                    if result.get('sha')!=change.new_oid:raise PublicationClaim('publication_blob_changed')
                for oid,entries in output.trees:
                    payload={'tree':list(entries)}
                    result=self._send(intent,job,'tree:'+oid,payload,
                        lambda payload=payload:{'sha':self._call(job,'POST','/git/trees',payload)['sha']},repeat_safe=True)
                    if result.get('sha')!=oid:raise PublicationClaim('publication_tree_changed')
                payload={'message':message,'tree':output.tree_oid,'parents':[parent]}
                result=self._send(intent,job,'commit',payload,lambda:{'sha':self._call(job,'POST','/git/commits',payload)['sha']})
                head=result.get('sha')
                if not isinstance(head,str) or not re.fullmatch('[0-9a-f]{40}',head):raise PublicationClaim('publication_commit_unverified')
                self._commit_proof(job,head,output,parent,marker,bot)
                self._cas(intent,job,'ref',before,head,force=force)
            else:
                changes=output.graphql_changes(parent)
                if before is None:self._cas(intent,job,'initial_ref',None,base)
                payload={'branch':{'repositoryNameWithOwner':target['repository'],'branchName':target['branch']},
                         'expectedHeadOid':parent,'message':{'headline':intent['report']['pr_title'],'body':marker},'fileChanges':changes}
                step=self.commands.prepare_step(identifier,'commit',payload)
                observed=self._ref(job,target['branch'])
                if observed!=parent and step['attempts']:
                    self._commit_proof(job,observed,output,parent,marker,bot)
                    self.commands.record_step(identifier,'commit',{'sha':observed})
                    head=observed
                else:
                    if observed!=parent:raise PublicationClaim('publication_head_changed')
                    result=self._send(intent,job,'commit',payload,
                        lambda:{'sha':self._graphql(job,COMMIT,{'input':payload})['createCommitOnBranch']['commit']['oid']},repeat_safe=True)
                    head=result['sha']
                self._commit_proof(job,head,output,parent,marker,bot)
            pr=self._pull(intent,job,head,bot)
            payload={'title':intent['report']['pr_title'],'body':intent['report']['pr_body']+'\n\n<!-- '+marker.replace('\n','; ')+' -->',
                     'head':target['branch'],'base':target['base_branch'],'draft':False}
            if target['pull_number'] is None:
                self.commands.prepare_step(identifier,'pr',payload)
                if pr is None:
                    self._send(intent,job,'pr',payload,lambda:{'number':self._call(job,'POST','/pulls',payload)['number']},repeat_safe=True)
                    pr=self._pull(intent,job,head,bot)
                    if pr is None:raise _Pending()
                self.commands.record_step(identifier,'pr',{'number':pr['number']})
            else:self._update_body(intent,job,pr,marker)
            # Every confirmation re-reads commit, ref and PR after all writes.
            self._commit_proof(job,head,output,parent,marker,bot)
            if self._ref(job,target['branch'])!=head:raise PublicationClaim('publication_head_changed')
            pr=self._call(job,'GET','/pulls/'+str(pr['number']))
            author=target['authorization']['author'] if target['authorization'] else {'login':bot,'type':'Bot'}
            if not self._pr_matches(pr,job,target,head,author):raise PublicationClaim('publication_pr_changed')
            self._admit(intent,job)
            self.commands.confirm(identifier,dict(intent_id=identifier,job_id=job['id'],output_digest=output.digest(),
                repository_id=job['repo_id'],branch=target['branch'],base_oid=base,tree_oid=output.tree_oid,head_oid=head,
                app_id=target['app_id'],bundle_digest=output.bundle_digest,source_input_digest=intent['output']['source_input_digest'],
                facts_digest=intent['output']['facts_digest'],verified=True,exact_ref=True,exact_pr=True,pull_number=pr['number'],
                observed_at=self.clock(),pr_author={k:pr['user'][k] for k in ('id','login','type')}))
            reads_succeeded=True
        except PublicationClaim as error:self.commands.block(identifier,error.payload())
        except _Pending:reads_succeeded=True
        except GitHubUnavailable as error:self._read_fault(intent,read_fault,error)
        except GitHubBudgetExhausted:pass
        except TransitionError:return dict(status='deferred',intent_id=identifier,claim=None,receipt=None)
        if reads_succeeded and read_fault:self.commands.record_step(identifier,read_fault,{'observed_at':self.clock()},resume=False)
        return self._result(identifier)
