"""Trusted bridge from fresh GitHub facts and exact processes to issue assignments.

The SQLite workflow owns new dispatch reservations. This adapter validates the
external evidence; it never claims issues, changes PR labels, or terminates work.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sqlite3
import time
import uuid
from datetime import datetime

from . import workflow
from .config import load_repositories
from .control import atomic_json, snapshot_path
from .lifecycle import observe_generation


def enabled(state):
    return (Path(state)/'workflow.enabled').is_file()


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def repository(config, session=None, repo=None):
    matches=[r for r in load_repositories(config) if r.enabled and
             (repo is None or r.repo.lower()==repo.lower()) and
             (session is None or any(r.short+'-'+role==session for role in ('build','review','audit')))]
    if len(matches)!=1:raise workflow.Refused('configured_workflow_identity_required')
    return matches[0]


def snapshot(state, repo, now=None):
    value=json.loads(snapshot_path(state,repo).read_text());now=time.time() if now is None else now
    at=value.get('observed_at')
    if (value.get('repo')!=repo or value.get('status')!='ok' or value.get('enabled') is not True
            or type(at) not in (int,float) or not 0<=now-at<=300):
        raise workflow.Refused('fresh_workflow_snapshot_required')
    return value


def pr_path(state,repo,number):
    return Path(state)/'workflow-prs'/digest(repo)/(str(number)+'.json')


def gh_json(args,run=subprocess.run):
    result=run(['gh',*args],capture_output=True,text=True,timeout=20)
    if result.returncode:raise workflow.Refused('github_assignment_read_failed')
    return json.loads(result.stdout)


def canonical_pr(state,repo,number,role,run=subprocess.run,*,author=None):
    value=gh_json(['pr','view',str(number),'--repo',repo,'--json',
        'number,url,state,isDraft,headRefOid,baseRefOid,closingIssuesReferences,labels,author,reviews'],run)
    labels={x['name'] for x in value.get('labels',[])}
    needed={'review':'ready-for-review','audit':'ready-for-audit'}
    if (value.get('number')!=number or value.get('url')!=f'https://github.com/{repo}/pull/{number}'
            or value.get('state')!='OPEN' or value.get('isDraft') is not False or 'hold' in labels):
        raise workflow.Refused('pr_not_currently_eligible')
    if author is not None and value.get('author',{}).get('login','').lower()!=author.lower():
        raise workflow.Refused('pr_author_changed')
    stage_labels=labels & {'ready-for-review','ready-for-audit','changes-requested'}
    if role in needed and stage_labels!={needed[role]}:raise workflow.Refused('pr_stage_changed')
    if role=='build' and stage_labels not in (set(),{'changes-requested'}):raise workflow.Refused('pr_stage_changed')
    for key in ('headRefOid','baseRefOid'):
        if not re.fullmatch('[0-9a-f]{40,64}',value.get(key,'')):raise workflow.Refused('pr_revision_unknown')
    refs=value.get('closingIssuesReferences')
    if not isinstance(refs,list) or len(refs)>20:raise workflow.Refused('issue_links_unbounded')
    numbers=[]
    for ref in refs:
        n=ref.get('number')
        if type(n) is not int or n<=0 or ref.get('url')!=f'https://github.com/{repo}/issues/{n}':
            raise workflow.Refused('issue_link_identity_conflict')
        linked_repo=ref.get('repository',{}).get('nameWithOwner')
        if linked_repo is not None and linked_repo.lower()!=repo.lower():
            raise workflow.Refused('issue_link_identity_conflict')
        issue=gh_json(['issue','view',str(n),'--repo',repo,'--json','number,url,state,labels'],run)
        if issue.get('number')!=n or issue.get('url')!=ref['url'] or issue.get('state')!='OPEN' or 'hold' in {x['name'] for x in issue.get('labels',[])}:
            raise workflow.Refused('linked_issue_not_open_and_eligible')
        numbers.append(n)
    if not numbers:raise workflow.Refused('pr_requires_linked_issue')
    if role=='audit' or role=='build' and stage_labels=={'changes-requested'}:
        from .workflow_results import before_rework
        before_rework(state,repo,value,author,next_role=role)
    record={'repo':repo,'number':number,'head':value['headRefOid'],'base':value['baseRefOid'],
            'issue_numbers':sorted(set(numbers)),'role':role,'observed_at':time.time()}
    atomic_json(pr_path(state,repo,number),record)
    return record


def facts(state,snap,extra=None):
    from .issue_flow import read_flow
    row={**snap,'short':snap['repo'].split('/')[-1],'queue_status':'ok'}
    flow=read_flow(state,[row],now=snap['observed_at'])
    result={}
    for item in flow['issues']+flow['backlog']+flow['completed']:
        stage=item['state']
        disposition='pending';reason=None
        if item.get('attention_state')=='CRITICAL':disposition='critical';reason='human_approval_required'
        elif item.get('attention_state')=='STALLED' or item.get('on_hold'):disposition='stalled';reason='recorded_issue_blocker'
        if stage=='held':stage={1:'build',2:'review',3:'audit'}.get(item.get('step'),'queued')
        if stage in ('ready','backlog'):stage='queued'
        if stage not in workflow.STAGES:stage='unknown'
        prs=[]
        for number in item.get('pr_numbers',[]):
            pr={'number':number,'head':None,'base':None}
            detail=(snap.get('item_details') or {}).get(str(number),{})
            if (detail.get('kind')=='pr' and detail.get('number')==number
                    and detail.get('revision_status')=='canonical'
                    and all(isinstance(detail.get(k),str) and re.fullmatch('[0-9a-f]{40,64}',detail[k])
                            for k in ('head_sha','base_sha'))):
                pr.update(head=detail['head_sha'],base=detail['base_sha'])
            elif 'revision_status' not in detail:
                # Old snapshots have no revision envelope. A dispatch cache is
                # usable only when no newer PR update is recorded locally.
                try:
                    cached=json.loads(pr_path(state,snap['repo'],number).read_text())
                    updated=detail.get('updated_at')
                    updated=datetime.fromisoformat(updated.replace('Z','+00:00')).timestamp() if updated else 0
                    if (cached['repo']==snap['repo'] and item['number'] in cached['issue_numbers']
                            and cached.get('observed_at',0)>=updated):
                        pr.update(head=cached['head'],base=cached['base'])
                except (OSError,ValueError,KeyError,TypeError):pass
            prs.append(pr)
        result[item['number']]={'number':item['number'],'stage':stage,'disposition':disposition,'reason':reason,'prs':prs}
    for number in snap.get('schedule',{}).get('eligible',[]):
        result.setdefault(number,{'number':number,'stage':'queued','disposition':'pending','reason':None,'prs':[]})
    if extra:
        for number in extra['issue_numbers']:
            item=result.setdefault(number,{'number':number,'stage':extra['role'],'disposition':'pending','reason':None,'prs':[]})
            item['stage']=extra['role']
            item['prs']=[p for p in item['prs'] if p['number']!=extra['number']]+[
                {k:extra[k] for k in ('number','head','base')}]
    return sorted(result.values(),key=lambda x:x['number'])


def reconcile_repository(state,repo,*,extra=None,now=None):
    snap=snapshot(state,repo,now);items=facts(state,snap,extra)
    revision=digest({'work':snap['work'],'schedule':{k:v for k,v in snap['schedule'].items() if k not in {'observed_at','first_ready','first_ready_at'}},'items':items})
    at=snap['observed_at']
    # Canonical selection reads may refine the same snapshot after an earlier import.
    if extra:at=max(at,extra['observed_at'])
    value=workflow.reconcile(state,repo,revision,at,items,
        source_event_id='snapshot:'+digest([repo,revision,at]),now=now)
    if value.get('status')=='ignored_stale':raise workflow.Refused('newer_workflow_evidence_exists')
    return snap,revision


def current(state,session):
    data=workflow.read_state(state)
    slot=next((s for s in data['slots'] if s['session']==session),None)
    assignment=next((a for a in data['assignments'] if slot and a['id']==slot.get('assignment_id')),None)
    return slot,assignment


def validate_target(config,state,a,*,run=subprocess.run):
    repo=repository(config,repo=a['repo']);snap=snapshot(state,repo.repo)
    issues=workflow.read_state(state,repo.repo)['issues']
    if any(i['number'] in a['issue_numbers'] and i['stage']=='failed' for i in issues):
        raise workflow.Refused('issue_retry_limit_reached')
    if a.get('pr'):
        target=canonical_pr(state,repo.repo,a['pr']['number'],a['role'],run,author=repo.author)
        if target['issue_numbers']!=a['issue_numbers'] or any(target[k]!=a['pr'][k] for k in ('head','base')):
            raise workflow.Refused('assignment_candidate_changed')
    else:
        if a['role']!='build' or len(a['issue_numbers'])!=1:raise workflow.Refused('invalid_issue_assignment')
        number=a['issue_numbers'][0]
        eligible=set(snap['schedule'].get('eligible',[]))
        reserving=set(snap['schedule'].get('active',[]))-set(snap['schedule'].get('handed_off',[]))
        detail=snap.get('item_details',{}).get(str(number),{})
        # A label added by another actor after reservation cannot establish our
        # ownership. Only a prior confirmed dispatch authorizes resuming WIP.
        allowed=eligible | (reserving if a.get('submitted_at') is not None else set())
        if number not in allowed or number in snap['work'].get('held',[]) or 'hold' in detail.get('labels',[]):
            raise workflow.Refused('assignment_no_longer_eligible')
        if detail and (detail.get('kind')!='issue' or detail.get('state')!='open'):
            raise workflow.Refused('assignment_issue_not_open')
    return snap


def confirmed_absent(session,run=subprocess.run):
    result=run(['tmux','list-sessions','-F','#{session_name}'],capture_output=True,text=True,timeout=3)
    return (result.returncode==0 and session not in result.stdout.splitlines() or
            result.returncode==1 and result.stderr.startswith('no server running on '))


CANCELABLE_TARGET_REASONS = {
    'issue_retry_limit_reached', 'assignment_candidate_changed', 'assignment_no_longer_eligible', 'assignment_issue_not_open',
    'pr_not_currently_eligible', 'pr_stage_changed', 'pr_author_changed',
    'linked_issue_not_open_and_eligible', 'pr_requires_linked_issue', 'issue_link_identity_conflict',
}


def reserve(config,state,session,*,run=subprocess.run):
    from .workflow_lifecycle import reconcile as reconcile_releases
    reconcile_releases(state)
    repo=repository(config,session);role=session.rsplit('-',1)[1]
    from .execution_retirement import is_quarantined
    if is_quarantined(config,state,session):
        raise workflow.Refused('preserved_workspace_requires_reconciliation')
    slot,assignment=current(state,session)
    if assignment:
        if assignment['state'] in {'reserved','blocked'} and assignment['generation'] is None:
            try:
                validate_target(config,state,assignment,run=run)
            except workflow.Refused as exc:
                if assignment.get('submitted_at') is not None and assignment.get('attempt',1)<=1:
                    raise workflow.Refused('previously_dispatched_assignment_requires_reconciliation')
                if str(exc) not in CANCELABLE_TARGET_REASONS:raise
                if not confirmed_absent(session,run):raise workflow.Refused('unstarted_slot_absence_unverified')
                workflow.transition(state,assignment['id'],'retired',generation=None,
                    proof={'verified':True,'released':True,'kind':'verified_unstarted_attempt' if assignment.get('attempt',1)>1 else 'verified_no_dispatch','assignment_id':assignment['id'],'generation':None},
                    reason='unstarted_assignment_became_ineligible',source_event_id='cancel:'+str(uuid.uuid4()))
                slot,assignment=current(state,session)
            else:return assignment
        else:raise workflow.Refused('existing_execution_requires_reconciliation')
    if slot and slot['state']!='free':raise workflow.Refused('existing_slot_still_occupied')
    snap=snapshot(state,repo.repo);target=None;numbers=[]
    def exhausted(numbers):
        return any(i['number'] in numbers and i['stage']=='failed' for i in workflow.read_state(state,repo.repo)['issues'])
    candidates=snap['work'].get({'build':'sentback','review':'review','audit':'audit'}[role],[])
    if role=='build':candidates=list(dict.fromkeys(candidates+snap['work'].get('limbo',[])))
    refused=[]
    for number in candidates[:20]:
        try:
            candidate=canonical_pr(state,repo.repo,number,role,run,author=repo.author)
            if exhausted(candidate['issue_numbers']):
                refused.append('issue_retry_limit_reached');continue
            target=candidate;numbers=target['issue_numbers'];break
        except workflow.Refused as exc:refused.append(str(exc))
    if target is None:
        eligible=[n for n in snap['schedule'].get('eligible',[]) if not exhausted([n])] if role=='build' else []
        if not eligible:raise workflow.Refused(refused[0] if refused else 'no_eligible_assignment')
        numbers=[eligible[0]]
    snap,revision=reconcile_repository(state,repo.repo,extra=target)
    assignment=workflow.reserve(state,repo.repo,role,session,revision,numbers,
        pr={k:target[k] for k in ('number','head','base')} if target else None,
        max_slots=int(os.environ.get('MAX_SESSIONS','3')),max_per_role=int(os.environ.get('MAX_SESSIONS_PER_ROLE','1')),
        source_event_id='reserve:'+str(uuid.uuid4()))
    return assignment


def assignment_prompt(state,session):
    slot,a=current(state,session)
    if not a:
        if slot and slot.get('bootstrap'):
            return 'This execution started before assignment tracking. Continue only its existing assigned work; do not claim another issue. Return after its handoff so the controller can create the next tracked assignment.'
        raise workflow.Refused('exact_assignment_required')
    issues=', '.join('#'+str(n) for n in a['issue_numbers'])
    target=f"PR #{a['pr']['number']} at head {a['pr']['head']}" if a['pr'] else f'issue {issues}'
    return (f"Controller assignment {a['id']}: {a['repo']}, {a['role']}, {target}. "
        f"Linked issues: {issues}. Work only this assignment; do not select or claim any other issue. "
        f"Before a new build claim run ~/ops/forge-watchdog/bin/forge next --repo {a['repo']} --assignment {a['id']}. "
        "If it returns no issue, do not choose another. Recheck current GitHub ownership, labels and candidate before edits; "
        "a changed candidate requires controller reconciliation. Finish this step and return; never start a sibling issue in this execution.")


def mutate(config,state,session,action,generation,provider=None,reason=None):
    repository(config,session)
    slot,a=current(state,session)
    if generation is not None:
        observed=observe_generation(session)
        if not observed or observed['id']!=generation:raise workflow.Refused('generation_changed')
    elif action!='blocked' or not a or a['generation'] is not None:
        raise workflow.Refused('generation_required')
    if not a:
        if slot and slot.get('bootstrap') and slot.get('generation')==generation:return {'status':'legacy_execution_preserved'}
        raise workflow.Refused('exact_assignment_required')
    if action=='bind' and a['generation']==generation and a['provider']==provider:return a
    return workflow.transition(state,a['id'],action,generation=generation,provider=provider,reason=reason,
        proof={'verified':True,'kind':'verified_delivery','assignment_id':a['id'],'generation':generation} if action=='submitted' else None,
        source_event_id='dispatch:'+str(uuid.uuid4()))


def observe_slots(config,state,*,bootstrap=False,run=subprocess.run):
    from .activity import observe_tmux
    repos=load_repositories(config)
    allowed={r.short+'-'+role:(r.repo,role) for r in repos for role in ('build','review','audit')}
    observations=observe_tmux(allowed,state=state)
    for session,(repo,role) in allowed.items():
        observation=observations.get(session,{})
        slot,a=current(state,session);gen=observation.get('generation_id')
        phase=observation.get('state');provider=observation.get('provider')
        provider=provider if provider in ('claude','codex') else None
        if a:
            assigned_generation=a.get('generation')
            # A queue becoming empty must not strand an unstarted reservation.
            # Only a positive absence observation and confirmed invalid target
            # authorize cancellation; migrated but unstarted attempts use a distinct
            # proof preserving their lifetime dispatch and migration history.
            if (assigned_generation is None and (a.get('submitted_at') is None or a.get('attempt',1)>1)
                    and a['state'] in {'reserved','blocked'} and gen is None and phase=='exited'
                    and observation.get('reason') in {'session_absent','no_tmux_server'}):
                try:validate_target(config,state,a,run=run)
                except workflow.Refused as exc:
                    if str(exc) in CANCELABLE_TARGET_REASONS:
                        workflow.transition(state,a['id'],'retired',generation=None,
                            proof={'verified':True,'released':True,'kind':'verified_unstarted_attempt' if a.get('attempt',1)>1 else 'verified_no_dispatch',
                                   'assignment_id':a['id'],'generation':None},
                            reason='unstarted_assignment_became_ineligible',source_event_id='cancel:'+str(uuid.uuid4()))
                continue
            if gen!=assigned_generation or phase=='exited':
                # Absence/replacement is a diagnostic, never release proof.
                # Keep the old assignment and logical slot occupied.
                if assigned_generation and (gen or phase=='exited'):
                    reason='assigned_worker_missing' if phase=='exited' else 'assignment_generation_mismatch'
                    if a['state']!='blocked' or a.get('reason')!=reason:
                        try:workflow.transition(state,a['id'],'blocked',generation=assigned_generation,
                            reason=reason,source_event_id='observe:'+str(uuid.uuid4()))
                        except workflow.Refused:pass
                continue
            action='working' if phase=='busy' else 'idle' if phase=='idle' else 'blocked' if phase=='blocked' else None
            execution=({'run_id':observation['provider_run_id'],'thread_id':observation.get('provider_thread_id'),
                        'started_at':observation['execution_started_at']}
                       if action in {'working','idle'} and observation.get('provider_run_id') and observation.get('execution_started_at') else None)
            same_execution=execution is None or a.get('execution')==execution
            if (not action or action=='working' and a['state']=='working' and same_execution
                    or action=='idle' and (a['state'] not in {'working','submitted'}
                        or slot['state']=='retiring' and same_execution)
                    or action=='blocked' and a['state']=='blocked' and a.get('reason')==observation.get('reason')):continue
            try:workflow.transition(state,a['id'],action,generation=gen,reason=observation.get('reason'),execution=execution,
                source_event_id='observe:'+str(uuid.uuid4()))
            except workflow.Refused:pass
        elif gen:
            phase={'busy':'working','idle':'retiring','shell':'starting'}.get(phase,'blocked')
            legacy=bool(bootstrap or slot and slot.get('bootstrap'))
            reason='started_before_assignment_tracking' if legacy else 'execution_without_assignment'
            if slot and slot.get('generation')==gen and slot['state']==phase:continue
            workflow.observe_slot(state,session,repo,role,gen,phase,provider=provider,reason=reason,bootstrap=legacy,
                source_event_id='slot-observed:'+str(uuid.uuid4()))
        elif phase=='exited' and slot is None:
            workflow.observe_slot(state,session,repo,role,None,'free',source_event_id='slot-created:'+digest(session))


def next_for_assignment(state,repo,assignment_id,plan):
    data=workflow.read_state(state,repo)
    a=next((a for a in data['assignments'] if a['id']==assignment_id),None)
    if not a or a['repo'].lower()!=repo.lower() or a['role']!='build' or a['pr'] is not None or len(a['issue_numbers'])!=1:
        raise workflow.Refused('not_a_new_issue_assignment')
    if a['state'] not in {'reserved','submitted','working'}:raise workflow.Refused('assignment_not_active')
    observed=observe_generation(a['session'])
    if not observed or observed['id']!=a['generation']:raise workflow.Refused('assignment_generation_changed')
    number=a['issue_numbers'][0]
    if any(i['number']==number and i['stage']=='failed' for i in data['issues']):return None
    return number if number in plan.get('eligible',[]) else None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['retry-failed','bootstrap','reconcile','observe','reserve','prompt','current','guard','bind','submitted','uncertain','begin-send','blocked'])
    parser.add_argument('--state',required=True);parser.add_argument('--config',required=True)
    parser.add_argument('--session');parser.add_argument('--repo');parser.add_argument('--generation');parser.add_argument('--provider');parser.add_argument('--reason')
    parser.add_argument('--issue',type=int);parser.add_argument('--event-id')
    args=parser.parse_args()
    try:
        if args.action=='retry-failed':
            repo=repository(args.config,repo=args.repo)
            print(json.dumps(workflow.retry_failed(args.state,repo.repo,args.issue,reason=args.reason,source_event_id=args.event_id)))
        elif args.action=='bootstrap':
            for repo in load_repositories(args.config):
                if repo.enabled:
                    try:reconcile_repository(args.state,repo.repo)
                    except (OSError,ValueError,workflow.Refused):continue
            observe_slots(args.config,args.state,bootstrap=True)
            atomic_json(Path(args.state)/'workflow.enabled',{'schema':1,'enabled_at':time.time()})
        elif args.action=='reconcile':
            from .workflow_lifecycle import reconcile as releases
            releases(args.state)
            reconcile_repository(args.state,repository(args.config,repo=args.repo).repo)
        elif args.action=='observe':observe_slots(args.config,args.state)
        elif args.action=='reserve':print(reserve(args.config,args.state,args.session)['id'])
        elif args.action=='prompt':print(assignment_prompt(args.state,args.session))
        elif args.action=='guard':
            slot,a=current(args.state,args.session)
            actual=observe_generation(args.session)
            if not actual or actual['id']!=args.generation:raise workflow.Refused('generation_changed')
            if not a:
                if not slot or not slot.get('bootstrap') or slot.get('generation')!=args.generation:raise workflow.Refused('exact_assignment_required')
            elif a['generation']!=args.generation or a['state'] not in {'reserved','submitted'}:
                raise workflow.Refused('assignment_not_ready_for_dispatch')
            else:validate_target(args.config,args.state,a)
        elif args.action=='current':
            slot,a=current(args.state,args.session)
            if not a:return 1
            print(a['id'])
        else:mutate(args.config,args.state,args.session,'uncertain' if args.action=='begin-send' else args.action,args.generation,args.provider,
                    'dispatch_in_progress' if args.action=='begin-send' else 'delivery_uncertain' if args.action=='uncertain' else args.reason)
        return 0
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,workflow.Refused) as exc:
        reason=str(exc) if isinstance(exc,workflow.Refused) else 'workflow_evidence_unavailable'
        print(json.dumps({'reason':reason}),file=__import__('sys').stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
