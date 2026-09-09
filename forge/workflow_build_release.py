"""Verify a tracked builder's publication independently of other queued work."""
import json
import math
from pathlib import Path
import re
import time

from .config import load_repositories
from .control import snapshot_path
from .workflow import read_state

SHA = re.compile(r'[0-9a-f]{40,64}')


def _rejected_after_build(pr, builder, view, author, now):
    """A rejection after this build's publication does not undo its handoff.

    An unchanged PR rejected before the builder started is still unfinished
    rework. Treating that as a handoff would create endless no-progress launches.
    """
    from .handoff import _latest_role_result, _exact_audit_failure, Unverified
    reviews=pr.get('reviews')
    if not isinstance(reviews,list) or len(reviews)>=100:return False
    candidate={'number':pr['number'],'head':pr['headRefOid'],'base':pr['baseRefOid']}
    attempts=[a for a in view['assignments'] if a.get('role') in {'review','audit'} and a.get('pr')==candidate
              and a.get('submitted_at') is not None and a['submitted_at']>=builder['submitted_at']]
    if not attempts:return False
    latest=max(attempts,key=lambda a:(a['submitted_at'],a['role']=='audit'))
    try:
        at,review,outcome=_latest_role_result(reviews,latest['role'],author,latest,
                                            {'session_created':int(latest['generation'].split('|')[1])},now)
        return (outcome=='fail' and at>=builder['submitted_at']
                and (latest['role']!='audit' or _exact_audit_failure(review,outcome,latest)))
    except (Unverified,ValueError,TypeError,KeyError,AttributeError):return False


