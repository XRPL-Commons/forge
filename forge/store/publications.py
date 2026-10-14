"""Before-candidate publication ledger. Only host read-back confirms delivery."""
from copy import deepcopy
import hashlib
import json
import math
import re
import time

from ..factory import source_inputs
from ..publication_artifacts import VerifiedOutput
from .effects import admit_send_in, retry_policy
from .executions import ExecutionQueries
from .models import Command, IdempotencyConflict, TransitionError
from .runtime import writable
from .transitions import Lifecycle, _id, _json, _unit, applicable_gates


def target_identity(value):
    fields={'repository_id','repository','branch','base_branch','expected_head_oid','pull_number','app_id','strategy','signing_qualified','authorization'}
    if not isinstance(value,dict) or set(value)!=fields:raise TransitionError('Publication target has unsupported fields')
    if any(type(value[key]) is not int or value[key]<=0 for key in ('repository_id','app_id')):raise TransitionError('Publication requires repository and App identity')
    if not isinstance(value['repository'],str) or not source_inputs.REPOSITORY.fullmatch(value['repository']):raise TransitionError('Publication repository is invalid')
    for field in ('branch','base_branch'):
        ref=value[field]
        if (not isinstance(ref,str) or not ref or len(ref)>240 or ref.startswith(('-','/')) or ref.endswith(('/','.','.lock'))
                or any(c in ref for c in ('..','@{','//','\\','~','^',':','?','*','[')) or any(ord(c)<=32 or ord(c)==127 for c in ref)):
            raise TransitionError('Publication branch is invalid')
    if value['branch']==value['base_branch']:raise TransitionError('Publisher cannot write the target branch')
    if value['expected_head_oid'] is not None and not re.fullmatch('[0-9a-f]{40}',str(value['expected_head_oid'])):raise TransitionError('Publication requires an exact prior head')
    if value['pull_number'] is not None and (type(value['pull_number']) is not int or value['pull_number']<=0):raise TransitionError('Publication PR identity is invalid')
    if value['strategy'] not in ('pending','rest','graphql') or type(value['signing_qualified']) is not bool:raise TransitionError('Publication strategy is unqualified')
    authorization=value['authorization']
    if authorization is not None and (not isinstance(authorization,dict) or set(authorization)!={'kind','branch','expected_head_oid','pull_number','author','replace_ref'}
            or authorization['kind'] not in ('forge_owned','ownership_transfer') or type(authorization['replace_ref']) is not bool
            or any(authorization[key]!=value[key] for key in ('branch','expected_head_oid','pull_number'))):
        raise TransitionError('Publication ownership authorization is invalid')
    if authorization is not None:
        author=authorization['author']
        if (not isinstance(author,dict) or set(author)!={'id','login','type'} or type(author['id']) is not int or author['id']<=0
                or not isinstance(author['login'],str) or not author['login'] or author['type'] not in ('Bot','User')):
            raise TransitionError('Publication author identity is invalid')
    if (value['expected_head_oid'] is not None or value['pull_number'] is not None) and authorization is None:
        raise TransitionError('Existing publication requires recorded ownership authorization')
    return dict(value)


