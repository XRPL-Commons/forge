"""Clear only verified completed review/audit workspace quarantines.

Original execution checkpoints remain immutable. No GitHub, checkout, issue, or
assignment mutations occur; a local acknowledgement only reopens clone admission.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import time
import subprocess

from . import execution_retirement as retirement, workflow
from .control import atomic_json
from .handoff import Unverified, bounded_run, command, _latest_role_result, _exact_audit_failure, _role_review_outcome
from .human_wait import _locked


def acknowledged(state,record,checkpoint):
    path=Path(state)/'execution-retirements'/'reconciled'/(record['id']+'.json')
    if not path.exists():return False
    value=retirement._read(path)
    proof=value.get('proof')
    if (value.get('schema')!=1 or value.get('id')!=record['id'] or value.get('checkpoint_sha256')!=record['checkpoint_sha256']
            or value.get('proof_sha256')!=retirement._digest(proof) or not isinstance(proof,dict)
            or proof.get('checkpoint_id')!=record['id'] or proof.get('generation')!=checkpoint['evidence']['generation']
            or proof.get('assignment_id')!=(checkpoint['evidence'].get('assignment') or {}).get('id')
            or proof.get('session')!=checkpoint['evidence']['session'] or proof.get('kind') not in {'verified_published_role_result','verified_operator_role_retry'}):
        raise Unverified('reconciliation_receipt_invalid')
    return True



def _policy_digest(role):
    root=Path(__file__).resolve().parent.parent
    runtime=root/'roles'
    directory=runtime if runtime.exists() else root/'legacy'/'roles'
    policy=directory/('reviewer.md' if role=='review' else 'auditor.md')
    if (directory.is_symlink() or policy.is_symlink() or not policy.is_file()
            or policy.stat().st_size>65536 or policy.resolve().parent!=directory.resolve()):
        raise Unverified('operator_retry_policy_unverified')
    return hashlib.sha256(policy.read_bytes()).hexdigest()



def _merged_checkpoint(repo,head,base,read):
    associated=read(['gh','api',f'repos/{repo.repo}/commits/{head}/pulls?per_page=10'])
    if not isinstance(associated,list) or len(associated)>=10:
        raise Unverified('reconciliation_checkout_publication_unbounded')
    candidates=[]
    for item in associated:
        number=item.get('number')
        if (type(number) is not int or number<=0 or item.get('html_url','').lower()!=f'https://github.com/{repo.repo}/pull/{number}'.lower()
                or item.get('base',{}).get('repo',{}).get('full_name','').lower()!=repo.repo.lower()):
            raise Unverified('reconciliation_checkout_publication_identity')
        if item.get('head',{}).get('sha')==head and item.get('merged_at'):
            candidates.append(number)
    if len(candidates)!=1:raise Unverified('reconciliation_checkout_publication_not_unique')
    number=candidates[0]
    pr=read(['gh','pr','view',str(number),'--repo',repo.repo,'--json','number,url,state,headRefOid,mergeCommit,author'])
    merge=(pr.get('mergeCommit') or {}).get('oid')
    if (pr.get('number')!=number or pr.get('url','').lower()!=f'https://github.com/{repo.repo}/pull/{number}'.lower()
            or pr.get('state')!='MERGED' or pr.get('headRefOid')!=head
            or pr.get('author',{}).get('login','').lower()!=repo.author.lower()
            or not isinstance(merge,str) or not re.fullmatch('[0-9a-f]{40,64}',merge)):
        raise Unverified('reconciliation_checkout_publication_identity')
    commits=[read(['gh','api',f'repos/{repo.repo}/git/commits/{sha}']) for sha in (head,merge)]
    trees=[(commit.get('tree') or {}).get('sha') for commit in commits]
    if (any(commit.get('sha')!=sha for commit,sha in zip(commits,(head,merge)))
            or not isinstance(trees[0],str) or not re.fullmatch('[0-9a-f]{40,64}',trees[0]) or trees[0]!=trees[1]):
        raise Unverified('reconciliation_checkout_merged_tree_differs')
    if merge!=base:
        comparison=read(['gh','api',f'repos/{repo.repo}/compare/{merge}...{base}',
            '--jq','{status,base_commit:{sha:.base_commit.sha},behind_by}'])
        if (comparison.get('status') not in {'ahead','identical'} or comparison.get('base_commit',{}).get('sha')!=merge
                or comparison.get('behind_by')!=0):raise Unverified('reconciliation_checkout_merge_not_reachable')
    return {'pr':number,'head':head,'merge':merge,'tree':trees[0],'verified_base':base}


def _absent(config,clone,run):
    from .config import load_repositories
    result=run(['tmux','list-sessions','-F','#{session_name}'],capture_output=True,text=True,timeout=3)
    if result.returncode==1 and result.stderr.startswith('no server running on '):names=[]
    elif result.returncode==0:names=result.stdout.splitlines()
    else:raise Unverified('reconciliation_sessions_unknown')
    for repo in load_repositories(config):
        for role in ('build','review','audit'):
            session=repo.short+'-'+role
            configured=getattr(repo,'clone_'+role)
            other=Path(configured or Path.home()/(repo.short if role=='build' else repo.short+'-'+role)).resolve()
            if other==clone and session in names:raise Unverified('reconciliation_workspace_occupied')


def _proof(config,state,key,run,*,retry_reason=None):
    root=retirement._directory(state)
    record=retirement._read(root/(key+'.json'))
    checkpoint=retirement._read(root/'checkpoints'/(key+'.json'),4*1024*1024)
    if (record.get('schema')!=1 or record.get('id')!=key or record.get('phase')!='confirmed'
            or record.get('released') is not True or retirement._digest(checkpoint)!=record.get('checkpoint_sha256')):
        raise Unverified('confirmed_execution_release_required')
    released_at=record.get('released_at')
    if type(released_at) not in (int,float) or not math.isfinite(released_at) or released_at>time.time():
        raise Unverified('reconciliation_release_time_invalid')
    e=checkpoint['evidence'];a=e.get('assignment')
    if e['role'] not in {'review','audit'} or not a or not isinstance(a.get('pr'),dict):
        raise Unverified('automatic_reconciliation_not_supported')
    repo,role,clone=retirement._configured(config,e['session'])
    if str(clone)!=e['clone'] or repo.repo.lower()!=e['repo'].lower() or role!=e['role']:
        raise Unverified('reconciliation_configuration_changed')
    state_view=workflow.read_state(state)
    assignments=[r for r in state_view['assignments'] if r['id']==a['id']]
    if len(assignments)!=1:raise Unverified('reconciliation_assignment_missing')
    saved=assignments[0]
    if saved['state']!='retired' or any(saved.get(k)!=a.get(k) for k in ('repo','role','session','generation','issue_numbers','pr','submitted_at')):
        raise Unverified('reconciliation_assignment_changed')
    if any(s.get('assignment_id') or s.get('generation') for s in state_view['slots'] if s['session']==e['session']):
        raise Unverified('reconciliation_slot_occupied')
    _absent(config,clone,run)
    git=['git','-C',str(clone)]
    if Path(command(git+['rev-parse','--show-toplevel'],run)).resolve()!=clone:raise Unverified('clone_mismatch')
    remote=command(git+['remote','get-url','origin'],run)
    if remote not in {f'git@github.com:{repo.repo}.git',f'https://github.com/{repo.repo}',f'https://github.com/{repo.repo}.git'}:
        raise Unverified('remote_mismatch')
    if command(git+['status','--porcelain=v1','--untracked-files=all'],run):raise Unverified('reconciliation_workspace_dirty')
    head=command(git+['rev-parse','HEAD'],run);branch=command(git+['rev-parse','--abbrev-ref','HEAD'],run)
    if not re.fullmatch('[0-9a-f]{40,64}',head):raise Unverified('reconciliation_head_invalid')
    if branch!=e['branch']:raise Unverified('reconciliation_branch_changed')
    def read(args):return json.loads(command(args,run))
    owners=set()
    for page in range(1,4):
        labels=read(['gh','api',f'repos/{repo.repo}/labels?per_page=100&page={page}'])
        if not isinstance(labels,list):raise Unverified('ownership_uncertain')
        owners.update(label['name'] for label in labels if label['name'].startswith('auto:owned-by-'))
        if len(labels)<100:break
    else:raise Unverified('ownership_inventory_unbounded')
    if owners!={repo.owner_label}:raise Unverified('ownership_uncertain')
    number=a['pr']['number']
    pr=read(['gh','pr','view',str(number),'--repo',repo.repo,'--json',
        'number,url,state,isDraft,headRefOid,baseRefOid,labels,author,closingIssuesReferences,reviews'])
    labels={item['name'] for item in pr['labels']}
    if (pr.get('number')!=number or pr.get('url','').lower()!=f'https://github.com/{repo.repo}/pull/{number}'.lower()
            or pr.get('author',{}).get('login','').lower()!=repo.author.lower() or pr.get('isDraft') is not False
            or pr.get('state') not in {'OPEN','MERGED'} or 'hold' in labels):raise Unverified('reconciliation_pr_changed')
    refs=pr.get('closingIssuesReferences');numbers=[]
    if not isinstance(refs,list) or not 0<len(refs)<=20:raise Unverified('reconciliation_references_unknown')
    for ref in refs:
        n=ref.get('number');r=ref.get('repository',{}).get('nameWithOwner');url=ref.get('url')
        if (type(n) is not int or n<=0 or not(r or url) or r and r.lower()!=repo.repo.lower()
                or url and url.lower()!=f'https://github.com/{repo.repo}/issues/{n}'.lower()):
            raise Unverified('reconciliation_references_changed')
        numbers.append(n)
    if sorted(set(numbers))!=a['issue_numbers']:raise Unverified('reconciliation_references_changed')
    reviews=pr.get('reviews')
    if not isinstance(reviews,list) or len(reviews)>=100:raise Unverified('reconciliation_reviews_unbounded')
    # The shared parser scopes to the exact assigned revision. Later revisions
    # cannot erase a published result from this retired execution.
    policy_sha=None
    if retry_reason is not None:
        expected='ready-for-review' if role=='review' else 'ready-for-audit'
        if (pr['state']!='OPEN' or labels & {'ready-for-review','ready-for-audit','changes-requested'}!={expected}
                or role=='audit' and 'critical' in labels):raise Unverified('operator_retry_target_not_ready')
        policy_sha=_policy_digest(role)
        submitted=None;review={'id':None,'body':''};outcome='retry'
    else:
        old=[r for r in reviews if r.get('commit',{}).get('oid')==a['pr']['head']]
        submitted,review,outcome=_latest_role_result(old,role,repo.author,a,{'session_created':e['session_created']},time.time())
        if outcome not in {'pass','fail'} or submitted>record['released_at']:
            raise Unverified('reconciliation_role_result_unverified')
        if role=='audit':
            exact=all(re.search(r'(?<![0-9a-f])'+re.escape(a['pr'][k])+r'(?![0-9a-f])',review.get('body','')) for k in ('head','base'))
            if not exact or outcome=='fail' and not _exact_audit_failure(review,outcome,a):
                raise Unverified('reconciliation_audit_revision_unverified')
            if outcome=='pass' and pr['state']!='MERGED':raise Unverified('reconciliation_audit_not_finalized')
    current_head=pr.get('headRefOid');current_base=pr.get('baseRefOid')
    if any(not isinstance(v,str) or not re.fullmatch('[0-9a-f]{40,64}',v) for v in (current_head,current_base)):
        raise Unverified('reconciliation_pr_revision_invalid')
    if role=='audit' and pr['state']=='OPEN' and 'critical' in labels:
        current_results=[_role_review_outcome(r,current_head) for r in reviews
                         if r.get('commit',{}).get('oid')==current_head and r.get('author',{}).get('login','').lower()==repo.author.lower()]
        if ('audit','pass') in current_results:raise Unverified('human_approval_still_required')
    if retry_reason is None and pr['state']=='OPEN' and current_head==a['pr']['head']:
        expected='ready-for-audit' if outcome=='pass' else 'changes-requested'
        if labels & {'ready-for-review','ready-for-audit','changes-requested'}!={expected}:raise Unverified('reconciliation_role_outcome_not_applied')
    if retry_reason is not None and head!=e['head']:raise Unverified('operator_retry_checkpoint_changed')
    checkout_basis='canonical_candidate_commit';checkout_publication=None
    if head not in {a['pr']['head'],a['pr']['base'],current_head,current_base}:
        history=read(['gh','api',f'repos/{repo.repo}/compare/{head}...{current_base}',
            '--jq','{status,base_commit:{sha:.base_commit.sha},behind_by}'])
        if (history.get('status') not in {'ahead','identical'} or history.get('base_commit',{}).get('sha')!=head
                or history.get('behind_by')!=0):
            checkout_publication=_merged_checkpoint(repo,head,current_base,read)
            checkout_basis='exact_tree_of_verified_merged_pr'
        else:checkout_basis='ancestor_of_canonical_candidate_base'
    commit=read(['gh','api',f'repos/{repo.repo}/git/commits/{head}'])
    if commit.get('sha')!=head:raise Unverified('reconciliation_checkout_unpublished')
    if head!=e['head']:
        compare=read(['gh','api',f'repos/{repo.repo}/compare/{e["head"]}...{head}',
            '--jq','{status,base_commit:{sha:.base_commit.sha},behind_by}'])
        if compare.get('status') not in {'ahead','identical'} or compare.get('base_commit',{}).get('sha')!=e['head'] or compare.get('behind_by')!=0:
            raise Unverified('reconciliation_checkout_diverged')
    if (command(git+['remote','get-url','origin'],run)!=remote or command(git+['rev-parse','HEAD'],run)!=head
            or command(git+['rev-parse','--abbrev-ref','HEAD'],run)!=branch
            or command(git+['status','--porcelain=v1','--untracked-files=all'],run)):
        raise Unverified('reconciliation_workspace_changed')
    current_view=workflow.read_state(state)
    current_assignment=[r for r in current_view['assignments'] if r['id']==a['id']]
    if current_assignment!=assignments or any(s.get('assignment_id') or s.get('generation') for s in current_view['slots'] if s['session']==e['session']):
        raise Unverified('reconciliation_assignment_changed')
    _absent(config,clone,run);run.remaining()
    return {'kind':'verified_operator_role_retry' if retry_reason is not None else 'verified_published_role_result',
        'source':'operator_retry' if retry_reason is not None else 'automatic_result','retry_reason':retry_reason,'policy_sha256':policy_sha,'checkpoint_id':key,'generation':e['generation'],
        'assignment_id':a['id'],'session':e['session'],'repo':repo.repo,'role':role,'pr':number,
        'assigned_head':a['pr']['head'],'assigned_base':a['pr']['base'],'current_head':current_head,'current_base':current_base,
        'current_labels':sorted(labels),'review_id':review['id'],'review_sha256':hashlib.sha256(review.get('body','').encode()).hexdigest(),
        'outcome':outcome,'checkout_basis':checkout_basis,'checkout_publication':checkout_publication,'published_at':submitted,'head':head,'branch':branch,'checkpoint_sha256':record['checkpoint_sha256']}


def reconcile(config,state,session,*,lock_fd=8,run=subprocess.run,retry_checkpoint=None,retry_reason=None):
    if not _locked(state,lock_fd):return {'reconciled':False,'reason':'supervisor_lock_required'}
    try:
        rows=[r for r in retirement.read_quarantines(state) if r['session']==session]
        if len(rows)!=1:raise Unverified('reconciliation_checkpoint_not_unique')
        key=rows[0]['id'];proof_run=bounded_run(run,40)
        if retry_checkpoint is not None:
            if retry_checkpoint!=key or not isinstance(retry_reason,str) or not 0<len(retry_reason.strip())<=500:
                raise Unverified('operator_retry_exact_checkpoint_required')
        elif retry_reason is not None:raise Unverified('operator_retry_exact_checkpoint_required')
        first=_proof(config,state,key,proof_run,retry_reason=retry_reason);second=_proof(config,state,key,proof_run,retry_reason=retry_reason)
        if first!=second:raise Unverified('reconciliation_evidence_changed')
        if not _locked(state,lock_fd):raise Unverified('supervisor_lock_required')
        proof_run.remaining()
        from .workflow_results import from_reconciliation, record
        record(state,from_reconciliation(first))
        path=retirement._directory(state)/'reconciled';path.mkdir(mode=0o700,exist_ok=True)
        if path.is_symlink() or path.stat().st_mode & 0o077:raise Unverified('reconciliation_directory_not_private')
        atomic_json(path/(key+'.json'),{'schema':1,'id':key,'checkpoint_sha256':first['checkpoint_sha256'],
            'proof':first,'proof_sha256':retirement._digest(first),'reconciled_at':time.time()})
        return {'reconciled':True,'reason':first['kind'],'id':key}
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        return {'reconciled':False,'reason':str(exc) if isinstance(exc,Unverified) else 'reconciliation_evidence_unavailable'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['reconcile']);parser.add_argument('--config',required=True)
    parser.add_argument('--state',required=True);parser.add_argument('--session',required=True);parser.add_argument('--lock-fd',type=int,default=8)
    parser.add_argument('--retry-checkpoint');parser.add_argument('--reason')
    args=parser.parse_args();result=reconcile(args.config,args.state,args.session,lock_fd=args.lock_fd,
        retry_checkpoint=args.retry_checkpoint,retry_reason=args.reason)
    print(json.dumps(result));return 0 if result['reconciled'] else 1


if __name__=='__main__':raise SystemExit(main())