def check(config,state,session,run,now,*,unverified,read_command,identify_session):
    if not session.endswith('-build'):
        return None
    view=read_state(state)
    slot=next((s for s in view['slots'] if s['session']==session),None)
    if not slot or not slot.get('assignment_id'):
        return None
    def refuse(reason):raise unverified(reason)
    assignments=[a for a in view['assignments'] if a['id']==slot['assignment_id']]
    if len(assignments)!=1:refuse('tracked_build_assignment_missing')
    assignment=assignments[0]
    repositories=[r for r in load_repositories(config) if r.enabled and r.short+'-build'==session]
    if len(repositories)!=1:refuse('not_enabled_worker')
    repo=repositories[0]
    if (assignment['repo']!=repo.repo.lower() or assignment['role']!='build'
            or assignment['session']!=session or assignment.get('submitted_at') is None
            or assignment['state'] not in {'submitted','working','blocked','handoff'}):
        refuse('tracked_build_not_completed_execution')
    numbers=assignment['issue_numbers']
    if not numbers or len(numbers)>20 or any(type(n) is not int or n<=0 for n in numbers):
        refuse('tracked_build_target_invalid')
    began=time.monotonic()
    path=snapshot_path(state,repo.repo);snapshot=json.loads(path.read_text());at=snapshot.get('observed_at')
    if (snapshot.get('repo')!=repo.repo or snapshot.get('status')!='ok' or snapshot.get('enabled') is not True
            or type(at) not in (int,float) or not math.isfinite(at) or not 0<=now-at<=300):
        refuse('snapshot_not_fresh')
    identity=identify_session(session,run)
    generation='|'.join(str(identity[k]) for k in ('session_id','session_created','pane_id','pane_pid'))
    if assignment['generation']!=generation or slot.get('generation')!=generation:
        refuse('tracked_build_generation_changed')
    clone=Path(repo.clone_build or Path.home()/repo.short).resolve();git=['git','-C',str(clone)]
    command=lambda args:read_command(args,run)
    if Path(command(git+['rev-parse','--show-toplevel'])).resolve()!=clone:refuse('clone_mismatch')
    remote=command(git+['remote','get-url','origin'])
    if remote not in {f'git@github.com:{repo.repo}.git',f'https://github.com/{repo.repo}.git',f'https://github.com/{repo.repo}'}:
        refuse('remote_mismatch')
    if command(git+['status','--porcelain=v1','--untracked-files=all']):refuse('worktree_not_clean')
    head=command(git+['rev-parse','HEAD'])
    if not SHA.fullmatch(head):refuse('invalid_head')
    if assignment.get('pr'):
        candidates=[assignment['pr']['number']]
    else:
        linked=json.loads(command(['gh','api',f'repos/{repo.repo}/commits/{head}/pulls?per_page=11']))
        if not isinstance(linked,list) or len(linked)>10:refuse('tracked_build_pr_inventory_unbounded')
        candidates=[p.get('number') for p in linked if
            (p.get('base') or {}).get('repo',{}).get('full_name','').lower()==repo.repo.lower()]
    if not candidates or any(type(n) is not int or n<=0 for n in candidates):
        refuse('tracked_build_published_pr_missing')
    verified=[]
    for number in sorted(set(candidates)):
        pr=json.loads(command(['gh','pr','view',str(number),'--repo',repo.repo,'--json',
            'number,url,state,isDraft,headRefOid,baseRefOid,closingIssuesReferences,labels,author,mergeCommit,mergedAt,reviews']))
        if (pr.get('number')!=number or pr.get('url','').lower()!=f'https://github.com/{repo.repo}/pull/{number}'.lower()
                or pr.get('author',{}).get('login','').lower()!=repo.author.lower()
                or pr.get('isDraft') is not False or not SHA.fullmatch(pr.get('headRefOid',''))
                or not SHA.fullmatch(pr.get('baseRefOid',''))):continue
        labels={label['name'] for label in pr['labels']}
        if 'hold' in labels:continue
        refs=pr.get('closingIssuesReferences');linked=set();valid=isinstance(refs,list) and 0<len(refs)<=20
        for ref in refs if valid else []:
            n=ref.get('number');owner=(ref.get('repository') or {}).get('nameWithOwner');url=ref.get('url')
            if (type(n) is not int or n<=0 or not (owner or url)
                    or owner and owner.lower()!=repo.repo.lower()
                    or url and url.lower()!=f'https://github.com/{repo.repo}/issues/{n}'.lower()):valid=False;break
            linked.add(n)
        if not valid or linked!=set(numbers):continue
        merge=(pr.get('mergeCommit') or {}).get('oid')
        if pr['state']=='OPEN':
            stages=labels & {'ready-for-review','ready-for-audit','changes-requested'}
            if stages not in ({'ready-for-review'},{'ready-for-audit'},{'changes-requested'}) or head!=pr['headRefOid']:continue
            if stages=={'changes-requested'} and not _rejected_after_build(pr,assignment,view,repo.author,now):continue
            publication='current_published_pr_head_rejected' if stages=={'changes-requested'} else 'current_published_pr_head'
        elif pr['state']=='MERGED' and pr.get('mergedAt') and isinstance(merge,str) and SHA.fullmatch(merge):
            if head!=merge:
                if head!=pr['headRefOid']:continue
                commit=json.loads(command(['gh','api',f'repos/{repo.repo}/git/commits/{merge}']))
                if commit.get('sha')!=merge or (commit.get('tree') or {}).get('sha')!=command(git+['rev-parse',head+'^{tree}']):continue
            main=json.loads(command(['gh','api',f'repos/{repo.repo}/git/ref/heads/main']))['object']['sha']
            if not isinstance(main,str) or not SHA.fullmatch(main):refuse('invalid_published_main')
            comparison=json.loads(command(['gh','api',f'repos/{repo.repo}/compare/{merge}...{main}',
                '--jq','{status,base_commit:{sha:.base_commit.sha},behind_by}']))
            if comparison.get('status') not in {'ahead','identical'} or comparison.get('base_commit',{}).get('sha')!=merge or comparison.get('behind_by')!=0:continue
            publication='merged_pr_on_published_main'
        else:continue
        verified.append({'pr':number,'pr_head':pr['headRefOid'],'pr_base':pr['baseRefOid'],
                         'merge':merge if pr['state']=='MERGED' else None,'checkout_basis':publication})
    if len(verified)!=1:refuse('tracked_build_publication_not_unique')
    current=read_state(state)
    if (next((a for a in current['assignments'] if a['id']==assignment['id']),None)!=assignment
            or next((s for s in current['slots'] if s['session']==session),None)!=slot):refuse('tracked_build_assignment_changed')
    if json.loads(path.read_text())!=snapshot:refuse('snapshot_changed')
    if now+time.monotonic()-began-at>300:refuse('snapshot_expired')
    if (command(git+['rev-parse','HEAD'])!=head or command(git+['remote','get-url','origin'])!=remote
            or command(git+['status','--porcelain=v1','--untracked-files=all'])):refuse('tracked_build_checkout_changed')
    if identify_session(session,run)!=identity:refuse('session_changed')
    return {'eligible':True,'reason':'verified_handoff','evidence':{
        'assignment_id':assignment['id'],'repo':repo.repo,'role':'build','clone':str(clone),
        'issues':numbers,'head':head,**verified[0],**identity}}