class Publications(Lifecycle):
    def __init__(self,store,*,clock=time.time,reliability=None):
        super().__init__(store,clock=clock)
        self.policy=retry_policy(reliability)
        self.reliability=reliability or {}

    def _ready(self,db,row):
        unit=_unit(db,row['unit_id'])
        job,current=self._job(db,unit,row['job_id'],row['execution_fence'],json.loads(row['input_manifest_json']))
        if not current or job['role']!='build' or not job['cleanup']:raise TransitionError('Publication no longer owns a cleaned build')
        if db.execute('SELECT 1 FROM job_tasks WHERE job_id=?',(job['id'],)).fetchone():raise TransitionError('Child tasks cannot publish')
        builder=db.execute('SELECT stage FROM builder_assignments WHERE job_id=?',(job['id'],)).fetchone()
        if builder and builder[0]!='integration':raise TransitionError('Only final integration can publish')
        for gate in applicable_gates(db,unit,job['member_id'],job['candidate_id'],job_id=job['id']):
            if gate['code']!='result_pending' or json.loads(gate['details_json']).get('job_id')!=job['id']:
                raise TransitionError('An independent gate blocks publication')
        return job,unit

    def queue(self,command,job_id,*,execution_fence,input_manifest,output,target,report):
        if not isinstance(output,VerifiedOutput):raise TransitionError('Publisher requires verified local output')
        target=target_identity(target);identity=output.identity()
        if (not isinstance(report,dict) or report.get('outcome') not in ('pass','needs_human_verification')
                or report.get('attempt')!=job_id or report.get('role')!='build'
                or any(not isinstance(report.get(key),str) or not report[key].strip() for key in ('pr_title','pr_body'))
                or len(report['pr_title'])>256 or len(_json(report).encode())>65536):
            raise TransitionError('Publication requires a bounded candidate-eligible report')
        query=ExecutionQueries(self.store);launch=query.job(job_id);capture=query.capture(job_id)
        from ..factory.contracts import result as parse_result
        try:
            retained_report=parse_result(deepcopy(capture.get('result')),launch)
            supplied_report=parse_result(deepcopy(report),launch)
        except (ValueError,TypeError,KeyError):raise TransitionError('Publication report is invalid') from None
        if (retained_report!=supplied_report or report.get('output_commit')!=output.head_oid
                or hashlib.sha256(query.bundle(job_id)).hexdigest()!=output.bundle_digest):
            raise TransitionError('Publication report or bundle differs from retained capture')
        report=retained_report
        export=dict(version=1,job_id=job_id,input_manifest_hash=output.source['manifest_digest'],base_oid=output.source['base_oid'],
                    local_head_oid=output.head_oid,object_format=output.source['object_format'],source_digest=source_inputs.digest(output.source),
                    bundle_digest=output.bundle_digest,bundle_size=output.bundle_size)
        if capture.get('export_receipt')!=export:raise TransitionError('Publication lacks its trusted export receipt')
        if target['strategy']=='pending' or not target['signing_qualified']:
            raise TransitionError('Publication signing strategy is not qualified')
        if (source_inputs.digest(input_manifest)!=output.source['manifest_digest']
                or (capture.get('bundle') or {}).get('digest')!=output.bundle_digest
                or target['repository_id']!=output.source['repository_id'] or target['repository']!=output.source['canonical_repository']
                or target['repository_id']!=launch['repo_id'] or target['branch']!=launch['branch']
                or target['base_branch']!=launch['target_branch']):
            raise TransitionError('Publication differs from its retained assignment')
        # Shared launch wiring pins source_input; a caller cannot substitute a
        # new policy/notes identity merely because the base commit is unchanged.
        if launch.get('source_input')!=output.source:raise TransitionError('Publication source input was not reserved')
        expected=(launch.get('resume_head') or (launch.get('candidate') or {}).get('head')
                  or (input_manifest.get('previous_publication') or {}).get('head_oid'))
        expected_pr=(launch.get('resume_pr') or (launch.get('candidate') or {}).get('pr')
                     or (input_manifest.get('previous_publication') or {}).get('pull_number'))
        authorization=launch.get('publication_authorization')
        if authorization:
            expected=authorization['expected_head_oid'];expected_pr=authorization['pull_number']
        if (target['expected_head_oid']!=expected or target['pull_number']!=expected_pr
                or target['authorization']!=authorization):raise TransitionError('Publication target was not reserved')
        if len(_json(identity).encode())>2097152:raise TransitionError('Publication identity exceeds bound')
        payload=dict(job_id=job_id,execution_fence=execution_fence,input_manifest=input_manifest,
                     output_digest=output.digest(),target=target,report=report)
        def change(db,unit):
            existing=db.execute('SELECT * FROM publication_intents WHERE job_id=?',(job_id,)).fetchone()
            if existing:
                if (existing['output_digest']!=output.digest() or existing['target_json']!=_json(target)
                        or existing['report_json']!=_json(report)):raise IdempotencyConflict('Job already has different publication intent')
                return {'intent_id':existing['id']}
            row=dict(unit_id=unit['id'],job_id=job_id,execution_fence=execution_fence,input_manifest_json=_json(input_manifest))
            self._ready(db,row)
            identifier=_id(command.request_id,'publication')
            db.execute("INSERT INTO publication_intents(id,job_id,unit_id,execution_fence,input_manifest_json,output_digest,output_json,target_json,report_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?)",
                (identifier,job_id,unit['id'],execution_fence,_json(input_manifest),output.digest(),_json(identity),_json(target),_json(report),self.clock(),self.clock()))
            return {'intent_id':identifier}
        return self._apply(command,'queue_publication',payload,change)

    def remember_claim(self,job_id,claim):
        """Retain a deterministic pre-intent refusal for steward/operator views."""
        if not isinstance(claim,dict) or not isinstance(claim.get('code'),str) or len(_json(claim).encode())>8192:
            raise TransitionError('Publication claim exceeds its contract')
        request_id='publication-claim:'+job_id+':'+source_inputs.digest(claim)
        with self.store.read_snapshot() as db:
            prior=db.execute('SELECT result_json FROM decisions WHERE request_id=?',(request_id,)).fetchone()
            if prior:return json.loads(prior[0])
        job=ExecutionQueries(self.store).job(job_id)
        command=Command(request_id,'supervisor:publisher',job['unit_id'],job['unit_version'])
        def change(db,unit):
            self._ready(db,dict(unit_id=unit['id'],job_id=job_id,execution_fence=job['execution_fence'],input_manifest_json=_json(job['input_manifest'])))
            return {'job_id':job_id,'claim':claim,'charged':False}
        return self._apply(command,'publication_claim',{'job_id':job_id,'source_input':job.get('source_input'),'claim':claim},change)

    def get(self,identifier):
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT * FROM publication_intents WHERE id=?',(identifier,)).fetchone()
            if row is None:raise KeyError(identifier)
            value=dict(row)
            for key in ('input_manifest','output','target','report','claim'):value[key]=json.loads(value.pop(key+'_json') or 'null')
            value['steps']={item['key']:{**dict(item),'receipt':json.loads(item['receipt_json'] or 'null')}
                for item in db.execute('SELECT * FROM publication_steps WHERE intent_id=?',(identifier,))}
            receipt=db.execute('SELECT receipt_json FROM publication_receipts WHERE intent_id=?',(identifier,)).fetchone()
            value['receipt']=json.loads(receipt[0]) if receipt else None
            return value

    def pending(self,*,limit=20):
        with self.store.read_snapshot() as db:
            return [row[0] for row in db.execute("SELECT id FROM publication_intents WHERE status IN('pending','working','uncertain') ORDER BY updated_at,id LIMIT ?",(limit,))]

    def admitted(self,identifier):
        with self.store.read_snapshot() as db:
            row=db.execute('SELECT * FROM publication_intents WHERE id=?',(identifier,)).fetchone()
            if row is None:raise KeyError(identifier)
            self._ready(db,row)
            return True

    def prepare_step(self,identifier,key,request):
        digest=source_inputs.digest(request)
        with self.store.write() as db:
            writable(db)
            row=db.execute('SELECT * FROM publication_steps WHERE intent_id=? AND key=?',(identifier,key)).fetchone()
            if row:
                if row['request_digest']!=digest:raise IdempotencyConflict('Publication step request changed')
                return dict(row)
            db.execute("INSERT INTO publication_steps(intent_id,key,request_digest,status,created_at,updated_at) VALUES(?,?,?,'prepared',?,?)",(identifier,key,digest,self.clock(),self.clock()))
        return self.get(identifier)['steps'][key]

    def claim_step(self,identifier,key,*,repeat_safe=False):
        with self.store.write() as db:
            writable(db)
            intent=db.execute('SELECT * FROM publication_intents WHERE id=?',(identifier,)).fetchone()
            if intent is None or intent['status'] not in ('pending','working','uncertain'):raise TransitionError('Publication is not runnable')
            self._ready(db,intent)
            row=db.execute('SELECT * FROM publication_steps WHERE intent_id=? AND key=?',(identifier,key)).fetchone()
            if row is None or row['status']=='confirmed':raise TransitionError('Publication step is not pending')
            if row['status']=='sending' and not repeat_safe:raise TransitionError('Uncertain publication requires read-back before another send')
            admit_send_in(row,self.clock(),reliability=self.reliability)
            delay=min(self.policy['retry_cap'],self.policy['retry_seconds']*2**min(row['attempts'],20))
            db.execute("UPDATE publication_steps SET status='sending',attempts=attempts+1,retry_at=?,uncertain_since=COALESCE(uncertain_since,?),updated_at=? WHERE intent_id=? AND key=?",
                (self.clock()+delay,self.clock(),self.clock(),identifier,key))
            db.execute("UPDATE publication_intents SET status='working',version=version+1,updated_at=? WHERE id=?",(self.clock(),identifier))

    def record_step(self,identifier,key,receipt,*,resume=True):
        if not isinstance(receipt,dict) or len(_json(receipt).encode())>65536:raise TransitionError('Publication step needs bounded read-back')
        with self.store.write() as db:
            writable(db)
            row=db.execute('SELECT * FROM publication_steps WHERE intent_id=? AND key=?',(identifier,key)).fetchone()
            if row is None:raise TransitionError('Publication step was not prepared')
            if row['status']=='confirmed':
                if row['receipt_json']!=_json(receipt):raise IdempotencyConflict('Publication receipt changed')
                return
            db.execute("UPDATE publication_steps SET status='confirmed',receipt_json=?,retry_at=NULL,updated_at=? WHERE intent_id=? AND key=?",(_json(receipt),self.clock(),identifier,key))
            if resume:db.execute("UPDATE publication_intents SET status='pending',version=version+1,updated_at=? WHERE id=? AND status NOT IN('superseded','confirmed')",(self.clock(),identifier))

    def refused_step(self,identifier,key):
        """A definite rate-limit refusal can retry after its durable backoff."""
        with self.store.write() as db:
            writable(db)
            db.execute("UPDATE publication_steps SET status='refused',updated_at=? WHERE intent_id=? AND key=? AND status='sending'",
                       (self.clock(),identifier,key))
        return self.uncertain(identifier,key)

    def uncertain(self,identifier,key):
        with self.store.write() as db:
            writable(db)
            row=db.execute('SELECT * FROM publication_steps WHERE intent_id=? AND key=?',(identifier,key)).fetchone()
            if row is None:raise TransitionError('Publication step was not sent')
            exhausted=row['attempts']>=self.policy['max_attempts'] or self.clock()-(row['uncertain_since'] if row['uncertain_since'] is not None else self.clock())>=self.policy['uncertainty_seconds']
            status='blocked' if exhausted else 'uncertain'
            claim=_json({'code':'publication_uncertain','step':key}) if exhausted else None
            db.execute('UPDATE publication_intents SET status=?,claim_json=?,version=version+1,updated_at=? WHERE id=?',(status,claim,self.clock(),identifier))
            if exhausted:db.execute('UPDATE publication_steps SET escalation_at=? WHERE intent_id=? AND key=?',(self.clock(),identifier,key))
        return {'status':status}

    def block(self,identifier,claim):
        if not isinstance(claim,dict) or len(_json(claim).encode())>8192:raise TransitionError('Publication claim exceeds bound')
        with self.store.write() as db:
            writable(db)
            db.execute("UPDATE publication_intents SET status='blocked',claim_json=?,version=version+1,updated_at=? WHERE id=? AND status!='confirmed'",(_json(claim),self.clock(),identifier))
        return {'status':'blocked','claim':claim}

    def recheck(self,command,identifier,*,evidence):
        """Fresh capability repair preserves every uncertain mutation receipt."""
        def change(db,unit):
            row=db.execute('SELECT * FROM publication_intents WHERE id=? AND unit_id=?',(identifier,unit['id'])).fetchone()
            if row is None:raise TransitionError('Publication belongs to another unit')
            self._ready(db,row)
            target=json.loads(row['target_json']);output=json.loads(row['output_json']);claim=json.loads(row['claim_json'] or 'null') or {}
            access_refusal=claim.get('code')=='publication_refused' and claim.get('http_status') in (401,403,404)
            capability_fault=claim.get('code') in ('publication_signing_unqualified','publication_capability_changed','publication_app_changed')
            if row['status']!='blocked' or not (access_refusal or capability_fault):
                raise TransitionError('Publication requires a content or uncertainty decision')
            required={'contents':'write','pull_requests':'write'}
            if any(path.startswith('.github/workflows/') for path in output['facts'].get('protected_paths',[])):required['workflows']='write'
            if (not isinstance(evidence,dict) or evidence.get('source')!='host_app_installation'
                    or evidence.get('repository_id')!=target['repository_id'] or evidence.get('app_id')!=target['app_id']
                    or evidence.get('source_input_digest')!=output['source_input_digest'] or evidence.get('output_digest')!=row['output_digest']
                    or evidence.get('strategy')!=target['strategy'] or evidence.get('signing_qualified') is not True
                    or any((evidence.get('permissions') or {}).get(k)!=v for k,v in required.items())
                    or type(evidence.get('observed_at')) not in (int,float) or not self.clock()-60<=evidence['observed_at']<=self.clock()):
                raise TransitionError('Publication repair requires fresh matching App access')
            if access_refusal:
                step=db.execute('SELECT * FROM publication_steps WHERE intent_id=? AND key=?',(identifier,claim['step'])).fetchone()
                if step is None or step['status']=='confirmed' or step['attempts']>=self.policy['max_attempts']:
                    raise TransitionError('Publication request is not retryable')
            key='repair:'+command.request_id
            db.execute("INSERT INTO publication_steps(intent_id,key,request_digest,status,receipt_json,created_at,updated_at) VALUES(?,?,?,'confirmed',?,?,?)",
                       (identifier,key,source_inputs.digest(evidence),_json(evidence),self.clock(),self.clock()))
            if access_refusal:
                db.execute("UPDATE publication_steps SET status='refused',retry_at=NULL,uncertain_since=NULL,escalation_at=NULL,updated_at=? WHERE intent_id=? AND key=?",
                           (self.clock(),identifier,claim['step']))
            db.execute("UPDATE publication_intents SET status='pending',claim_json=NULL,version=version+1,updated_at=? WHERE id=?",(self.clock(),identifier))
            return {'intent_id':identifier,'status':'pending'}
        return self._apply(command,'recheck_publication',{'intent_id':identifier,'evidence':evidence},change)

    def confirm(self,identifier,receipt):
        with self.store.write() as db:
            writable(db)
            row=db.execute('SELECT * FROM publication_intents WHERE id=?',(identifier,)).fetchone()
            if row is None:raise KeyError(identifier)
            prior=db.execute('SELECT receipt_json FROM publication_receipts WHERE intent_id=?',(identifier,)).fetchone()
            if prior:
                if json.loads(prior[0])!=receipt:raise IdempotencyConflict('Publication confirmation changed')
                return
            self._ready(db,row)
            output=json.loads(row['output_json']);target=json.loads(row['target_json'])
            expected=dict(intent_id=identifier,job_id=row['job_id'],output_digest=row['output_digest'],
                          repository_id=target['repository_id'],branch=target['branch'],base_oid=output['source_input']['base_oid'],
                          tree_oid=output['tree_oid'],app_id=target['app_id'],bundle_digest=output['bundle_digest'],
                          source_input_digest=output['source_input_digest'],facts_digest=output['facts_digest'])
            if (not isinstance(receipt,dict) or any(receipt.get(k)!=v for k,v in expected.items())
                    or receipt.get('verified') is not True or receipt.get('exact_ref') is not True or receipt.get('exact_pr') is not True
                    or not re.fullmatch('[0-9a-f]{40}',str(receipt.get('head_oid')))
                    or type(receipt.get('pull_number')) is not int or receipt['pull_number']<=0
                    or type(receipt.get('observed_at')) not in (int,float) or not math.isfinite(receipt['observed_at'])
                    or not self.clock()-60<=receipt['observed_at']<=self.clock()):
                raise TransitionError('Publication receipt does not prove the exact candidate')
            if target['pull_number'] is not None and receipt['pull_number']!=target['pull_number']:raise TransitionError('Publication changed its approved PR')
            prior=db.execute('SELECT receipt_json FROM publication_receipts WHERE intent_id=?',(identifier,)).fetchone()
            if prior:
                if json.loads(prior[0])!=receipt:raise IdempotencyConflict('Publication confirmation changed')
                return
            db.execute('INSERT INTO publication_receipts VALUES(?,?,?)',(identifier,_json(receipt),self.clock()))
            db.execute("UPDATE publication_intents SET status='confirmed',version=version+1,updated_at=? WHERE id=?",(self.clock(),identifier))


