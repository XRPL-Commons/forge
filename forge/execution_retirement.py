"""Retire a finished execution while quarantining its preserved workspace.

Terminal execution is not successful issue completion. No GitHub state or clone
contents are changed. Quarantines must be explicitly reconciled before reuse.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import subprocess
import time

from . import codex_worker, workflow, workflow_lifecycle
from .config import load_repositories
from .control import atomic_json
from .handoff import Unverified, bounded_run, command, release_command, styled_lines
from .human_wait import _locked


def _digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _read(path,limit=65536):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size>limit:
        raise Unverified('retirement_evidence_invalid')
    return json.loads(path.read_text())


def _directory(state,create=False):
    root=Path(state)/'execution-retirements'
    if create:root.mkdir(mode=0o700,parents=True,exist_ok=True)
    if root.exists() and (root.is_symlink() or not root.is_dir() or root.stat().st_mode & 0o077):
        raise Unverified('retirement_directory_not_private')
    return root


def _configured(config,session):
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}-(build|review|audit)',session):
        raise Unverified('retirement_session_invalid')
    role=session.rsplit('-',1)[1]
    rows=[r for r in load_repositories(config) if r.enabled and r.short+'-'+role==session]
    if len(rows)!=1:raise Unverified('retirement_repository_unverified')
    repo=rows[0]
    configured=getattr(repo,'clone_'+role)
    clone=Path(configured or Path.home()/(repo.short if role=='build' else repo.short+'-'+role)).resolve()
    return repo,role,clone


def _identity(state,session,repo,run):
    raw=command(['tmux','list-panes','-s','-t','='+session,'-F',
        '#{session_name}|#{session_id}|#{session_created}|#{pane_id}|#{pane_pid}|#{pane_current_command}'],run)
    rows=raw.splitlines();parts=rows[0].split('|') if len(rows)==1 else []
    if len(parts)!=6 or parts[0]!=session or parts[5]!='bash':raise Unverified('terminal_shell_required')
    gen=codex_worker.parse_generation('|'.join(parts[1:5]))
    observed=codex_worker.observe(state,session,generation=gen['id'],repo=repo)
    receipt=_read(Path(state)/'codex-workers'/(session+'.json'))
    if (observed.get('provider')!='codex' or observed.get('state') not in {'idle','blocked'}
            or receipt.get('state') not in {'completed','failed'} or receipt.get('run_id')!=observed.get('run_id')
            or receipt.get('generation')!=gen):raise Unverified('terminal_execution_unverified')
    if receipt.get('reason') in {'background_inventory_unavailable','background_inventory_unbounded'}:
        raise Unverified('background_inventory_unverified')
    provider=receipt.get('provider_process')
    if (not isinstance(provider,dict) or type(provider.get('pid')) is not int
            or provider['pid']<=1 or provider.get('process_session')!=provider['pid']):
        raise Unverified('provider_process_session_unverified')
    # Fresh inventory is required even when an earlier failure masked a failed
    # end-of-run inventory. Reparented children may no longer belong to the pane.
    try:
        remaining=codex_worker.remaining_group(provider['pid'])
    except (OSError,ValueError,KeyError,TypeError):
        raise Unverified('background_inventory_unverified')
    if remaining:raise Unverified('execution_background_processes')
    completed=receipt.get('completed_at')
    if (type(completed) not in (int,float) or not math.isfinite(completed)
            or not receipt['started_at']<=completed<=time.time()-60):
        raise Unverified('terminal_execution_not_settled')
    pane=command(['tmux','capture-pane','-e','-p','-t',gen['pane_id']],run)
    lines=[''.join(c for c,_ in line).strip() for line in styled_lines(pane)]
    lines=[line for line in lines if line]
    if not lines or not re.fullmatch(r'[\w.-]+@[\w.-]+:[^$#\r\n]*[$#]',lines[-1]):
        raise Unverified('not_empty_shell_prompt')
    processes={}
    for line in command(['ps','-eo','pid=,ppid=,comm='],run).splitlines():
        pid,parent,name=line.split(None,2);processes[int(pid)]=(int(parent),name)
    pid=gen['pane_pid']
    if processes.get(pid,(None,None))[1]!='bash' or any(parent==pid for parent,_ in processes.values()):
        raise Unverified('processes_not_quiescent')
    return gen,receipt


def _raw(args,run):
    result=run(args,capture_output=True,text=True,timeout=15)
    if result.returncode:raise Unverified('workspace_read_failed')
    return result.stdout


def _proof(config,state,session,run):
    repo,role,clone=_configured(config,session)
    gen,receipt=_identity(state,session,repo.repo,run)
    if receipt.get('clone')!=str(clone) or receipt.get('role')!=role:raise Unverified('execution_clone_mismatch')
    view=workflow.read_state(state)
    if view.get('status') not in {'ok','uninitialized'}:raise Unverified('retirement_ledger_unavailable')
    slots=[s for s in view['slots'] if s['session']==session]
    if len(slots)>1:raise Unverified('retirement_slot_ambiguous')
    slot=slots[0] if slots else None
    assignment=None
    if slot:
        if slot.get('generation')!=gen['id'] or slot['repo']!=repo.repo.lower() or slot['role']!=role:
            raise Unverified('retirement_slot_changed')
        if slot.get('assignment_id'):
            matches=[a for a in view['assignments'] if a['id']==slot['assignment_id']]
            if len(matches)!=1:raise Unverified('retirement_assignment_unavailable')
            a=matches[0]
            if (a['generation']!=gen['id'] or a['repo']!=repo.repo.lower() or a['role']!=role
                    or a['session']!=session or a['state']=='retired'):
                raise Unverified('retirement_assignment_changed')
            execution=a.get('execution') or {}
            if execution.get('run_id') and execution['run_id']!=receipt['run_id']:
                raise Unverified('assignment_execution_changed')
            if a.get('submitted_at') is not None and receipt['completed_at']<a['submitted_at']:
                raise Unverified('assignment_execution_predates_submission')
            assignment={k:a.get(k) for k in ('id','repo','role','session','generation','issue_numbers','pr','submitted_at','attempt')}
    git=['git','-C',str(clone)]
    if Path(command(git+['rev-parse','--show-toplevel'],run)).resolve()!=clone:raise Unverified('clone_mismatch')
    remote=command(git+['remote','get-url','origin'],run)
    if remote not in {f'git@github.com:{repo.repo}.git',f'https://github.com/{repo.repo}',f'https://github.com/{repo.repo}.git'}:
        raise Unverified('remote_mismatch')
    head=command(git+['rev-parse','HEAD'],run)
    if not re.fullmatch('[0-9a-f]{40,64}',head):raise Unverified('retirement_head_invalid')
    branch=command(git+['rev-parse','--abbrev-ref','HEAD'],run)
    status=_raw(git+['status','--porcelain=v1','-z','--untracked-files=all'],run)
    diff=_raw(git+['diff','--binary','HEAD','--'],run)
    if len(status)>65536 or len(diff)>2*1024*1024:raise Unverified('workspace_checkpoint_unbounded')
    names=_raw(git+['ls-files','--others','--exclude-standard','-z'],run).strip('\0').split('\0')
    names=[n for n in names if n]
    if len(names)>200:raise Unverified('workspace_checkpoint_unbounded')
    untracked=[];total=0
    for name in names:
        path=clone/name
        if Path(name).is_absolute() or '..' in Path(name).parts or not path.parent.resolve().is_relative_to(clone):
            raise Unverified('workspace_path_unverified')
        info=path.lstat();total+=info.st_size
        if total>16*1024*1024:raise Unverified('workspace_checkpoint_unbounded')
        if stat.S_ISLNK(info.st_mode):data=str(path.readlink()).encode()
        elif stat.S_ISREG(info.st_mode):data=path.read_bytes()
        else:raise Unverified('workspace_file_unverified')
        untracked.append({'path':name,'sha256':hashlib.sha256(data).hexdigest(),'mode':info.st_mode})
    final='';log_sha=None
    if receipt.get('raw_log'):
        log=Path(receipt['raw_log'])
        if log.is_symlink() or not log.resolve().is_relative_to((Path(state)/'codex-workers').resolve()) or log.stat().st_size>16*1024*1024:
            raise Unverified('execution_log_unverified')
        data=log.read_bytes();log_sha=hashlib.sha256(data).hexdigest()
        for line in data.decode().splitlines():
            event=json.loads(line)
            if event.get('type')=='item.completed' and event.get('item',{}).get('type')=='agent_message':
                final=event['item'].get('text','')
        if not isinstance(final,str):raise Unverified('execution_log_unverified')
    failure=None;path=Path(state)/'lifecycle'/(session+'.json')
    if path.exists():
        value=_read(path)
        saved=value.get('generation')
        if (saved.get('id') if isinstance(saved,dict) else saved)==gen['id'] or value.get('generation_id')==gen['id']:
            failure={k:value.get(k) for k in ('phase','reason','since','observed_at')}
    final_gen,final_receipt=_identity(state,session,repo.repo,run)
    if final_gen!=gen or _digest(final_receipt)!=_digest(receipt):raise Unverified('terminal_execution_changed')
    run.remaining()
    return {'repo':repo.repo,'role':role,'session':session,**{k:gen[k] for k in ('session_id','session_created','pane_id','pane_pid')},
        'generation':gen['id'],'provider':'codex','clone':str(clone),'assignment':assignment,
        'head':head,'branch':branch,'git_status':status,'diff_sha256':hashlib.sha256(diff.encode()).hexdigest(),
        'untracked':untracked,'run_id':receipt['run_id'],'thread_id':receipt.get('thread_id'),
        'receipt_sha256':_digest(receipt),'log_sha256':log_sha,'execution_state':receipt['state'],
        'execution_reason':receipt.get('reason'),'final_message':final[-6000:],'final_message_sha256':hashlib.sha256(final.encode()).hexdigest(),
        'last_lifecycle_failure':failure}


def check(config,state,session,*,run=subprocess.run):
    try:return {'eligible':True,'reason':'verified_terminal_execution','evidence':_proof(config,state,session,bounded_run(run,30))}
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        return {'eligible':False,'reason':str(exc) if isinstance(exc,Unverified) else 'retirement_evidence_unavailable','evidence':{}}


def read_quarantines(state):
    root=_directory(state)
    if not root.exists():return []
    paths=list(root.glob('*.json'))
    if len(paths)>1000:raise Unverified('quarantine_inventory_unbounded')
    rows=[]
    for path in paths:
        value=_read(path)
        if (not re.fullmatch('[0-9a-f]{64}',path.stem) or value.get('schema')!=1 or value.get('id')!=path.stem
                or value.get('phase') not in {'prepared','confirmed'}):raise Unverified('quarantine_record_invalid')
        checkpoint=_read(root/'checkpoints'/(path.stem+'.json'),4*1024*1024)
        if _digest(checkpoint)!=value.get('checkpoint_sha256'):raise Unverified('quarantine_checkpoint_changed')
        from .execution_reconciliation import acknowledged
        if acknowledged(state,value,checkpoint):continue
        e=checkpoint['evidence']
        rows.append({'id':value['id'],'session':e['session'],'repo':e['repo'],'role':e['role'],'clone':e['clone'],
            'generation':e['generation'],'assignment_id':(e.get('assignment') or {}).get('id'),
            'issue_numbers':(e.get('assignment') or {}).get('issue_numbers',[]),'pr':(e.get('assignment') or {}).get('pr'),
            'phase':value['phase'],'released':value.get('released') is True,'reason':'workspace_reconciliation_required',
            'head':e['head'],'branch':e['branch'],'execution_state':e['execution_state'],
            'final_message':e['final_message'],'final_message_sha256':e['final_message_sha256'],
            'last_lifecycle_failure':e['last_lifecycle_failure'],
            'identity_basis':'exact_assignment' if e.get('assignment') else 'legacy_identity_unknown'})
    return rows


def is_quarantined(config,state,session):
    _,_,clone=_configured(config,session)
    return any(Path(row['clone']).resolve()==clone for row in read_quarantines(state))


def release(config,state,session,*,lock_fd=8,run=subprocess.run):
    if not _locked(state,lock_fd):return {'released':False,'reason':'supervisor_lock_required'}
    proof_run=bounded_run(run,45)
    try:
        first=_proof(config,state,session,proof_run)
        proof={'eligible':True,'reason':'verified_terminal_execution','evidence':first}
        ticket=workflow_lifecycle.prepare(state,session,proof)
        root=_directory(state,True);checkpoints=root/'checkpoints';checkpoints.mkdir(mode=0o700,exist_ok=True)
        if checkpoints.is_symlink() or checkpoints.stat().st_mode & 0o077:raise Unverified('checkpoint_directory_not_private')
        key=hashlib.sha256((session+'\0'+first['generation']+'\0'+first['run_id']).encode()).hexdigest()
        checkpoint={'schema':1,'id':key,'evidence':first}
        path=checkpoints/(key+'.json')
        if path.exists():
            if _read(path,4*1024*1024)!=checkpoint:raise Unverified('retirement_checkpoint_changed')
        else:atomic_json(path,checkpoint)
        record={'schema':1,'id':key,'phase':'prepared','released':False,'checkpoint_sha256':_digest(checkpoint),'observed_at':time.time()}
        atomic_json(root/(key+'.json'),record)
        second=_proof(config,state,session,proof_run)
        if first!=second:raise Unverified('retirement_evidence_changed')
        if not _locked(state,lock_fd):raise Unverified('supervisor_lock_required')
        release_command(['tmux','kill-session','-t',first['session_id']],proof_run,run)
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError) as exc:
        return {'released':False,'reason':str(exc) if isinstance(exc,Unverified) else 'retirement_evidence_unavailable'}
    # Successful teardown remains successful even if durable follow-up needs replay.
    marker_error = False
    try:(Path(state)/(session+'.up')).unlink(missing_ok=True)
    except OSError:marker_error = True
    ledger=workflow_lifecycle.confirmed(state,ticket)
    try:atomic_json(root/(key+'.json'),{**record,'phase':'confirmed','released':True,'released_at':time.time()})
    except OSError:return {'released':True,'reason':'workspace_quarantined','workflow':ledger,'quarantine':'confirmation_pending'}
    return {'released':True,'reason':'workspace_quarantined','workflow':ledger,'quarantine':key,'marker_cleanup_pending':marker_error}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['check','release','quarantined']);parser.add_argument('--config',required=True)
    parser.add_argument('--state',required=True);parser.add_argument('--session',required=True);parser.add_argument('--lock-fd',type=int,default=8)
    args=parser.parse_args()
    if args.action=='quarantined':
        try:return 0 if is_quarantined(args.config,args.state,args.session) else 1
        except (OSError,ValueError,KeyError,TypeError):return 2
    result=(release(args.config,args.state,args.session,lock_fd=args.lock_fd) if args.action=='release'
            else check(args.config,args.state,args.session))
    # CLI never emits private final messages, file names, or raw receipts.
    print(json.dumps({k:v for k,v in result.items() if k!='evidence'}))
    return 0 if result.get('released',result.get('eligible')) else 1


if __name__=='__main__':raise SystemExit(main())