def require_receipt_in(db,job,candidate,evidence):
    """Source-bound builds can select only their independently confirmed tree."""
    source=db.execute('SELECT source_digest,execution_fence FROM mirror_job_inputs WHERE job_id=?',(job['id'],)).fetchone()
    if source is None:return None  # A job launched before the source boundary.
    identifier=evidence.get('publication_receipt_id') if isinstance(evidence,dict) else None
    row=db.execute('SELECT i.*,r.receipt_json FROM publication_intents i JOIN publication_receipts r ON r.intent_id=i.id '
                   "WHERE i.id=? AND i.job_id=? AND i.status='confirmed'",(identifier,job['id'])).fetchone()
    if row is None:raise TransitionError('Source-bound publication requires its confirmed receipt')
    receipt=json.loads(row['receipt_json']);output=json.loads(row['output_json'])
    if (row['execution_fence']!=source['execution_fence'] or row['input_manifest_json']!=job['input_manifest_json']
            or receipt.get('source_input_digest')!=source['source_digest']
            or any(receipt.get(key)!=getattr(candidate,key) for key in ('pull_number','branch','head_oid','base_oid'))
            or receipt.get('tree_oid')!=output['tree_oid'] or receipt.get('verified') is not True
            or receipt.get('exact_ref') is not True or receipt.get('exact_pr') is not True):
        raise TransitionError('Publication receipt differs from the assigned candidate')
    return receipt
